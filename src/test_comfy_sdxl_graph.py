"""
Tests for comfy_sdxl_graph.py. Run with: python -m unittest test_comfy_sdxl_graph -v
(no real ComfyUI server needed - HTTP is faked).
"""

import copy
import json
import tempfile
import shutil
import sqlite3
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import comfy_sdxl_graph as mod

MINIMAL_GRAPH = {
    "15": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "epicrealismXL_pureFix.safetensors"}},
    "10": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["15", 1], "text": "cat"}},
    "11": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["15", 1], "text": "dog"}},
    "13": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "12": {"class_type": "KSampler", "inputs": {"model": ["15", 0], "positive": ["10", 0], "negative": ["11", 0], "latent_image": ["13", 0], "seed": 1, "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
    "14": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["15", 2]}},
    "7": {"class_type": "SaveImage", "inputs": {"images": ["14", 0]}},
}


def fresh_graph():
    """A deep copy: prepare_graph() mutates node['inputs'] dicts in place, and MINIMAL_GRAPH
    is reused across many tests, so a shallow fresh_graph() would leak mutations
    between tests (e.g. an injected filename_prefix surviving into a later, unrelated test)."""
    return copy.deepcopy(MINIMAL_GRAPH)


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
    """One simulated ComfyUI server (single base_url), like the one in test_comfy_sdxl_direct.py."""

    def __init__(self, checkpoints=None, reject_with=None):
        self.checkpoints = checkpoints
        self.reject_with = reject_with
        self.submitted = []
        self.uploads = []
        self.files = set()  # (subfolder, filename)
        self.history = {}

    def get(self, url, timeout=None):
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/object_info/CheckpointLoaderSimple":
            if self.checkpoints is None:
                return FakeResponse(404)
            return FakeResponse(200, {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [self.checkpoints, {}]}}}})
        if parsed.path == "/view":
            key = (query.get("subfolder", [""])[0], query.get("filename", [""])[0])
            if key in self.files:
                return FakeResponse(200, content=b"PNGDATA:" + key[1].encode())
            return FakeResponse(404)
        if parsed.path.startswith("/history/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            entry = self.history.get(job_id)
            return FakeResponse(200, {job_id: entry} if entry else {})
        if parsed.path == "/queue":
            return FakeResponse(200, {"queue_running": [], "queue_pending": []})
        return FakeResponse(404)

    def post(self, url, json=None, timeout=None, headers=None, files=None, data=None, params=None):
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/upload/image":
            name, _content, _ctype = files["image"]
            subfolder = (data or {}).get("subfolder", "")
            self.files.add((subfolder, name))
            self.uploads.append({"filename": name, "subfolder": subfolder, "type": (data or {}).get("type")})
            return FakeResponse(200, {"name": name, "subfolder": subfolder, "type": (data or {}).get("type", "output")})
        if parsed.path == "/prompt":
            if self.reject_with:
                return FakeResponse(400, self.reject_with)
            self.submitted.append(json["prompt"])
            job_id = f"job-{len(self.submitted)}"
            self.history[job_id] = {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"7": {"images": [{"filename": "sdxl_graph_00001_.png", "subfolder": "", "type": "output"}]}},
            }
            self.files.add(("", "sdxl_graph_00001_.png"))
            return FakeResponse(200, {"prompt_id": job_id, "number": len(self.submitted), "node_errors": {}})
        return FakeResponse(404)


class GraphPreparationTests(unittest.TestCase):
    def test_rejects_missing_save_image(self):
        graph = fresh_graph()
        del graph["7"]
        with self.assertRaises(ValueError) as cm:
            mod.prepare_graph(dict(graph), "prefix", None)
        self.assertIn("SaveImage", str(cm.exception))

    def test_rejects_reserved_node_id(self):
        graph = fresh_graph()
        graph[mod.SOURCE_IMAGE_NODE_ID] = {"class_type": "Anything", "inputs": {}}
        with self.assertRaises(ValueError) as cm:
            mod.prepare_graph(graph, "prefix", None)
        self.assertIn("reserved", str(cm.exception))

    def test_injects_placeholder_when_no_source_image(self):
        graph, needs_placeholder = mod.prepare_graph(fresh_graph(), "prefix", None)
        self.assertTrue(needs_placeholder)
        node = graph[mod.SOURCE_IMAGE_NODE_ID]
        self.assertEqual(node["class_type"], "LoadImageOutput")
        self.assertEqual(node["inputs"]["image"], f"{mod.PLACEHOLDER_FILENAME} [output]")

    def test_injects_given_source_image(self):
        graph, needs_placeholder = mod.prepare_graph(fresh_graph(), "prefix", "sub/pic.png")
        self.assertFalse(needs_placeholder)
        self.assertEqual(graph[mod.SOURCE_IMAGE_NODE_ID]["inputs"]["image"], "sub/pic.png [output]")

    def test_default_filename_prefix_applied_only_when_missing(self):
        graph = fresh_graph()
        graph["7"] = {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": "custom"}}
        prepared, _ = mod.prepare_graph(graph, "default_prefix", None)
        self.assertEqual(prepared["7"]["inputs"]["filename_prefix"], "custom")

        graph2 = {k: dict(v, inputs=dict(v["inputs"])) for k, v in MINIMAL_GRAPH.items()}
        prepared2, _ = mod.prepare_graph(graph2, "default_prefix", None)
        self.assertEqual(prepared2["7"]["inputs"]["filename_prefix"], "default_prefix")

    def test_parse_workflow_accepts_json_string_and_dict(self):
        from_str = mod.parse_workflow(json.dumps(MINIMAL_GRAPH))
        from_dict = mod.parse_workflow(fresh_graph())
        self.assertEqual(set(from_str.keys()), set(MINIMAL_GRAPH.keys()))
        self.assertEqual(set(from_dict.keys()), set(MINIMAL_GRAPH.keys()))

    def test_parse_workflow_rejects_garbage(self):
        for bad in ("not json", "[]", "{}", '{"1": {"no_class_type": true}}'):
            with self.assertRaises(ValueError, msg=bad):
                mod.parse_workflow(bad)


class JobDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The SQLite job record for run_workflow: built -> submitted -> completed/failed/rejected."""

    def setUp(self):
        mod.POLL_INTERVAL_SECONDS = 0
        mod._job_db_schema_ready.clear()
        mod._placeholder_ready.clear()
        self.tool = mod.Tools()
        self.tool.valves.SHOW_PROGRESS = False
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False
        self.tool.valves.GPU_SERVERS = ["http://gpu:8188"]
        self.tool.valves.DEFAULT_GPU_SERVER = "http://gpu:8188"
        self.tmpdir = tempfile.mkdtemp()
        self.tool.valves.JOB_DB_PATH = str(Path(self.tmpdir) / "jobs.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_db_schema_ready.clear()

    def _connect(self):
        return sqlite3.connect(self.tool.valves.JOB_DB_PATH)

    async def test_successful_run_writes_full_lifecycle(self):
        fake = FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH))
        self.assertTrue(res["success"], res)

        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["tool"], "run_workflow")
            self.assertEqual(job["comfy_prompt_id"], res["prompt_id"])

            out = conn.execute("SELECT raw_json FROM outputs WHERE job_uuid=?", (job["job_uuid"],)).fetchone()
            graph = json.loads(out[0])
            self.assertIn(mod.SOURCE_IMAGE_NODE_ID, graph)  # the prepared graph, not the raw input

            prompt_row = conn.execute("SELECT positive_prompt, negative_prompt FROM prompts WHERE job_uuid=?", (job["job_uuid"],)).fetchone()
            self.assertEqual(tuple(prompt_row), ("cat", "dog"))  # extracted via KSampler wiring

            n_params = conn.execute("SELECT COUNT(*) FROM node_params WHERE job_uuid=?", (job["job_uuid"],)).fetchone()[0]
            self.assertGreater(n_params, 0)
        finally:
            conn.close()

    async def test_rejected_job_recorded_with_no_prompt_id(self):
        fake = FakeComfy(reject_with={"error": {"message": "bad graph"}, "node_errors": {}})
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH))
        self.assertFalse(res["success"])
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(job["status"], "rejected")
            self.assertIsNone(job["comfy_prompt_id"])
            err = conn.execute("SELECT stage, message FROM errors WHERE job_uuid=?", (job["job_uuid"],)).fetchone()
            self.assertEqual(err[0], "submit")
        finally:
            conn.close()

    async def test_missing_save_image_writes_no_job_row_at_all(self):
        """Validation fails before job_uuid is even generated - nothing to catalogue yet."""
        graph = fresh_graph()
        del graph["7"]
        with patch.object(mod, "requests", FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])):
            res = await self.tool.run_workflow(json.dumps(graph))
        self.assertFalse(res["success"])
        self.assertFalse(Path(self.tool.valves.JOB_DB_PATH).exists())

    async def test_queue_only_does_not_reach_completed(self):
        fake = FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH), queue_only=True)
        self.assertTrue(res["success"], res)
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs WHERE comfy_prompt_id=?", (res["job_id"],)).fetchone()
            self.assertEqual(job["status"], "queued")
        finally:
            conn.close()

    async def test_disabled_writes_nothing(self):
        self.tool.valves.LOG_TO_SQLITE = False
        with patch.object(mod, "requests", FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH))
        self.assertTrue(res["success"], res)
        self.assertFalse(Path(self.tool.valves.JOB_DB_PATH).exists())

    async def test_delivery_failure_after_success_keeps_status_completed(self):
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = True
        self.tool.valves.OPEN_WEBUI_API_KEY = "sk-test"

        class BrokenUploadFake(FakeComfy):
            def post(self, url, **kwargs):
                if url.endswith("/api/v1/files/"):
                    raise RuntimeError("boom")
                return super().post(url, **kwargs)

        with patch.object(mod, "requests", BrokenUploadFake(checkpoints=["epicrealismXL_pureFix.safetensors"])):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH))
        self.assertFalse(res["success"])
        self.assertIn("boom", res["error"])

        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(job["status"], "completed")
        finally:
            conn.close()


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod.POLL_INTERVAL_SECONDS = 0
        mod._placeholder_ready.clear()
        mod._job_logger_cache.clear()
        self.tool = mod.Tools()
        self.tool.valves.SHOW_PROGRESS = False
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False
        self.tool.valves.GPU_SERVERS = ["http://gpu:8188"]
        self.tool.valves.DEFAULT_GPU_SERVER = "http://gpu:8188"
        self.tmpdir = tempfile.mkdtemp()
        self.tool.valves.LOG_BACKEND = "file"
        self.tool.valves.JOB_LOG_PATH = str(Path(self.tmpdir) / "graph_jobs.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_logger_cache.clear()

    def _log_lines(self):
        path = Path(self.tool.valves.JOB_LOG_PATH)
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    async def test_end_to_end_txt2img_uploads_placeholder_and_logs(self):
        fake = FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["images"][0]["filename"], "sdxl_graph_00001_.png")
        self.assertIn("image_markdown", res)  # model_embed default, no emitter

        submitted = fake.submitted[0]
        self.assertIn(mod.SOURCE_IMAGE_NODE_ID, submitted)
        self.assertEqual(submitted[mod.SOURCE_IMAGE_NODE_ID]["inputs"]["image"], f"{mod.PLACEHOLDER_FILENAME} [output]")
        self.assertEqual(submitted["7"]["inputs"]["filename_prefix"], "sdxl_graph")

        # placeholder was actually uploaded (server had nothing, so no /view hit before upload)
        self.assertEqual(len(fake.uploads), 1)
        self.assertEqual(fake.uploads[0]["filename"], mod.PLACEHOLDER_FILENAME)

        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"], "graph_render_complete")
        self.assertEqual(lines[0]["filenames"], ["sdxl_graph_00001_.png"])
        self.assertIn(mod.SOURCE_IMAGE_NODE_ID, lines[0]["workflow"])

    async def test_placeholder_not_reuploaded_on_second_call(self):
        fake = FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH))
            await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH))
        self.assertEqual(len(fake.uploads), 1)  # cached after the first check

    async def test_checkpoint_name_is_fixed_up(self):
        graph = fresh_graph()
        graph["15"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "EpicRealism XL"}}
        fake = FakeComfy(checkpoints=["SDXL/epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(graph))
        self.assertTrue(res["success"], res)
        self.assertEqual(fake.submitted[0]["15"]["inputs"]["ckpt_name"], "SDXL/epicrealismXL_pureFix.safetensors")
        self.assertIn("15", res["checkpoint_notes"])

    async def test_missing_save_image_never_submits(self):
        graph = fresh_graph()
        del graph["7"]
        fake = FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(graph))
        self.assertFalse(res["success"])
        self.assertIn("SaveImage", res["error"])
        self.assertEqual(fake.submitted, [])
        self.assertEqual(fake.uploads, [])  # never even got to the placeholder step

    async def test_invalid_json_is_a_clean_error(self):
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.run_workflow("{not valid json")
        self.assertFalse(res["success"])
        self.assertIn("JSON", res["error"])

    async def test_reserved_node_id_is_a_clean_error(self):
        graph = fresh_graph()
        graph[mod.SOURCE_IMAGE_NODE_ID] = {"class_type": "X", "inputs": {}}
        with patch.object(mod, "requests", FakeComfy()):
            res = await self.tool.run_workflow(json.dumps(graph))
        self.assertFalse(res["success"])
        self.assertIn("reserved", res["error"])

    async def test_queue_only_does_not_wait_and_is_not_logged(self):
        fake = FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH), queue_only=True)
        self.assertTrue(res["success"], res)
        self.assertTrue(res["queued"])
        self.assertIn("job_id", res)
        self.assertEqual(self._log_lines(), [])

    async def test_queue_rejection_is_reported(self):
        fake = FakeComfy(reject_with={"error": {"message": "bad graph"}, "node_errors": {}})
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH), queue_only=True)
        self.assertFalse(res["success"])
        self.assertIn("bad graph", res["error"])

    async def test_return_img_url_adds_link_markdown(self):
        fake = FakeComfy(checkpoints=["epicrealismXL_pureFix.safetensors"])
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(json.dumps(MINIMAL_GRAPH), return_img_url=True)
        self.assertTrue(res["success"], res)
        self.assertIn("link_markdown", res["images"][0])
        self.assertIn("comfy_url", res["images"][0])

    async def test_source_image_across_servers_is_copied_first(self):
        source_files = {("", "earlier.png")}

        class TwoServerFake(FakeComfy):
            def __init__(self):
                super().__init__(checkpoints=["epicrealismXL_pureFix.safetensors"])
                self.source_files = set(source_files)

            def get(self, url, timeout=None):
                if url.startswith("http://source-gpu:8188/view"):
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    key = (q.get("subfolder", [""])[0], q.get("filename", [""])[0])
                    return FakeResponse(200, content=b"SRC:" + key[1].encode()) if key in self.source_files else FakeResponse(404)
                return super().get(url, timeout=timeout)

        fake = TwoServerFake()
        self.tool.valves.GPU_SERVERS = ["http://source-gpu:8188", "http://gpu:8188"]
        with patch.object(mod, "requests", fake):
            res = await self.tool.run_workflow(
                json.dumps(MINIMAL_GRAPH),
                source_image="earlier.png",
                source_server="http://source-gpu:8188",
                gpu_server="http://gpu:8188",
            )
        self.assertTrue(res["success"], res)
        self.assertIn("source_image_note", res)
        self.assertIn(("", "earlier.png"), fake.files)  # landed on the target server
        submitted = fake.submitted[0]
        self.assertEqual(submitted[mod.SOURCE_IMAGE_NODE_ID]["inputs"]["image"], "earlier.png [output]")


if __name__ == "__main__":
    unittest.main()
