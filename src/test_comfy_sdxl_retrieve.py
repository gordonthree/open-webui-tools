"""
Tests for comfy_sdxl_retrieve.py. Run with:  python -m unittest test_comfy_sdxl_retrieve -v
(no ComfyUI server or Open WebUI needed - HTTP is faked).
"""

import json
import re
import shutil
import sqlite3
import tempfile
import unittest
import urllib.parse
import uuid
from pathlib import Path
from unittest.mock import patch

import comfy_sdxl_retrieve as mod


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = content
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mod.RequestException(f"HTTP {self.status_code}")


class FakeRetrieveComfy:
    """
    Stands in for the `requests` module, for a single ComfyUI server. No /prompt endpoint -
    comfy_sdxl_retrieve.py never submits a job, only looks results up.
    """

    def __init__(self, upload_status=200):
        self.files: set = set()  # (subfolder, filename)
        self.file_bytes: dict = {}
        self.history: dict = {}  # comfy_prompt_id -> history entry
        self.queue = {"queue_running": [], "queue_pending": []}
        self.upload_status = upload_status
        self.uploads: list = []
        self.history_list_requests = 0  # counts GET /history (list mode), not /history/<id>

    def seed_file(self, subfolder, filename, data=b"\x89PNG-fake"):
        self.files.add((subfolder, filename))
        self.file_bytes[(subfolder, filename)] = data

    def seed_history(self, job_id, entry):
        self.history[job_id] = entry

    def set_queue(self, running=None, pending=None):
        self.queue = {"queue_running": running or [], "queue_pending": pending or []}

    def get(self, url, timeout=None, params=None):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if path == "/view":
            query = urllib.parse.parse_qs(parsed.query)
            key = (query.get("subfolder", [""])[0], query.get("filename", [""])[0])
            if key in self.files:
                return FakeResponse(200, content=self.file_bytes.get(key, b"\x89PNG-fake"))
            return FakeResponse(404)
        if path.startswith("/history/"):
            job_id = path.rsplit("/", 1)[-1]
            entry = self.history.get(job_id)
            return FakeResponse(200, {job_id: entry} if entry else {})
        if path == "/history":
            self.history_list_requests += 1
            max_items = (params or {}).get("max_items")
            items = list(self.history.items())
            if max_items:
                items = items[-max_items:]
            return FakeResponse(200, dict(items))
        if path == "/queue":
            return FakeResponse(200, self.queue)
        return FakeResponse(404)

    def post(self, url, json=None, timeout=None, headers=None, files=None, data=None, params=None):
        if url.endswith("/api/v1/files/"):
            self.uploads.append({"url": url, "headers": headers, "files": files})
            if self.upload_status != 200:
                return FakeResponse(self.upload_status, {"detail": "nope"})
            return FakeResponse(200, {"id": "file-123"})
        return FakeResponse(404)


class FakeMultiServerRetrieveComfy:
    """Like FakeRetrieveComfy, but keys everything by base URL so one patched `requests` can
    stand in for several ComfyUI servers at once - needed for reconciliation's server-scoped
    matching and multi-server search_jobs tests."""

    def __init__(self):
        self.servers: dict = {}

    def _server(self, url) -> FakeRetrieveComfy:
        origin = urllib.parse.urlparse(url)
        base = f"{origin.scheme}://{origin.netloc}"
        return self.servers.setdefault(base, FakeRetrieveComfy())

    def seed_file(self, server, subfolder, filename, data=b"\x89PNG-fake"):
        self._server(server).seed_file(subfolder, filename, data)

    def seed_history(self, server, job_id, entry):
        self._server(server).seed_history(job_id, entry)

    def set_queue(self, server, running=None, pending=None):
        self._server(server).set_queue(running, pending)

    def history_list_requests(self, server) -> int:
        return self._server(server).history_list_requests

    def get(self, url, timeout=None, params=None):
        return self._server(url).get(url, timeout=timeout, params=params)

    def post(self, url, **kwargs):
        return self._server(url).post(url, **kwargs)


def _seed_job(
    db_path,
    job_uuid=None,
    tool="generate_image",
    server="http://s1:8188",
    status="built",
    created_at=None,
    comfy_prompt_id=None,
    graph=None,
    positive_prompt=None,
    negative_prompt=None,
    node_params=None,
    history_entry=None,
):
    """Writes directly into all seven job-DB tables via mod.job_db_connect, independent of any
    lifecycle function (this file has none of its own to round-trip through) - gives full control
    over status/prompts/checkpoint/seed/timestamps for test setup."""
    conn = mod.job_db_connect(db_path)
    try:
        job_uuid = job_uuid or str(uuid.uuid4())
        created_at = created_at or mod._now_iso()
        graph = {} if graph is None else graph
        conn.execute(
            "INSERT INTO jobs (job_uuid, comfy_prompt_id, tool, server, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_uuid, comfy_prompt_id, tool, server, status, created_at),
        )
        conn.execute(
            "INSERT INTO inputs (job_uuid, raw_json, recorded_at) VALUES (?, ?, ?)",
            (job_uuid, json.dumps({"tool": tool}), created_at),
        )
        conn.execute(
            "INSERT INTO outputs (job_uuid, raw_json, recorded_at) VALUES (?, ?, ?)",
            (job_uuid, json.dumps(graph), created_at),
        )
        conn.execute(
            "INSERT INTO prompts (job_uuid, positive_prompt, negative_prompt) VALUES (?, ?, ?)",
            (job_uuid, positive_prompt, negative_prompt),
        )
        if mod.has_prompts_fts(conn):
            conn.execute(
                "INSERT INTO prompts_fts (job_uuid, positive_prompt, negative_prompt) VALUES (?, ?, ?)",
                (job_uuid, positive_prompt or "", negative_prompt or ""),
            )
        for node_id, class_type, param_name, value_text, value_num in (node_params or []):
            conn.execute(
                "INSERT INTO node_params (job_uuid, node_id, class_type, param_name, value_text, value_num) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_uuid, node_id, class_type, param_name, value_text, value_num),
            )
        if history_entry is not None:
            conn.execute(
                "INSERT INTO results (job_uuid, stage, raw_json, recorded_at) VALUES (?, 'history', ?, ?)",
                (job_uuid, json.dumps(history_entry), created_at),
            )
        conn.commit()
    finally:
        conn.close()
    return job_uuid


class SearchJobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod._job_db_schema_ready.clear()
        self.tool = mod.Tools()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmpdir) / "jobs.sqlite3")
        self.tool.valves.JOB_DB_PATH = self.db_path

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_db_schema_ready.clear()

    async def test_no_database_file_returns_empty_without_creating_one(self):
        res = await self.tool.search_jobs()
        self.assertTrue(res["success"])
        self.assertEqual(res["jobs"], [])
        self.assertFalse(Path(self.db_path).exists())

    async def test_filters_by_prompt_text_via_fts(self):
        _seed_job(self.db_path, positive_prompt="a lighthouse at dusk", negative_prompt="blurry")
        _seed_job(self.db_path, positive_prompt="a cat on a windowsill", negative_prompt="blurry")
        res = await self.tool.search_jobs(prompt_text="lighthouse")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)
        self.assertEqual(res["jobs"][0]["positive_prompt"], "a lighthouse at dusk")

    async def test_prompt_text_falls_back_to_like_when_fts_unavailable(self):
        _seed_job(self.db_path, positive_prompt="a lighthouse at dusk", negative_prompt="blurry")
        _seed_job(self.db_path, positive_prompt="a cat on a windowsill", negative_prompt="blurry")
        with patch.object(mod, "has_prompts_fts", return_value=False):
            res = await self.tool.search_jobs(prompt_text="lighthouse")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)
        self.assertEqual(res["jobs"][0]["positive_prompt"], "a lighthouse at dusk")

    async def test_filters_by_job_search_matching_filename(self):
        _seed_job(
            self.db_path, status="completed", positive_prompt="match me",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "findable.png", "subfolder": "", "type": "output"}]}}},
        )
        _seed_job(
            self.db_path, status="completed", positive_prompt="not this one",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "other.png", "subfolder": "", "type": "output"}]}}},
        )
        res = await self.tool.search_jobs(job_search="findable")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)
        self.assertIn("findable.png", res["jobs"][0]["filenames"])

    async def test_filters_by_job_search_matching_job_uuid(self):
        job_uuid = _seed_job(self.db_path, positive_prompt="find by id")
        _seed_job(self.db_path, positive_prompt="not this one")
        res = await self.tool.search_jobs(job_search=job_uuid)
        self.assertTrue(res["success"], res)
        self.assertEqual([j["job_uuid"] for j in res["jobs"]], [job_uuid])

    async def test_filters_by_checkpoint_substring_case_insensitive(self):
        _seed_job(
            self.db_path,
            node_params=[("15", "CheckpointLoaderSimple", "ckpt_name", "epicrealismXL_pureFix.safetensors", None)],
        )
        _seed_job(
            self.db_path,
            node_params=[("15", "CheckpointLoaderSimple", "ckpt_name", "dreamshaperXL.safetensors", None)],
        )
        res = await self.tool.search_jobs(checkpoint="epicrealism")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)

    async def test_filters_by_exact_seed(self):
        _seed_job(self.db_path, node_params=[("12", "KSampler", "seed", "42", 42.0)])
        _seed_job(self.db_path, node_params=[("12", "KSampler", "seed", "7", 7.0)])
        res = await self.tool.search_jobs(seed=42)
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)

    async def test_filters_by_seed_on_ksampler_advanced_noise_seed(self):
        _seed_job(self.db_path, node_params=[("12", "KSamplerAdvanced", "noise_seed", "99", 99.0)])
        _seed_job(self.db_path, node_params=[("12", "KSamplerAdvanced", "noise_seed", "7", 7.0)])
        res = await self.tool.search_jobs(seed=99)
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)

    async def test_filters_by_server_and_tool(self):
        _seed_job(self.db_path, server="http://s1:8188", tool="generate_image")
        _seed_job(self.db_path, server="http://s2:8188", tool="generate_image")
        _seed_job(self.db_path, server="http://s1:8188", tool="run_workflow")
        res = await self.tool.search_jobs(gpu_server="http://s1:8188", tool="generate_image")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)

    async def test_filters_by_status_list(self):
        _seed_job(self.db_path, status="built")
        _seed_job(self.db_path, status="completed")
        _seed_job(self.db_path, status="rejected")
        _seed_job(self.db_path, status="failed")
        res = await self.tool.search_jobs(status=["completed", "failed"])
        self.assertTrue(res["success"], res)
        self.assertEqual({j["status"] for j in res["jobs"]}, {"completed", "failed"})

    async def test_filters_by_date_range(self):
        _seed_job(self.db_path, job_uuid="job-jan", created_at="2026-01-01T00:00:00+00:00")
        _seed_job(self.db_path, job_uuid="job-mid-jan", created_at="2026-01-15T23:00:00+00:00")
        _seed_job(self.db_path, job_uuid="job-feb", created_at="2026-02-01T00:00:00+00:00")

        res = await self.tool.search_jobs(date_from="2026-01-15")
        self.assertEqual({j["job_uuid"] for j in res["jobs"]}, {"job-mid-jan", "job-feb"})

        # A bare date_to should include that whole day (end-of-day padding).
        res = await self.tool.search_jobs(date_to="2026-01-15")
        self.assertEqual({j["job_uuid"] for j in res["jobs"]}, {"job-jan", "job-mid-jan"})

    async def test_completed_jobs_include_filenames_incomplete_jobs_dont(self):
        history_entry = {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {"7": {"images": [{"filename": "done.png", "subfolder": "", "type": "output"}]}},
        }
        completed_uuid = _seed_job(self.db_path, status="completed", history_entry=history_entry)
        built_uuid = _seed_job(self.db_path, status="built")

        res = await self.tool.search_jobs()
        by_uuid = {j["job_uuid"]: j for j in res["jobs"]}
        self.assertEqual(by_uuid[completed_uuid]["filenames"], ["done.png"])
        self.assertNotIn("filenames", by_uuid[built_uuid])

    async def test_limit_is_capped_by_max_search_results_valve(self):
        for _ in range(5):
            _seed_job(self.db_path)
        self.tool.valves.MAX_SEARCH_RESULTS = 2
        res = await self.tool.search_jobs(limit=10)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 2)

    async def test_combines_multiple_filters_with_and_semantics(self):
        target = _seed_job(
            self.db_path,
            tool="generate_image",
            server="http://s1:8188",
            status="completed",
            positive_prompt="a lighthouse at dusk",
            node_params=[("12", "KSampler", "seed", "42", 42.0)],
        )
        _seed_job(
            self.db_path,
            tool="generate_image",
            server="http://s1:8188",
            status="completed",
            positive_prompt="a lighthouse at dusk",
            node_params=[("12", "KSampler", "seed", "7", 7.0)],  # different seed
        )
        res = await self.tool.search_jobs(
            prompt_text="lighthouse", gpu_server="http://s1:8188", tool="generate_image",
            status=["completed"], seed=42,
        )
        self.assertTrue(res["success"], res)
        self.assertEqual([j["job_uuid"] for j in res["jobs"]], [target])

    async def test_empty_string_sentinels_mean_no_filter(self):
        # A caller whose tool-calling format can't omit a declared parameter may send "" for every
        # argument it doesn't want to set. Before the fix, seed="" reached a raw float(seed) call
        # and failed with a cryptic conversion error, and limit="" or status="" could do the same -
        # this locks in that they now behave exactly like omitting the argument instead.
        _seed_job(self.db_path, node_params=[("12", "KSampler", "seed", "42", 42.0)])
        res = await self.tool.search_jobs(
            prompt_text="", job_search="", checkpoint="", seed="", gpu_server="", tool="",
            status="", date_from="", date_to="", limit="",
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(len(res["jobs"]), 1)  # no filter excluded the one seeded job

    async def test_status_accepts_a_json_string_list(self):
        _seed_job(self.db_path, status="completed")
        _seed_job(self.db_path, status="failed")
        _seed_job(self.db_path, status="built")
        res = await self.tool.search_jobs(status='["completed", "failed"]')
        self.assertTrue(res["success"], res)
        self.assertEqual({j["status"] for j in res["jobs"]}, {"completed", "failed"})


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod._job_db_schema_ready.clear()
        self.tool = mod.Tools()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmpdir) / "jobs.sqlite3")
        self.server = "http://s1:8188"
        self.tool.valves.JOB_DB_PATH = self.db_path
        self.tool.valves.GPU_SERVERS = [self.server]
        self.tool.valves.DEFAULT_GPU_SERVER = self.server
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_db_schema_ready.clear()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def test_retrieve_image_history_empty_string_sentinel_means_omitted(self):
        # history="" (sent by a caller that can't omit a declared parameter) must behave exactly
        # like not passing history at all - it should NOT be treated as "list mode with an invalid
        # count", which would otherwise produce a confusing "history must be a positive integer"
        # error instead of the correct "provide a job id" one.
        res = await self.tool.retrieve_image(job_id_or_filename="", history="")
        self.assertFalse(res["success"])
        self.assertIn("Provide a job id", res["error"])

    @staticmethod
    def _history_entry(graph, comfy_prompt_id="prompt-abc", start_ms=1_000, end_ms=6_000, filename="img.png"):
        return {
            "prompt": [1, comfy_prompt_id, graph, {}, ["7"]],
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [
                    ["execution_start", {"timestamp": start_ms}],
                    ["execution_success", {"timestamp": end_ms}],
                ],
            },
            "outputs": {"7": {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}},
        }

    async def test_backfills_matching_unlinked_job(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 42}}}
        job_uuid = _seed_job(self.db_path, server=self.server, status="built", graph=graph)
        entry = self._history_entry(graph)

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        fake.seed_history(self.server, "prompt-abc", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        self.assertEqual(
            res["reconciliation"], {"matched": True, "job_uuid": job_uuid, "comfy_prompt_id": "prompt-abc"}
        )
        row = self._connect().execute("SELECT * FROM jobs WHERE job_uuid=?", (job_uuid,)).fetchone()
        self.assertEqual(row["comfy_prompt_id"], "prompt-abc")
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["duration_s"])
        results = self._connect().execute("SELECT stage FROM results WHERE job_uuid=?", (job_uuid,)).fetchall()
        self.assertIn("history", [r["stage"] for r in results])

    async def test_no_unlinked_jobs_skips_history_scan_entirely(self):
        _seed_job(self.db_path, server=self.server, status="completed", comfy_prompt_id="already-linked")

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        self.assertNotIn("reconciliation", res)
        self.assertEqual(fake.history_list_requests(self.server), 0)

    async def test_no_graph_match_leaves_job_alone(self):
        job_uuid = _seed_job(self.db_path, server=self.server, status="built", graph={"a": 1})
        entry = self._history_entry({"b": 2})  # different graph

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        fake.seed_history(self.server, "prompt-abc", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        self.assertNotIn("reconciliation", res)
        row = self._connect().execute("SELECT * FROM jobs WHERE job_uuid=?", (job_uuid,)).fetchone()
        self.assertIsNone(row["comfy_prompt_id"])
        self.assertEqual(row["status"], "built")

    async def test_clear_timing_resolves_without_ambiguity(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 42}}}
        close_uuid = _seed_job(
            self.db_path, server=self.server, status="built", graph=graph,
            created_at="1970-01-01T00:00:01+00:00",  # 1s after epoch, close to execution_start below
        )
        _seed_job(
            self.db_path, server=self.server, status="built", graph=graph,
            created_at="1970-01-01T01:00:00+00:00",  # 1 hour away
        )
        entry = self._history_entry(graph, start_ms=1_000)  # execution_start = 1s after epoch

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        fake.seed_history(self.server, "prompt-abc", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        self.assertEqual(res["reconciliation"]["job_uuid"], close_uuid)
        self.assertNotIn("ambiguous", res["reconciliation"])

    async def test_ambiguous_match_within_window_is_flagged(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 42}}}
        uuid_a = _seed_job(
            self.db_path, server=self.server, status="built", graph=graph,
            created_at="1970-01-01T00:00:01+00:00",
        )
        uuid_b = _seed_job(
            self.db_path, server=self.server, status="built", graph=graph,
            created_at="1970-01-01T00:00:03+00:00",  # 2s away from uuid_a - within the 5s window
        )
        entry = self._history_entry(graph, start_ms=2_000)  # equidistant-ish from both

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        fake.seed_history(self.server, "prompt-abc", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        recon = res["reconciliation"]
        self.assertTrue(recon["ambiguous"])
        self.assertIn(recon["job_uuid"], (uuid_a, uuid_b))
        self.assertIn("2 unlinked jobs", recon["note"])

    async def test_no_execution_start_still_links_oldest_but_flags_ambiguous(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 42}}}
        older_uuid = _seed_job(
            self.db_path, server=self.server, status="built", graph=graph,
            created_at="1970-01-01T00:00:01+00:00",
        )
        _seed_job(
            self.db_path, server=self.server, status="built", graph=graph,
            created_at="1970-01-01T00:00:02+00:00",
        )
        entry = {
            "prompt": [1, "prompt-abc", graph, {}, ["7"]],
            "status": {"status_str": "success", "completed": True, "messages": []},  # no execution_start
            "outputs": {"7": {"images": [{"filename": "img.png", "subfolder": "", "type": "output"}]}},
        }

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        fake.seed_history(self.server, "prompt-abc", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        recon = res["reconciliation"]
        self.assertTrue(recon["ambiguous"])
        self.assertEqual(recon["job_uuid"], older_uuid)

    async def test_log_to_sqlite_false_disables_reconciliation(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 42}}}
        job_uuid = _seed_job(self.db_path, server=self.server, status="built", graph=graph)
        entry = self._history_entry(graph)
        self.tool.valves.LOG_TO_SQLITE = False

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        fake.seed_history(self.server, "prompt-abc", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        self.assertNotIn("reconciliation", res)
        row = self._connect().execute("SELECT * FROM jobs WHERE job_uuid=?", (job_uuid,)).fetchone()
        self.assertIsNone(row["comfy_prompt_id"])

    async def test_reconciliation_failure_does_not_break_image_retrieval(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 42}}}
        _seed_job(self.db_path, server=self.server, status="built", graph=graph)
        entry = self._history_entry(graph)

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(self.server, "", "img.png")
        fake.seed_history(self.server, "prompt-abc", entry)
        with patch.object(mod, "requests", fake), patch.object(
            mod, "find_history_entry_by_filename", side_effect=RuntimeError("boom")
        ):
            res = await self.tool.retrieve_image("img.png", gpu_server=self.server)

        self.assertTrue(res["success"], res)
        self.assertTrue(res.get("found"))
        self.assertNotIn("reconciliation", res)

    async def test_reconciliation_is_scoped_to_the_matching_server_only(self):
        server_a, server_b = "http://s1:8188", "http://s2:8188"
        self.tool.valves.GPU_SERVERS = [server_a, server_b]
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 42}}}
        job_uuid = _seed_job(self.db_path, server=server_a, status="built", graph=graph)
        entry = self._history_entry(graph)

        fake = FakeMultiServerRetrieveComfy()
        fake.seed_file(server_b, "", "img.png")
        fake.seed_history(server_b, "prompt-abc", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_image("img.png", gpu_server=server_b)

        self.assertTrue(res["success"], res)
        self.assertNotIn("reconciliation", res)
        row = self._connect().execute("SELECT * FROM jobs WHERE job_uuid=?", (job_uuid,)).fetchone()
        self.assertIsNone(row["comfy_prompt_id"])


class ReconciliationHelperTests(unittest.TestCase):
    """Pure-function tests: no DB, no network."""

    def test_resolve_single_candidate(self):
        res = mod.resolve_reconciliation_match([{"job_uuid": "a", "created_at": "x"}], execution_start_ts_ms=None)
        self.assertEqual(res, {"job_uuid": "a", "ambiguous": False})

    def test_resolve_multiple_with_clear_time_separation(self):
        candidates = [
            {"job_uuid": "close", "created_at": "1970-01-01T00:00:01+00:00"},
            {"job_uuid": "far", "created_at": "1970-01-01T01:00:00+00:00"},
        ]
        res = mod.resolve_reconciliation_match(candidates, execution_start_ts_ms=1_000)
        self.assertEqual(res["job_uuid"], "close")
        self.assertFalse(res["ambiguous"])

    def test_resolve_multiple_within_ambiguity_window(self):
        candidates = [
            {"job_uuid": "a", "created_at": "1970-01-01T00:00:01+00:00"},
            {"job_uuid": "b", "created_at": "1970-01-01T00:00:03+00:00"},
        ]
        res = mod.resolve_reconciliation_match(candidates, execution_start_ts_ms=2_000)
        self.assertTrue(res["ambiguous"])
        self.assertIn(res["job_uuid"], ("a", "b"))

    def test_resolve_multiple_with_no_execution_start_picks_oldest(self):
        candidates = [
            {"job_uuid": "newer", "created_at": "1970-01-01T00:00:02+00:00"},
            {"job_uuid": "older", "created_at": "1970-01-01T00:00:01+00:00"},
        ]
        res = mod.resolve_reconciliation_match(candidates, execution_start_ts_ms=None)
        self.assertEqual(res["job_uuid"], "older")
        self.assertTrue(res["ambiguous"])

    def test_find_history_entry_by_filename(self):
        class StubClient:
            def get_history_list(self, max_items):
                return {
                    "id1": {"outputs": {"7": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}},
                    "id2": {"outputs": {"7": {"images": [{"filename": "b.png", "subfolder": "sub", "type": "output"}]}}},
                }

        found = mod.find_history_entry_by_filename(StubClient(), "sub", "b.png", 50)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "id2")

        self.assertIsNone(mod.find_history_entry_by_filename(StubClient(), "", "missing.png", 50))

    def test_fts_phrase_query_escapes_quotes(self):
        self.assertEqual(mod.fts_phrase_query('a "quoted" word'), '"a ""quoted"" word"')


class ListJobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod._job_db_schema_ready.clear()
        self.tool = mod.Tools()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmpdir) / "jobs.sqlite3")
        self.server = "http://s1:8188"
        self.other_server = "http://s2:8188"
        self.tool.valves.JOB_DB_PATH = self.db_path
        self.tool.valves.GPU_SERVERS = [self.server, self.other_server]
        self.tool.valves.DEFAULT_GPU_SERVER = self.server

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_db_schema_ready.clear()

    @staticmethod
    def _live_entry(number, filename, positive="", negative="", start_ms=1_000):
        graph = {
            "10": {"class_type": "CLIPTextEncode", "inputs": {"text": positive}},
            "11": {"class_type": "CLIPTextEncode", "inputs": {"text": negative}},
            "12": {
                "class_type": "KSampler",
                "inputs": {"positive": ["10", 0], "negative": ["11", 0], "seed": 1, "steps": 20},
            },
        }
        return {
            "prompt": [number, f"prompt-{number}", graph, {}, ["7"]],
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [["execution_start", {"timestamp": start_ms}]],
            },
            "outputs": {"7": {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}},
        }

    async def test_default_live_mode_reads_default_server(self):
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.server, "prompt-1", self._live_entry(1, "a.png", "a cat"))
        fake.seed_history(self.server, "prompt-2", self._live_entry(2, "b.png", "a dog"))
        with patch.object(mod, "requests", fake):
            res = await self.tool.list_jobs()

        self.assertTrue(res["success"], res)
        table = res["markdown_table"]
        self.assertIn("Showing 1-2 of 2", table)
        # newest (highest queue number) first
        lines = [l for l in table.splitlines() if l.startswith("| 1 |") or l.startswith("| 2 |")]
        self.assertIn("prompt-2", lines[0])
        self.assertIn("prompt-1", lines[1])
        self.assertIn(self.server, table)
        self.assertIn("a cat", table)

    async def test_empty_string_sentinels_mean_defaults(self):
        # job_count/skip_to are computed via max(1, ...)/max(0, ...) before anything else in this
        # method; before the fix, a "" sentinel for either (sent by a caller whose format can't
        # omit a declared parameter) reached that max() call as a bare string and raised an
        # unhandled TypeError rather than falling back to the normal default.
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.server, "prompt-1", self._live_entry(1, "a.png"))
        with patch.object(mod, "requests", fake):
            res = await self.tool.list_jobs(
                data_source="", job_count="", skip_to="", job_search="", prompt_search="",
            )
        self.assertTrue(res["success"], res)
        self.assertIn("a.png", res["markdown_table"])

    async def test_live_mode_pagination_skip_to(self):
        fake = FakeMultiServerRetrieveComfy()
        for n in range(1, 6):
            fake.seed_history(self.server, f"prompt-{n}", self._live_entry(n, f"{n}.png"))
        with patch.object(mod, "requests", fake):
            res = await self.tool.list_jobs(job_count=2, skip_to=2)

        self.assertTrue(res["success"], res)
        table = res["markdown_table"]
        self.assertIn("Showing 3-4 of 5", table)
        self.assertIn("prompt-3", table)
        self.assertIn("prompt-2", table)
        self.assertIn("| 3 |", table)
        self.assertIn("| 4 |", table)

    async def test_live_mode_explicit_server_address(self):
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.other_server, "prompt-1", self._live_entry(1, "x.png"))
        with patch.object(mod, "requests", fake):
            res = await self.tool.list_jobs(data_source=self.other_server)

        self.assertTrue(res["success"], res)
        self.assertIn(self.other_server, res["markdown_table"])
        self.assertNotIn(self.server, res["markdown_table"].replace(self.other_server, ""))

    async def test_live_mode_filename_is_linked_by_default_and_omittable(self):
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.server, "prompt-1", self._live_entry(1, "a.png"))
        with patch.object(mod, "requests", fake):
            linked = await self.tool.list_jobs()
            unlinked = await self.tool.list_jobs(link_images=False)

        self.assertIn(f"[a.png]({self.server}/view?filename=a.png", linked["markdown_table"])
        self.assertNotIn("![", linked["markdown_table"])  # a link, never an embedded image
        self.assertIn("**File:** a.png", unlinked["markdown_table"])
        self.assertNotIn("[a.png](", unlinked["markdown_table"])

    async def test_live_mode_no_jobs_found(self):
        fake = FakeMultiServerRetrieveComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.list_jobs()
        self.assertTrue(res["success"], res)
        self.assertEqual(res["markdown_table"], "No jobs found.")

    async def test_database_mode_shows_completed_only(self):
        _seed_job(
            self.db_path, server=self.server, status="completed",
            positive_prompt="a lighthouse",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "done.png", "subfolder": "", "type": "output"}]}}},
        )
        _seed_job(self.db_path, server=self.server, status="built", positive_prompt="a cat")
        _seed_job(self.db_path, server=self.server, status="failed", positive_prompt="a dog")

        res = await self.tool.list_jobs(data_source="database")
        self.assertTrue(res["success"], res)
        table = res["markdown_table"]
        self.assertIn("done.png", table)
        self.assertIn("a lighthouse", table)
        self.assertNotIn("a cat", table)
        self.assertNotIn("a dog", table)
        self.assertIn("Showing 1-1 of 1", table)

    async def test_database_mode_job_search_matches_filename(self):
        _seed_job(
            self.db_path, server=self.server, status="completed", positive_prompt="match me",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "findable.png", "subfolder": "", "type": "output"}]}}},
        )
        _seed_job(
            self.db_path, server=self.server, status="completed", positive_prompt="not this one",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "other.png", "subfolder": "", "type": "output"}]}}},
        )

        res = await self.tool.list_jobs(data_source="database", job_search="findable")
        self.assertTrue(res["success"], res)
        table = res["markdown_table"]
        self.assertIn("findable.png", table)
        self.assertNotIn("other.png", table)

    async def test_database_mode_prompt_search(self):
        _seed_job(
            self.db_path, server=self.server, status="completed", positive_prompt="a lighthouse at dusk",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}},
        )
        _seed_job(
            self.db_path, server=self.server, status="completed", positive_prompt="a cat on a windowsill",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "b.png", "subfolder": "", "type": "output"}]}}},
        )

        res = await self.tool.list_jobs(data_source="database", prompt_search="lighthouse")
        self.assertTrue(res["success"], res)
        table = res["markdown_table"]
        self.assertIn("a.png", table)
        self.assertNotIn("b.png", table)

    async def test_database_mode_no_db_file_yet(self):
        res = await self.tool.list_jobs(data_source="database")
        self.assertTrue(res["success"], res)
        self.assertIn("No job database found", res["markdown_table"])

    async def test_database_mode_pagination_total_count(self):
        for i in range(5):
            _seed_job(
                self.db_path, server=self.server, status="completed", positive_prompt=f"job {i}",
                history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": f"{i}.png", "subfolder": "", "type": "output"}]}}},
            )
        res = await self.tool.list_jobs(data_source="database", job_count=2, skip_to=2)
        self.assertTrue(res["success"], res)
        self.assertIn("Showing 3-4 of 5", res["markdown_table"])

    async def test_pipe_and_newline_in_prompt_do_not_break_table(self):
        _seed_job(
            self.db_path, server=self.server, status="completed",
            positive_prompt="a | vertical bar\nand a newline",
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "weird.png", "subfolder": "", "type": "output"}]}}},
        )
        res = await self.tool.list_jobs(data_source="database")
        self.assertTrue(res["success"], res)
        table = res["markdown_table"]
        # exactly 4 unescaped pipes on the data row (the table's own column separators)
        data_line = [l for l in table.splitlines() if l.startswith("| 1 |")][0]
        self.assertEqual(len(re.findall(r"(?<!\\)\|", data_line)), 5)  # 5 unescaped '|' delimit 4 columns
        self.assertNotIn("\n", data_line)

    async def test_database_mode_type_and_mode_line(self):
        direct_graph = {"15": {"class_type": "CheckpointLoaderSimple", "inputs": {}}, "12": {"class_type": "KSampler", "inputs": {"latent_image": ["13", 0]}}, "13": {"class_type": "EmptyLatentImage", "inputs": {}}}
        graph_graph = {mod.GRAPH_TOOL_MARKER_NODE_ID: {"class_type": "LoadImageOutput", "inputs": {}}}
        _seed_job(
            self.db_path, job_uuid="direct-job", server=self.server, status="completed", graph=direct_graph,
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}},
        )
        _seed_job(
            self.db_path, job_uuid="graph-job", server=self.server, status="completed", graph=graph_graph,
            history_entry={"status": {}, "outputs": {"7": {"images": [{"filename": "b.png", "subfolder": "", "type": "output"}]}}},
        )
        res = await self.tool.list_jobs(data_source="database")
        self.assertTrue(res["success"], res)
        table = res["markdown_table"]
        self.assertIn("**Type:** txt2img, **Mode:** direct", table)
        self.assertIn("**Type:** unknown, **Mode:** graph", table)

    async def test_live_mode_type_and_mode_line(self):
        entry = self._live_entry(1, "a.png")  # built via a KSampler+EmptyLatentImage-free graph in _live_entry
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.server, "prompt-1", entry)
        with patch.object(mod, "requests", fake):
            res = await self.tool.list_jobs()
        self.assertTrue(res["success"], res)
        self.assertIn("**Mode:** direct", res["markdown_table"])  # _live_entry's graph has no marker node


class RetrieveGraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod._job_db_schema_ready.clear()
        self.tool = mod.Tools()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmpdir) / "jobs.sqlite3")
        self.server = "http://s1:8188"
        self.other_server = "http://s2:8188"
        self.tool.valves.JOB_DB_PATH = self.db_path
        self.tool.valves.GPU_SERVERS = [self.server, self.other_server]
        self.tool.valves.DEFAULT_GPU_SERVER = self.server

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_db_schema_ready.clear()

    GRAPH = {mod.GRAPH_TOOL_MARKER_NODE_ID: {"class_type": "LoadImageOutput", "inputs": {}}, "1": {"class_type": "SaveImage", "inputs": {}}}
    JOB_ID = str(uuid.uuid4())

    async def test_live_mode_by_job_id_completed(self):
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.server, self.JOB_ID, {
            "prompt": [1, self.JOB_ID, self.GRAPH, {}, ["1"]],
            "status": {"status_str": "success"},
            "outputs": {},
        })
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_graph(self.JOB_ID)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["graph"], self.GRAPH)
        self.assertEqual(res["job_mode"], "graph")
        self.assertEqual(res["server"], self.server)
        self.assertEqual(res["status"], "completed")

    async def test_live_mode_by_job_id_still_queued(self):
        fake = FakeMultiServerRetrieveComfy()
        fake.set_queue(self.server, pending=[[5, self.JOB_ID, self.GRAPH, {}, []]])
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_graph(self.JOB_ID)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["graph"], self.GRAPH)
        self.assertEqual(res["status"], "pending")

    async def test_live_mode_by_filename(self):
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.server, "prompt-xyz", {
            "prompt": [1, "prompt-xyz", self.GRAPH, {}, ["1"]],
            "status": {"status_str": "success"},
            "outputs": {"1": {"images": [{"filename": "found.png", "subfolder": "", "type": "output"}]}},
        })
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_graph("found.png")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["graph"], self.GRAPH)
        self.assertEqual(res["comfy_prompt_id"], "prompt-xyz")

    async def test_live_mode_not_found(self):
        fake = FakeMultiServerRetrieveComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_graph("nope.png")
        self.assertFalse(res["success"])
        self.assertIn("error", res)

    async def test_live_mode_explicit_server(self):
        fake = FakeMultiServerRetrieveComfy()
        fake.seed_history(self.other_server, self.JOB_ID, {
            "prompt": [1, self.JOB_ID, self.GRAPH, {}, ["1"]],
            "status": {"status_str": "success"},
            "outputs": {},
        })
        with patch.object(mod, "requests", fake):
            res = await self.tool.retrieve_graph(self.JOB_ID, gpu_server=self.other_server)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["server"], self.other_server)

    async def test_database_mode_by_job_uuid(self):
        job_uuid = _seed_job(self.db_path, server=self.server, status="built", graph=self.GRAPH)
        res = await self.tool.retrieve_graph(job_uuid, data_source="database")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["graph"], self.GRAPH)
        self.assertEqual(res["job_mode"], "graph")

    async def test_database_mode_by_comfy_prompt_id(self):
        _seed_job(self.db_path, server=self.server, status="completed", comfy_prompt_id="prompt-abc", graph=self.GRAPH)
        res = await self.tool.retrieve_graph("prompt-abc", data_source="database")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["graph"], self.GRAPH)

    async def test_database_mode_by_filename(self):
        _seed_job(
            self.db_path, server=self.server, status="completed", graph=self.GRAPH,
            history_entry={"status": {}, "outputs": {"1": {"images": [{"filename": "dbfound.png", "subfolder": "", "type": "output"}]}}},
        )
        res = await self.tool.retrieve_graph("dbfound.png", data_source="database")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["graph"], self.GRAPH)

    async def test_database_mode_no_db_file(self):
        res = await self.tool.retrieve_graph("anything", data_source="database")
        self.assertFalse(res["success"])
        self.assertIn("No job database", res["error"])

    async def test_database_mode_not_found(self):
        _seed_job(self.db_path, server=self.server, status="completed", graph=self.GRAPH)
        res = await self.tool.retrieve_graph("does-not-exist", data_source="database")
        self.assertFalse(res["success"])

    async def test_direct_mode_graph_classified_correctly(self):
        direct_graph = {"15": {"class_type": "CheckpointLoaderSimple", "inputs": {}}}
        job_uuid = _seed_job(self.db_path, server=self.server, status="built", graph=direct_graph)
        res = await self.tool.retrieve_graph(job_uuid, data_source="database")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["job_mode"], "direct")


if __name__ == "__main__":
    unittest.main()
