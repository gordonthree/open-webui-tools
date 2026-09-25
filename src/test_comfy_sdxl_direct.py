"""
Tests for comfy_sdxl_direct.py. Run with:  python -m unittest test_comfy_sdxl_direct -v
(no ComfyUI server or Open WebUI needed - HTTP is faked).
"""

import asyncio
import datetime
import inspect
import json
import re
import logging
import shutil
import sqlite3
import socket
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import comfy_sdxl_direct as mod

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None


BASE = dict(
    positive_prompt="cat",
    negative_prompt="dog",
    checkpoint_name="ckpt.safetensors",
    seed=1,
    steps=25,
    cfg=5.0,
    sampler_name="dpmpp_2m",
    scheduler="karras",
    denoise=1.0,
    width=1216,
    height=824,
    batch_size=1,
)


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


class FakeComfy:
    """Stands in for the `requests` module."""

    def __init__(self, reject_with=None, history_polls_before_done=1, checkpoints=None, upload_status=200, gate=None):
        self.gate = gate  # threading.Event; history stays empty until it is set
        self.client_ids = []
        self.checkpoints = checkpoints  # None -> object_info unavailable
        self.upload_status = upload_status
        self.uploads = []
        self.submitted = []
        self.reject_with = reject_with
        self.polls_left = history_polls_before_done

    def post(self, url, json=None, timeout=None, headers=None, files=None, params=None):
        if url.endswith("/api/v1/files/"):  # Open WebUI file upload
            self.uploads.append({"url": url, "headers": headers, "files": files, "params": params})
            if self.upload_status != 200:
                return FakeResponse(self.upload_status, {"detail": "nope"})
            return FakeResponse(200, {"id": "file-123"})
        if self.reject_with:
            return FakeResponse(400, self.reject_with)
        self.submitted.append(json["prompt"])
        self.client_ids.append(json.get("client_id"))
        return FakeResponse(200, {"prompt_id": "abcdef123456", "number": 1, "node_errors": {}})

    def get(self, url, timeout=None):
        path = urllib.parse.urlparse(url).path
        if path.startswith("/history/"):
            if self.gate is not None and not self.gate.is_set():
                return FakeResponse(200, {})
            if self.gate is None and self.polls_left > 0:
                self.polls_left -= 1
                return FakeResponse(200, {})
            return FakeResponse(
                200,
                {
                    "abcdef123456": {
                        "status": {"status_str": "success", "completed": True, "messages": []},
                        "outputs": {"7": {"images": [{"filename": "sdxl_simple_00003_.png", "subfolder": "", "type": "output"}]}},
                    }
                },
            )
        if path == "/object_info/CheckpointLoaderSimple":
            if self.checkpoints is None:
                return FakeResponse(404)
            return FakeResponse(200, {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [self.checkpoints, {}]}}}})
        if path == "/view":
            return FakeResponse(200, content=b"\x89PNG-fake")
        return FakeResponse(404)


class FakeMultiServerComfy:
    """
    Like FakeComfy, but keys everything by base URL so a single patched
    `requests` module can stand in for two different ComfyUI servers at once -
    needed to test copying a source image from one server to another.
    """

    def __init__(self):
        self.servers: dict = {}

    def _state(self, url):
        origin = urllib.parse.urlparse(url)
        base = f"{origin.scheme}://{origin.netloc}"
        return base, self.servers.setdefault(base, {"files": set(), "uploads": [], "submitted": [], "history": {}})

    def seed_file(self, server, subfolder, filename):
        _, st = self._state(server)
        st["files"].add((subfolder, filename))

    def get(self, url, timeout=None):
        _base, st = self._state(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/view":
            key = (query.get("subfolder", [""])[0], query.get("filename", [""])[0])
            if key in st["files"]:
                return FakeResponse(200, content=b"PNGDATA:" + key[1].encode())
            return FakeResponse(404)
        if parsed.path == "/object_info/CheckpointLoaderSimple":
            return FakeResponse(200, {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["ckpt.safetensors"], {}]}}}})
        if parsed.path.startswith("/history/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            entry = st["history"].get(job_id)
            return FakeResponse(200, {job_id: entry} if entry else {})
        return FakeResponse(404)

    def post(self, url, json=None, timeout=None, headers=None, files=None, data=None, params=None):
        _base, st = self._state(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/upload/image":
            name, _content, _ctype = files["image"]
            subfolder = (data or {}).get("subfolder", "")
            st["files"].add((subfolder, name))
            st["uploads"].append({"filename": name, "subfolder": subfolder, "type": (data or {}).get("type")})
            return FakeResponse(200, {"name": name, "subfolder": subfolder, "type": (data or {}).get("type", "output")})
        if parsed.path == "/prompt":
            st["submitted"].append(json["prompt"])
            job_id = f"job-{len(st['submitted'])}"
            st["history"][job_id] = {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"7": {"images": [{"filename": "rendered.png", "subfolder": "", "type": "output"}]}},
            }
            st["files"].add(("", "rendered.png"))
            return FakeResponse(200, {"prompt_id": job_id, "number": len(st["submitted"]), "node_errors": {}})
        return FakeResponse(404)


class JobLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod.POLL_INTERVAL_SECONDS = 0
        mod._job_logger_cache.clear()  # each test gets fresh handlers
        self.tool = mod.Tools()
        self.tool.valves.SHOW_PROGRESS = False
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False
        self.tmpdir = tempfile.mkdtemp()
        self.tool.valves.LOG_BACKEND = "file"
        self.tool.valves.JOB_LOG_PATH = str(Path(self.tmpdir) / "jobs.jsonl")

    def tearDown(self):
        for h in list(logging.getLogger("comfy_sdxl_direct.jobs." + "x").handlers):
            pass  # loggers are per-config-hash; nothing named this exactly, just a guard
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_logger_cache.clear()

    def _read_lines(self):
        path = Path(self.tool.valves.JOB_LOG_PATH)
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    async def test_completed_render_is_logged_with_full_parameters(self):
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image(
                "a cat", "blurry", seed=42, steps=30, cfg=6.5, sampler_name="euler", scheduler="normal"
            )
        self.assertTrue(res["success"], res)
        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertEqual(rec["event"], "render_complete")
        self.assertEqual(rec["job_id"], res["prompt_id"])
        self.assertEqual(rec["mode"], "txt2img")
        self.assertEqual(rec["filenames"], ["sdxl_simple_00003_.png"])
        self.assertEqual(rec["params"]["seed"], 42)
        self.assertEqual(rec["params"]["positive_prompt"], "a cat")
        self.assertEqual(rec["params"]["sampler_name"], "euler")
        self.assertIn("timestamp", rec)
        datetime.datetime.fromisoformat(rec["timestamp"])  # parses as real ISO 8601

    async def test_queue_only_is_not_logged(self):
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog", queue_only=True)
        self.assertTrue(res["success"], res)
        self.assertFalse(Path(self.tool.valves.JOB_LOG_PATH).exists())

    async def test_logging_disabled_writes_nothing(self):
        self.tool.valves.LOG_JOBS = False
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog")
        self.assertTrue(res["success"], res)
        self.assertFalse(Path(self.tool.valves.JOB_LOG_PATH).exists())

    async def test_two_renders_append_two_lines_without_duplicate_handlers(self):
        with patch.object(mod, "requests", FakeComfy()):
            await self.tool.generate_image("cat", "dog")
            await self.tool.generate_image("cat", "dog")
        lines = self._read_lines()
        self.assertEqual(len(lines), 2)  # not 1+2=3: no handler duplication across calls

    async def test_broken_log_path_does_not_fail_the_render(self):
        # A path that can't be created (parent is actually a file) should degrade gracefully.
        blocker = Path(self.tmpdir) / "blocker"
        blocker.write_text("x")
        self.tool.valves.JOB_LOG_PATH = str(blocker / "jobs.jsonl")
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog")
        self.assertTrue(res["success"], res)  # the render itself still succeeds

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "syslog sockets are POSIX-only")
    async def test_syslog_backend_delivers_to_a_real_unix_socket(self):
        sock_path = str(Path(self.tmpdir) / "fake-syslog.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(sock_path)
        srv.settimeout(2)
        try:
            self.tool.valves.LOG_BACKEND = "syslog"
            self.tool.valves.SYSLOG_ADDRESS = sock_path
            self.tool.valves.SYSLOG_IDENT = "comfy-test"
            with patch.object(mod, "requests", FakeComfy()):
                res = await self.tool.generate_image("cat", "dog")
            self.assertTrue(res["success"], res)
            data, _ = srv.recvfrom(65536)
            text = data.decode("utf-8", "replace").rstrip("\x00")  # SysLogHandler appends a NUL terminator
            self.assertIn("comfy-test:", text)
            payload = text.split("comfy-test:", 1)[1].strip()
            rec = json.loads(payload[payload.index("{"):])
            self.assertEqual(rec["event"], "render_complete")
        finally:
            srv.close()
            Path(sock_path).unlink(missing_ok=True)

    async def test_unreachable_syslog_falls_back_to_file(self):
        self.tool.valves.LOG_BACKEND = "both"
        self.tool.valves.SYSLOG_ADDRESS = str(Path(self.tmpdir) / "no-such-socket")
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog")
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._read_lines()), 1)  # the file backend still caught it


class SourceServerCopyTests(unittest.IsolatedAsyncioTestCase):
    """generate_image(source_server=...) copying an img2img source across GPU servers."""

    def setUp(self):
        mod.POLL_INTERVAL_SECONDS = 0
        self.tool = mod.Tools()
        self.tool.valves.SHOW_PROGRESS = False
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False  # avoids needing an Open WebUI upload endpoint here

    async def test_copies_source_image_to_the_target_server(self):
        fake = FakeMultiServerComfy()
        source, target = "http://source-gpu:8188", "http://target-gpu:8188"
        fake.seed_file(source, "", "sdxl_simple_00002_.png")
        self.tool.valves.GPU_SERVERS = [source, target]
        self.tool.valves.DEFAULT_GPU_SERVER = target

        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog",
                source_image="sdxl_simple_00002_.png",
                source_server=source,
                gpu_server=target,
                checkpoint_name="ckpt.safetensors",
            )

        self.assertTrue(res["success"], res)
        self.assertEqual(res["mode"], "img2img")
        self.assertIn("source_image_note", res)
        self.assertIn(source, res["source_image_note"])
        self.assertIn(target, res["source_image_note"])

        _, target_state = fake._state(target)
        self.assertEqual(len(target_state["uploads"]), 1)
        self.assertEqual(target_state["uploads"][0], {"filename": "sdxl_simple_00002_.png", "subfolder": "", "type": "output"})
        self.assertIn(("", "sdxl_simple_00002_.png"), target_state["files"])  # actually landed in target's output folder

        submitted = target_state["submitted"][0]
        self.assertEqual(submitted["32"]["inputs"]["image"], "sdxl_simple_00002_.png [output]")
        self.assertEqual(submitted["30"]["inputs"]["pixels"], ["32", 0])  # LoadImageOutput node is untouched

        _, source_state = fake._state(source)
        self.assertEqual(source_state["submitted"], [])  # nothing was ever queued on the source server

    async def test_same_source_and_target_server_skips_the_copy(self):
        fake = FakeMultiServerComfy()
        server = "http://only-gpu:8188"
        fake.seed_file(server, "", "a.png")
        self.tool.valves.GPU_SERVERS = [server]
        self.tool.valves.DEFAULT_GPU_SERVER = server

        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog", source_image="a.png", source_server=server, gpu_server=server,
                checkpoint_name="ckpt.safetensors",
            )

        self.assertTrue(res["success"], res)
        self.assertNotIn("source_image_note", res)
        _, state = fake._state(server)
        self.assertEqual(state["uploads"], [])  # no upload needed - already on the target

    async def test_omitting_source_server_assumes_no_copy_needed(self):
        """Backward compatibility: old callers that never pass source_server still work."""
        fake = FakeMultiServerComfy()
        server = "http://only-gpu:8188"
        fake.seed_file(server, "", "a.png")
        self.tool.valves.GPU_SERVERS = [server]
        self.tool.valves.DEFAULT_GPU_SERVER = server

        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog", source_image="a.png", gpu_server=server, checkpoint_name="ckpt.safetensors"
            )

        self.assertTrue(res["success"], res)
        self.assertNotIn("source_image_note", res)

    async def test_missing_file_on_source_server_fails_without_queuing(self):
        fake = FakeMultiServerComfy()
        source, target = "http://src:8188", "http://tgt:8188"
        self.tool.valves.GPU_SERVERS = [source, target]
        self.tool.valves.DEFAULT_GPU_SERVER = target

        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog", source_image="missing.png", source_server=source, gpu_server=target,
                checkpoint_name="ckpt.safetensors",
            )

        self.assertFalse(res["success"])
        self.assertIn("missing.png", res["error"])
        _, target_state = fake._state(target)
        self.assertEqual(target_state["submitted"], [])  # the target was never asked to render

    async def test_unreachable_source_server_fails_cleanly(self):
        fake = FakeMultiServerComfy()
        target = "http://tgt:8188"
        self.tool.valves.GPU_SERVERS = ["http://127.0.0.1:1", target]
        self.tool.valves.DEFAULT_GPU_SERVER = target

        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog", source_image="a.png", source_server="http://127.0.0.1:1", gpu_server=target
            )
        self.assertFalse(res["success"])
        _, target_state = fake._state(target)
        self.assertEqual(target_state["submitted"], [])


class JobDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The SQLite job record: built -> submitted -> completed/failed/rejected, and queue_only."""

    def setUp(self):
        mod.POLL_INTERVAL_SECONDS = 0
        mod._job_db_schema_ready.clear()
        self.tool = mod.Tools()
        self.tool.valves.SHOW_PROGRESS = False
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False
        self.tool.valves.LOG_JOBS = False  # isolate from the syslog/file log; SQLite tested separately
        self.tmpdir = tempfile.mkdtemp()
        self.tool.valves.JOB_DB_PATH = str(Path(self.tmpdir) / "jobs.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_db_schema_ready.clear()

    def _connect(self):
        import sqlite3
        return sqlite3.connect(self.tool.valves.JOB_DB_PATH)

    def _row(self, job_uuid):
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM jobs WHERE job_uuid = ?", (job_uuid,)).fetchone()
        finally:
            conn.close()

    async def test_successful_render_writes_full_lifecycle(self):
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("a cat", "blurry", seed=42, steps=10)
        self.assertTrue(res["success"], res)

        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            jobs = conn.execute("SELECT * FROM jobs").fetchall()
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["comfy_prompt_id"], res["prompt_id"])
            self.assertEqual(job["tool"], "generate_image")
            self.assertIsNotNone(job["submitted_at"])
            # ComfyUI's fake history has no status.messages timestamps, so timing is legitimately absent
            self.assertIsNone(job["duration_s"])

            job_uuid = job["job_uuid"]
            self.assertTrue(re.match(r"^[0-9a-f-]{36}$", job_uuid))

            inp = conn.execute("SELECT raw_json FROM inputs WHERE job_uuid=?", (job_uuid,)).fetchone()
            self.assertEqual(json.loads(inp[0])["positive_prompt"], "a cat")
            self.assertEqual(json.loads(inp[0])["seed"], 42)

            out = conn.execute("SELECT raw_json FROM outputs WHERE job_uuid=?", (job_uuid,)).fetchone()
            graph = json.loads(out[0])
            self.assertIn("12", graph)  # KSampler node from the fixed graph
            self.assertEqual(graph["12"]["inputs"]["seed"], 42)

            results = conn.execute("SELECT stage FROM results WHERE job_uuid=? ORDER BY id", (job_uuid,)).fetchall()
            self.assertEqual([r[0] for r in results], ["submit_response", "history"])

            self.assertEqual(conn.execute("SELECT COUNT(*) FROM errors WHERE job_uuid=?", (job_uuid,)).fetchone()[0], 0)

            prompt_row = conn.execute("SELECT positive_prompt, negative_prompt FROM prompts WHERE job_uuid=?", (job_uuid,)).fetchone()
            self.assertEqual(tuple(prompt_row), ("a cat", "blurry"))

            params = conn.execute(
                "SELECT param_name, value_num FROM node_params WHERE job_uuid=? AND node_id='12' AND param_name='seed'", (job_uuid,)
            ).fetchone()
            self.assertEqual(params[1], 42.0)
        finally:
            conn.close()

    async def test_queue_only_records_built_and_queued_not_completed(self):
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog", queue_only=True)
        self.assertTrue(res["success"], res)
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs WHERE comfy_prompt_id=?", (res["job_id"],)).fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "queued")
            n_results = conn.execute("SELECT COUNT(*) FROM results WHERE job_uuid=?", (job["job_uuid"],)).fetchone()[0]
            self.assertEqual(n_results, 1)  # submit_response only - never waited for completion
        finally:
            conn.close()

    async def test_rejected_job_is_recorded_with_no_prompt_id(self):
        fake = FakeComfy(reject_with={"error": {"message": "bad graph"}, "node_errors": {}})
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog")
        self.assertFalse(res["success"])

        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(job["status"], "rejected")
            self.assertIsNone(job["comfy_prompt_id"])
            err = conn.execute("SELECT stage, message FROM errors WHERE job_uuid=?", (job["job_uuid"],)).fetchone()
            self.assertEqual(err[0], "submit")
            self.assertIn("bad graph", err[1])
            # inputs/outputs were still written before submission was attempted
            self.assertIsNotNone(conn.execute("SELECT 1 FROM inputs WHERE job_uuid=?", (job["job_uuid"],)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM outputs WHERE job_uuid=?", (job["job_uuid"],)).fetchone())
        finally:
            conn.close()

    async def test_timeout_is_recorded_as_failed_with_prompt_id_known(self):
        self.tool.valves.MAX_WAIT_SECONDS = 0
        fake = FakeComfy(history_polls_before_done=10**6)
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog")
        self.assertFalse(res["success"])

        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertIsNotNone(job["comfy_prompt_id"])  # the server DID accept it before we gave up waiting
            err = conn.execute("SELECT stage, message FROM errors WHERE job_uuid=?", (job["job_uuid"],)).fetchone()
            self.assertEqual(err[0], "wait")
            self.assertIn("Timed out", err[1])
        finally:
            conn.close()

    async def test_disabled_writes_nothing(self):
        self.tool.valves.LOG_TO_SQLITE = False
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog")
        self.assertTrue(res["success"], res)
        self.assertFalse(Path(self.tool.valves.JOB_DB_PATH).exists())

    async def test_two_jobs_get_distinct_uuids_and_schema_created_once(self):
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            await self.tool.generate_image("cat", "dog")
            await self.tool.generate_image("cat", "dog")
        conn = self._connect()
        try:
            rows = conn.execute("SELECT job_uuid FROM jobs").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0][0], rows[1][0])
        finally:
            conn.close()

    async def test_delivery_failure_after_successful_render_keeps_status_completed(self):
        """A crash in chat delivery (after the render itself succeeded) must not mark
        an already-completed job as failed - the image really was rendered."""
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = True
        self.tool.valves.OPEN_WEBUI_API_KEY = "sk-test"

        class BrokenUploadFake(FakeComfy):
            def post(self, url, **kwargs):
                if url.endswith("/api/v1/files/"):
                    raise RuntimeError("boom")
                return super().post(url, **kwargs)

        with patch.object(mod, "requests", BrokenUploadFake()):
            res = await self.tool.generate_image("cat", "dog")
        self.assertFalse(res["success"])  # the unexpected error still surfaces to the caller
        self.assertIn("boom", res["error"])

        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(job["status"], "completed")  # NOT overwritten to 'failed'
            self.assertIsNotNone(job["comfy_prompt_id"])
        finally:
            conn.close()


        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            await self.tool.generate_image("a lighthouse at dusk", "blurry")
            await self.tool.generate_image("a cat in a hat", "blurry")
        conn = self._connect()
        try:
            self.assertTrue(mod.has_prompts_fts(conn))
            hits = conn.execute("SELECT job_uuid FROM prompts_fts WHERE prompts_fts MATCH 'lighthouse'").fetchall()
            self.assertEqual(len(hits), 1)
        finally:
            conn.close()


class WorkflowTests(unittest.TestCase):
    def test_txt2img_wiring(self):
        wf = mod.build_workflow(**BASE)
        self.assertEqual(wf["12"]["inputs"]["latent_image"], ["13", 0])
        self.assertEqual(wf["12"]["inputs"]["model"], ["15", 0])
        self.assertEqual(wf["10"]["inputs"]["clip"], ["15", 1])
        self.assertEqual(wf["12"]["inputs"]["denoise"], 1.0)
        self.assertEqual(wf["10"]["inputs"]["text"], "cat")
        self.assertEqual(wf["11"]["inputs"]["text"], "dog")
        self.assertIn("filename_prefix", wf["7"]["inputs"])
        for absent in ("27", "30", "32", "35", "36"):
            self.assertNotIn(absent, wf)

    def test_img2img_wiring(self):
        wf = mod.build_workflow(**{**BASE, "denoise": 0.45}, source_image="sdxl_simple_00002_.png")
        self.assertEqual(wf["32"]["class_type"], "LoadImageOutput")
        self.assertEqual(wf["32"]["inputs"]["image"], "sdxl_simple_00002_.png [output]")
        self.assertEqual(wf["30"]["inputs"]["pixels"], ["32", 0])
        self.assertEqual(wf["12"]["inputs"]["latent_image"], ["30", 0])
        self.assertEqual(wf["12"]["inputs"]["denoise"], 0.45)
        self.assertNotIn("13", wf)
        self.assertNotIn("36", wf)

    def test_img2img_batch_uses_repeat_latent(self):
        wf = mod.build_workflow(**{**BASE, "batch_size": 3}, source_image="a.png")
        self.assertEqual(wf["36"]["class_type"], "RepeatLatentBatch")
        self.assertEqual(wf["36"]["inputs"], {"samples": ["30", 0], "amount": 3})
        self.assertEqual(wf["12"]["inputs"]["latent_image"], ["36", 0])
        self.assertNotIn("batch_size", wf["30"]["inputs"])

    def test_loras_route_through_node_27(self):
        wf = mod.build_workflow(**BASE, loras=[{"lora": "test.safetensors", "strength": 0.7}, {"lora": "b.safetensors"}])
        n27 = wf["27"]["inputs"]
        self.assertEqual(n27["model"], ["15", 0])
        self.assertEqual(n27["clip"], ["15", 1])
        self.assertEqual(n27["lora_1"], {"on": True, "lora": "test.safetensors", "strength": 0.7, "strengthTwo": None})
        self.assertEqual(n27["lora_2"]["strength"], 1.0)
        self.assertEqual(wf["12"]["inputs"]["model"], ["27", 0])
        self.assertEqual(wf["10"]["inputs"]["clip"], ["27", 1])
        self.assertEqual(wf["11"]["inputs"]["clip"], ["27", 1])

    def test_lora_missing_name(self):
        with self.assertRaises(ValueError):
            mod.build_workflow(**BASE, loras=[{"strength": 1}])

    def test_every_link_points_at_an_existing_node(self):
        for kwargs in ({}, {"source_image": "a.png", "batch_size": 2}, {"loras": [{"lora": "x"}]}):
            wf = mod.build_workflow(**{**BASE, **kwargs})
            for nid, node in wf.items():
                for val in node["inputs"].values():
                    if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                        self.assertIn(val[0], wf, f"node {nid} links to missing node {val[0]}")


class NormalizeTests(unittest.TestCase):
    def test_variants(self):
        n = mod.normalize_source_image
        self.assertEqual(n("a.png"), "a.png [output]")
        self.assertEqual(n("a.png [output]"), "a.png [output]")
        self.assertEqual(n("sub/a.png"), "sub/a.png [output]")
        self.assertEqual(
            n("http://h:8188/view?filename=a.png&subfolder=sub&type=output"), "sub/a.png [output]"
        )
        self.assertEqual(n("![x](http://h:8188/view?filename=a.png&subfolder=&type=output)"), "a.png [output]")

    def test_rejects(self):
        for bad in ("", "../etc/passwd", "a.png [input]", "http://h/view?filename=a.png&type=temp"):
            with self.assertRaises(ValueError, msg=bad):
                mod.normalize_source_image(bad)



class ProgressMessageTests(unittest.TestCase):
    def ev(self, type_, **data):
        return {"type": type_, "data": data}

    def test_stage_labels(self):
        st = {}
        self.assertEqual(mod.progress_message(self.ev("executing", node="15", prompt_id="p"), st, "p"), "Loading checkpoint...")
        self.assertEqual(mod.progress_message(self.ev("executing", node="14", prompt_id="p"), st, "p"), "Decoding image...")
        self.assertIsNone(mod.progress_message(self.ev("executing", node="999", prompt_id="p"), st, "p"))
        self.assertIsNone(mod.progress_message(self.ev("executing", node=None, prompt_id="p"), st, "p"))  # end marker

    def test_sampler_progress_and_percent(self):
        st = {}
        mod.progress_message(self.ev("executing", node="12", prompt_id="p"), st, "p")
        # older ComfyUI progress events carry no node id: label comes from the last 'executing'
        msg = mod.progress_message(self.ev("progress", value=12, max=25, prompt_id="p"), st, "p", now=100.0)
        self.assertEqual(msg, "Sampling: step 12/25 (48%)")

    def test_progress_is_throttled_but_last_step_always_shows(self):
        st = {}
        first = mod.progress_message(self.ev("progress", value=1, max=25, node="12"), st, now=100.0)
        self.assertIsNotNone(first)
        self.assertIsNone(mod.progress_message(self.ev("progress", value=2, max=25, node="12"), st, now=100.1))
        self.assertIsNotNone(mod.progress_message(self.ev("progress", value=3, max=25, node="12"), st, now=100.5))
        self.assertEqual(
            mod.progress_message(self.ev("progress", value=25, max=25, node="12"), st, now=100.51),
            "Sampling: step 25/25 (100%)",
        )

    def test_other_prompts_are_ignored(self):
        self.assertIsNone(mod.progress_message(self.ev("progress", value=5, max=25, prompt_id="other"), {}, "mine"))

    def test_queue_position_only_before_start(self):
        st = {}
        busy = self.ev("status", status={"exec_info": {"queue_remaining": 3}})
        self.assertEqual(mod.progress_message(busy, st), "Queued: 2 job(s) ahead of this one")
        self.assertIsNone(mod.progress_message(self.ev("status", status={"exec_info": {"queue_remaining": 1}}), st))
        mod.progress_message(self.ev("execution_start", prompt_id="p"), st, "p")
        self.assertIsNone(mod.progress_message(busy, st))

    def test_garbage_events_are_harmless(self):
        for e in ({}, {"type": "progress", "data": {"value": "x", "max": 0}}, {"type": "unknown"}, {"type": "status", "data": None}):
            self.assertIsNone(mod.progress_message(e, {}))

class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod.POLL_INTERVAL_SECONDS = 0
        self.tool = mod.Tools()
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False  # individual tests turn it on
        self.tool.valves.SHOW_PROGRESS = False  # avoids websocket connect attempts; progress tests enable it
        self.events = []

    async def emitter(self, payload):
        self.events.append(payload)

    def test_only_generate_image_is_exposed(self):
        # Mirrors how Open WebUI discovers tools on the class.
        exposed = [
            n
            for n in dir(self.tool)
            if callable(getattr(self.tool, n)) and not n.startswith("__") and not inspect.isclass(getattr(self.tool, n))
        ]
        self.assertEqual(exposed, ["generate_image"])

    async def test_txt2img_end_to_end(self):
        self.tool.valves.IMAGE_DELIVERY = "message_event"
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog", width=1000, height=700, denoise=0.9, __event_emitter__=self.emitter)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["mode"], "txt2img")
        self.assertEqual(res["images"][0]["use_as_source_image"], "sdxl_simple_00003_.png")
        self.assertEqual(res["params"]["width"], 1000)  # multiple of 8
        self.assertEqual(res["params"]["height"], 704)  # 700 snapped to 704
        sent = fake.submitted[0]
        self.assertEqual(sent["12"]["inputs"]["denoise"], 0.9)
        self.assertEqual(sent["13"]["inputs"]["height"], 704)
        msgs = [e for e in self.events if e["type"] == "message"]
        self.assertEqual(len(msgs), 1)
        self.assertIn(
            "![generated image](http://192.168.10.11:8188/view?filename=sdxl_simple_00003_.png&subfolder=&type=output)",
            msgs[0]["data"]["content"],
        )
        self.assertNotIn("files", [e["type"] for e in self.events])
        self.assertEqual(self.events[-1]["data"], {"description": "Done", "done": True})
        self.assertNotIn("image_markdown", res)

    async def test_img2img_defaults_denoise_and_uses_same_server(self):
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog", source_image="sdxl_simple_00002_.png", gpu_server="http://192.168.10.12:8188"
            )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["mode"], "img2img")
        self.assertEqual(res["server"], "http://192.168.10.12:8188")
        self.assertEqual(fake.submitted[0]["12"]["inputs"]["denoise"], mod.DEFAULT_IMG2IMG_DENOISE)
        self.assertEqual(fake.submitted[0]["32"]["inputs"]["image"], "sdxl_simple_00002_.png [output]")

    async def test_placeholder_strings_mean_unset(self):
        """Replays a real call where the model sent 'default'/null-ish strings."""
        self.tool.valves.GPU_SERVERS = ["http://100.64.219.107:8188", "http://100.95.85.50:8188"]
        self.tool.valves.DEFAULT_GPU_SERVER = "http://100.95.85.50:8188"
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog", width=1536, height=630, seed=-1, batch_size=1, steps=30,
                sampler_name="dpmpp_2m", scheduler="karras", cfg=7, denoise=1,
                checkpoint_name="default", loras=[], gpu_server="default",
                source_image="null", verbose=True,
            )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["server"], "http://100.95.85.50:8188")
        self.assertEqual(res["mode"], "txt2img")
        self.assertEqual(fake.submitted[0]["15"]["inputs"]["ckpt_name"], self.tool.valves.DEFAULT_CHECKPOINT)
        self.assertNotIn("32", fake.submitted[0])

    async def test_empty_string_sentinels_for_args_with_real_defaults(self):
        # A caller whose tool-calling format can't omit a declared parameter (e.g. an OpenAI-style
        # "strict" function schema) may send "" for every argument it doesn't actually want to
        # set - including ones typed as plain int/float/str with a real (non-None) Python default,
        # not just the Optional[str] ones. Before the fix, several of these reached a numeric
        # comparison as a bare string and raised an unhandled TypeError instead of using their
        # default; this locks in that they now behave exactly like omitting the argument.
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image(
                "cat", "dog",
                batch_size="", seed="", steps="", cfg="", denoise="",
                sampler_name="", scheduler="", width="", height="",
                checkpoint_name="", source_image="", source_server="", loras="", gpu_server="",
            )
        self.assertTrue(res["success"], res)
        sent = fake.submitted[0]["12"]["inputs"]
        self.assertEqual(sent["steps"], 25)
        self.assertEqual(sent["cfg"], 5.0)
        self.assertEqual(sent["sampler_name"], "dpmpp_2m")
        self.assertEqual(sent["scheduler"], "karras")
        self.assertEqual(sent["denoise"], mod.DEFAULT_TXT2IMG_DENOISE)
        self.assertEqual(fake.submitted[0]["13"]["inputs"]["width"], 1216)
        self.assertEqual(fake.submitted[0]["13"]["inputs"]["height"], 824)
        self.assertNotIn("27", fake.submitted[0])  # no LoRA node built

    async def test_loras_empty_json_container_forms_all_mean_no_loras(self):
        for empty_loras in ("", "{}", "[]", [], {}):
            fake = FakeComfy()
            with patch.object(mod, "requests", fake):
                res = await self.tool.generate_image("cat", "dog", loras=empty_loras)
            self.assertTrue(res["success"], res)
            self.assertNotIn("27", fake.submitted[0], f"loras={empty_loras!r} should mean no LoRAs")

    def test_resolve_server(self):
        allowed = ["http://100.64.219.107:8188", "http://100.95.85.50:8188"]
        d = "http://100.95.85.50:8188"
        for unset in (None, "", "default", "DEFAULT", " null "):
            self.assertEqual(mod.resolve_server(unset, d, allowed), d)
        self.assertEqual(mod.resolve_server("http://100.64.219.107:8188/", d, allowed), allowed[0])
        self.assertEqual(mod.resolve_server("100.64.219.107", d, allowed), allowed[0])
        self.assertEqual(mod.resolve_server("100.64.219.107:8188", d, allowed), allowed[0])
        with self.assertRaises(ValueError):
            mod.resolve_server("http://evil:8188", d, allowed)
        with self.assertRaises(ValueError):
            mod.resolve_server("100.64.219.107:9999", d, allowed)

    async def test_checkpoint_resolved_when_folder_differs(self):
        fake = FakeComfy(checkpoints=["SDXL/epicrealismXL_pureFix.safetensors", "other.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog")
        self.assertTrue(res["success"], res)
        self.assertEqual(fake.submitted[0]["15"]["inputs"]["ckpt_name"], "SDXL/epicrealismXL_pureFix.safetensors")
        self.assertEqual(res["params"]["checkpoint"], "SDXL/epicrealismXL_pureFix.safetensors")
        self.assertIn("checkpoint_note", res)

    async def test_checkpoint_missing_lists_available_and_does_not_submit(self):
        fake = FakeComfy(checkpoints=["a.safetensors", "b.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog")
        self.assertFalse(res["success"])
        self.assertIn("a.safetensors", res["error"])
        self.assertIn("DEFAULT_CHECKPOINT", res["error"])
        self.assertEqual(fake.submitted, [])

    async def test_exact_checkpoint_has_no_note(self):
        fake = FakeComfy(checkpoints=["checkpoints/epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog")
        self.assertTrue(res["success"], res)
        self.assertNotIn("checkpoint_note", res)

    def test_resolve_checkpoint(self):
        r = mod.resolve_checkpoint
        avail = ["SDXL/epicrealismXL_pureFix.safetensors", "sd15/dreamshaper_8.safetensors", "juggernautXL_v9.safetensors"]
        self.assertEqual(r("juggernautXL_v9.safetensors", avail), "juggernautXL_v9.safetensors")
        self.assertEqual(r("checkpoints/epicrealismXL_pureFix.safetensors", avail), avail[0])
        self.assertEqual(r("SDXL\\EPICREALISMXL_PUREFIX.safetensors", avail), avail[0])
        self.assertEqual(r("EpicRealism XL", avail), avail[0])
        with self.assertRaises(ValueError) as cm:
            r("nonexistent.safetensors", avail)
        self.assertIn("dreamshaper_8", str(cm.exception))
        with self.assertRaises(ValueError) as cm:  # same file name in two folders
            r("x/a.safetensors", ["one/a.safetensors", "two/a.safetensors"])
        self.assertIn("ambiguous", str(cm.exception))

    async def test_lookup_failure_is_reported_in_error(self):
        body = {"error": {"message": "Prompt outputs failed validation"},
                "node_errors": {"15": {"class_type": "CheckpointLoaderSimple", "errors": [{"message": "Value not in list", "details": "ckpt_name: 'x' not in (list of length 58)"}]}}}
        with patch.object(mod, "requests", FakeComfy(reject_with=body, checkpoints=None)):
            res = await self.tool.generate_image("cat", "dog")
        self.assertFalse(res["success"])
        self.assertIn("list of length 58", res["error"])
        self.assertIn("could not be fetched", res["error"])
        self.assertIn("HTTP 404", res["error"])

    async def test_no_lookup_note_when_lookup_worked(self):
        body = {"error": {"message": "Prompt outputs failed validation"}, "node_errors": {}}
        fake = FakeComfy(reject_with=body, checkpoints=["checkpoints/epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog")
        self.assertFalse(res["success"])
        self.assertNotIn("could not be fetched", res["error"])

    async def test_no_emit_returns_markdown(self):
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog")
        self.assertIn("image_markdown", res)

    async def test_files_mode_attaches_image(self):
        self.tool.valves.IMAGE_DELIVERY = "files_event"
        with patch.object(mod, "requests", FakeComfy()):
            await self.tool.generate_image("cat", "dog", __event_emitter__=self.emitter)
        files = [e for e in self.events if e["type"] == "files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["data"]["files"][0]["type"], "image")
        self.assertTrue(files[0]["data"]["files"][0]["url"].startswith("http://"))
        self.assertNotIn("message", [e["type"] for e in self.events])

    async def test_upload_to_open_webui(self):
        v = self.tool.valves
        v.UPLOAD_TO_OPEN_WEBUI, v.OPEN_WEBUI_API_KEY, v.OPEN_WEBUI_BASE_URL = True, "sk-test", "http://localhost:8080/"
        v.IMAGE_DELIVERY = "message_event"
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog", return_img_url=True, __event_emitter__=self.emitter)
        self.assertTrue(res["success"], res)
        self.assertEqual(len(fake.uploads), 1)
        self.assertEqual(fake.uploads[0]["url"], "http://localhost:8080/api/v1/files/")
        self.assertEqual(fake.uploads[0]["headers"], {"Authorization": "Bearer sk-test"})
        self.assertEqual(fake.uploads[0]["params"], {"process": "false"})
        name, payload, ctype = fake.uploads[0]["files"]["file"]
        self.assertEqual((name, payload, ctype), ("sdxl_simple_00003_.png", b"\x89PNG-fake", "image/png"))
        img = res["images"][0]
        self.assertEqual(img["chat_url"], "/api/v1/files/file-123/content")
        self.assertEqual(img["comfy_url"], "http://192.168.10.11:8188/view?filename=sdxl_simple_00003_.png&subfolder=&type=output")
        self.assertEqual(img["delivery"], "openwebui_upload")
        self.assertEqual(img["use_as_source_image"], "sdxl_simple_00003_.png")  # still the ComfyUI name
        self.assertNotIn("warnings", res)
        msg = [e for e in self.events if e["type"] == "message"][0]
        self.assertIn("![generated image](/api/v1/files/file-123/content)", msg["data"]["content"])
        self.assertNotIn("100.", msg["data"]["content"])

    async def test_upload_failure_falls_back_to_direct_link(self):
        v = self.tool.valves
        v.UPLOAD_TO_OPEN_WEBUI, v.OPEN_WEBUI_API_KEY = True, "sk-test"
        with patch.object(mod, "requests", FakeComfy(upload_status=401)):
            res = await self.tool.generate_image("cat", "dog", return_img_url=True)
        self.assertTrue(res["success"], res)  # the render itself worked
        self.assertEqual(res["images"][0]["delivery"], "direct_comfy_url")
        self.assertTrue(res["images"][0]["chat_url"].startswith("http://"))
        self.assertIn("upload failed", res["warnings"][0])
        self.assertIn("401", res["warnings"][0])

    async def test_missing_api_key_is_explained(self):
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = True  # key left empty
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog")
        self.assertTrue(res["success"], res)
        self.assertEqual(fake.uploads, [])
        self.assertIn("OPEN_WEBUI_API_KEY", res["warnings"][0])

    async def test_model_result_carries_no_image_bytes(self):
        v = self.tool.valves
        v.UPLOAD_TO_OPEN_WEBUI, v.OPEN_WEBUI_API_KEY = True, "sk-test"
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog")
        self.assertNotIn("base64", json.dumps(res))
        self.assertEqual(res["image_markdown"], "![generated image](/api/v1/files/file-123/content)")

    async def test_model_embed_is_the_default_and_does_not_emit(self):
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog", __event_emitter__=self.emitter)
        self.assertEqual(self.tool.valves.IMAGE_DELIVERY, "model_embed")
        self.assertEqual(res["displayed_via"], "model_embed")
        self.assertNotIn("message", [e["type"] for e in self.events])
        self.assertNotIn("files", [e["type"] for e in self.events])
        self.assertTrue(res["image_markdown"].startswith("![generated image]("))
        self.assertIn("Include image_markdown", res["note"])
        self.assertNotIn("don't embed", res["note"])

    async def _ws_server(self, script, gate, settle=0.15):
        """A tiny aiohttp server that behaves like ComfyUI's /ws endpoint."""
        seen = {}

        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            seen["client_id"] = request.query.get("clientId")
            for ev in script:
                await ws.send_str(json.dumps(ev))
                await asyncio.sleep(0.02)
            await asyncio.sleep(settle)  # let the client drain before history reports done
            gate.set()
            await asyncio.sleep(0.3)
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/ws", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}", seen

    @unittest.skipIf(web is None, "aiohttp not installed")
    async def test_live_progress_reaches_the_ui(self):
        script = [
            {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}},
            {"type": "execution_start", "data": {"prompt_id": "abcdef123456"}},
            {"type": "executing", "data": {"node": "15", "prompt_id": "abcdef123456"}},
            {"type": "executing", "data": {"node": "12", "prompt_id": "abcdef123456"}},
            {"type": "progress", "data": {"value": 10, "max": 25, "prompt_id": "abcdef123456", "node": "12"}},
            {"type": "progress", "data": {"value": 25, "max": 25, "prompt_id": "abcdef123456", "node": "12"}},
            {"type": "executing", "data": {"node": "14", "prompt_id": "abcdef123456"}},
            {"type": "executing", "data": {"node": None, "prompt_id": "abcdef123456"}},
        ]
        gate = threading.Event()
        runner, url, seen = await self._ws_server(script, gate)
        try:
            mod.POLL_INTERVAL_SECONDS = 0.01
            self.tool.valves.SHOW_PROGRESS = True
            self.tool.valves.GPU_SERVERS = [url]
            self.tool.valves.DEFAULT_GPU_SERVER = url
            fake = FakeComfy(gate=gate)
            with patch.object(mod, "requests", fake):
                res = await self.tool.generate_image("cat", "dog", __event_emitter__=self.emitter)
        finally:
            await runner.cleanup()
        self.assertTrue(res["success"], res)
        texts = [e["data"]["description"] for e in self.events if e["type"] == "status"]
        for expected in ("Loading checkpoint...", "Sampling...", "Sampling: step 25/25 (100%)", "Decoding image..."):
            self.assertIn(expected, texts)
        # the socket must be opened with the same client id the prompt was queued with
        self.assertEqual(seen["client_id"], fake.client_ids[0])
        # the final status is still 'Done', i.e. no late progress line overwrote it
        self.assertEqual(self.events[-1]["data"], {"description": "Done", "done": True})

    async def test_progress_falls_back_silently_when_websocket_is_down(self):
        url = "http://127.0.0.1:1"  # connection refused
        self.tool.valves.SHOW_PROGRESS = True
        self.tool.valves.GPU_SERVERS = [url]
        self.tool.valves.DEFAULT_GPU_SERVER = url
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog", __event_emitter__=self.emitter)
        self.assertTrue(res["success"], res)
        texts = [e["data"]["description"] for e in self.events if e["type"] == "status"]
        self.assertFalse(any("step" in t for t in texts))
        self.assertEqual(texts[-1], "Done")

    async def test_urls_only_returned_on_request(self):
        with patch.object(mod, "requests", FakeComfy()):
            plain = await self.tool.generate_image("cat", "dog")
        for key in ("url", "chat_url", "comfy_url"):
            self.assertNotIn(key, plain["images"][0])
        with patch.object(mod, "requests", FakeComfy()):
            with_urls = await self.tool.generate_image("cat", "dog", return_img_url=True)
        self.assertIn("comfy_url", with_urls["images"][0])
        self.assertIn("chat_url", with_urls["images"][0])
        self.assertEqual(
            with_urls["images"][0]["link_markdown"],
            "[sdxl_simple_00003_.png](" + with_urls["images"][0]["chat_url"] + ")",
        )
        self.assertIn("link_markdown", with_urls["note"])
        self.assertIn("backticks", with_urls["note"])
        self.assertNotIn("link_markdown", plain["images"][0])
        self.assertNotIn("backticks", plain["note"])

    async def test_no_event_emitter_means_model_must_embed(self):
        self.tool.valves.IMAGE_DELIVERY = "message_event"
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog")  # no emitter passed
        self.assertEqual(res["displayed_via"], "none")
        self.assertIn("image_markdown", res)
        self.assertIn("image_markdown", res["note"])

    async def test_displayed_via_reports_event_type(self):
        self.tool.valves.IMAGE_DELIVERY = "message_event"
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.generate_image("cat", "dog", __event_emitter__=self.emitter)
        self.assertEqual(res["displayed_via"], "message_event")
        self.assertNotIn("image_markdown", res)

    async def test_bad_arguments(self):
        with patch.object(mod, "requests", FakeComfy()):
            for kwargs in ({"denoise": 1.5}, {"batch_size": 99}, {"gpu_server": "http://"}, {"steps": 0}):
                res = await self.tool.generate_image("cat", "dog", **kwargs)
                self.assertFalse(res["success"], kwargs)

    async def test_node_errors_are_surfaced(self):
        body = {
            "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"},
            "node_errors": {"12": {"class_type": "KSampler", "errors": [{"message": "Value not in list", "details": "sampler_name: 'nope'"}]}},
        }
        with patch.object(mod, "requests", FakeComfy(reject_with=body)):
            res = await self.tool.generate_image("cat", "dog", sampler_name="nope")
        self.assertFalse(res["success"])
        self.assertIn("KSampler", res["error"])
        self.assertIn("nope", res["error"])

    async def test_loras_passed_as_json_string(self):
        fake = FakeComfy()
        with patch.object(mod, "requests", fake):
            res = await self.tool.generate_image("cat", "dog", loras='[{"lora": "x.safetensors", "strength": 0.5}]')
        self.assertTrue(res["success"], res)
        self.assertEqual(fake.submitted[0]["27"]["inputs"]["lora_1"]["strength"], 0.5)

    async def test_timeout(self):
        self.tool.valves.MAX_WAIT_SECONDS = 0
        with patch.object(mod, "requests", FakeComfy(history_polls_before_done=10**6)):
            res = await self.tool.generate_image("cat", "dog")
        self.assertFalse(res["success"])
        self.assertIn("Timed out", res["error"])


if __name__ == "__main__":
    unittest.main()
