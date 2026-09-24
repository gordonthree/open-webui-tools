"""
Tests for import_server_history.py. Run with:  python -m unittest test_import_server_history -v
(no ComfyUI server needed - HTTP is faked).
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import import_server_history as mod
import job_db_common as jdc

SERVER = "http://s1:8188"


def _entry(prompt_id, graph, filename="x.png", status_str="success", start_ms=None, end_ms=None):
    messages = []
    if start_ms is not None:
        messages.append(["execution_start", {"timestamp": start_ms}])
    if end_ms is not None:
        messages.append(["execution_success", {"timestamp": end_ms}])
    return {
        "prompt": [1, prompt_id, graph, {}, []],
        "status": {"status_str": status_str, "messages": messages},
        "outputs": {"7": {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}} if status_str == "success" else {},
    }


def _fake_response(history):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = history
    return resp


def _seed_unlinked(db_path, server, graph, created_at=None):
    conn = jdc.job_db_connect(db_path)
    job_uuid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (job_uuid, tool, server, status, created_at) VALUES (?, ?, ?, 'built', ?)",
        (job_uuid, "generate_image", server, created_at or jdc.now_iso()),
    )
    conn.execute(
        "INSERT INTO outputs (job_uuid, raw_json, recorded_at) VALUES (?, ?, ?)",
        (job_uuid, json.dumps(graph), jdc.now_iso()),
    )
    conn.commit()
    conn.close()
    return job_uuid


def _seed_tracked(db_path, server, comfy_prompt_id):
    conn = jdc.job_db_connect(db_path)
    conn.execute(
        "INSERT INTO jobs (job_uuid, comfy_prompt_id, tool, server, status, created_at) VALUES (?, ?, 'generate_image', ?, 'completed', ?)",
        (str(uuid.uuid4()), comfy_prompt_id, server, jdc.now_iso()),
    )
    conn.commit()
    conn.close()


class ImportServerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmpdir) / "jobs.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, history, extra_args=None):
        with patch.object(mod, "requests") as fake_requests:
            fake_requests.get.return_value = _fake_response(history)
            fake_requests.exceptions.RequestException = Exception
            sys.argv = ["import_server_history.py", SERVER, self.db_path] + (extra_args or [])
            mod.main()

    def _jobs(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM jobs").fetchall()
        conn.close()
        return rows

    def test_skips_errored_entries(self):
        self._run({"e1": _entry("e1", {}, status_str="error")})
        self.assertEqual(len(self._jobs()), 0)

    def test_skips_already_tracked(self):
        _seed_tracked(self.db_path, SERVER, "already-id")
        self._run({"already-id": _entry("already-id", {"1": {"class_type": "KSampler", "inputs": {}}})})
        self.assertEqual(len(self._jobs()), 1)  # unchanged, still just the seeded row

    def test_reconciles_unlinked_job_without_creating_a_new_row(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 1}}}
        job_uuid = _seed_unlinked(self.db_path, SERVER, graph)
        self._run({"found-id": _entry("found-id", graph, start_ms=1000, end_ms=5000)})

        rows = self._jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_uuid"], job_uuid)
        self.assertEqual(rows[0]["comfy_prompt_id"], "found-id")
        self.assertEqual(rows[0]["status"], "completed")
        self.assertIsNotNone(rows[0]["duration_s"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        results = conn.execute("SELECT stage FROM results WHERE job_uuid=?", (job_uuid,)).fetchall()
        self.assertIn("history", [r["stage"] for r in results])
        conn.close()

    def test_imports_brand_new_job_with_correct_fields(self):
        graph = {
            "10": {"class_type": "CLIPTextEncode", "inputs": {"text": "a dragon"}},
            "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry"}},
            "12": {"class_type": "KSampler", "inputs": {"positive": ["10", 0], "negative": ["11", 0], "seed": 99}},
        }
        self._run({"brand-new": _entry("brand-new", graph, filename="dragon.png")})

        rows = self._jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "external")
        self.assertEqual(rows[0]["comfy_prompt_id"], "brand-new")
        self.assertEqual(rows[0]["status"], "completed")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        job_uuid = rows[0]["job_uuid"]
        prompts = conn.execute("SELECT * FROM prompts WHERE job_uuid=?", (job_uuid,)).fetchone()
        self.assertEqual(prompts["positive_prompt"], "a dragon")
        self.assertEqual(prompts["negative_prompt"], "blurry")
        params = {r["param_name"]: r["value_text"] for r in conn.execute("SELECT * FROM node_params WHERE job_uuid=?", (job_uuid,))}
        self.assertEqual(params["seed"], "99")
        outputs = conn.execute("SELECT raw_json FROM outputs WHERE job_uuid=?", (job_uuid,)).fetchone()
        self.assertEqual(json.loads(outputs["raw_json"]), graph)
        inputs = conn.execute("SELECT raw_json FROM inputs WHERE job_uuid=?", (job_uuid,)).fetchone()
        self.assertIn("import_server_history.py", json.loads(inputs["raw_json"])["source"])
        conn.close()

    def test_dry_run_writes_nothing(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {}}}
        self._run({"would-import": _entry("would-import", graph)}, extra_args=["--dry-run"])
        self.assertEqual(len(self._jobs()), 0)

    def test_dry_run_does_not_backfill_existing_row(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 1}}}
        job_uuid = _seed_unlinked(self.db_path, SERVER, graph)
        self._run({"found-id": _entry("found-id", graph)}, extra_args=["--dry-run"])
        rows = self._jobs()
        self.assertEqual(rows[0]["job_uuid"], job_uuid)
        self.assertIsNone(rows[0]["comfy_prompt_id"])  # still unlinked - dry-run must not write

    def test_ambiguous_match_still_backfills_the_closest_candidate(self):
        graph = {"12": {"class_type": "KSampler", "inputs": {"seed": 1}}}
        close_uuid = _seed_unlinked(self.db_path, SERVER, graph, created_at="1970-01-01T00:00:01+00:00")
        _seed_unlinked(self.db_path, SERVER, graph, created_at="1970-01-01T00:00:02+00:00")
        self._run({"amb-id": _entry("amb-id", graph, start_ms=1500)})

        rows = self._jobs()
        linked = [r for r in rows if r["comfy_prompt_id"] == "amb-id"]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["job_uuid"], close_uuid)

    def test_no_history_entries(self):
        self._run({})
        self.assertEqual(len(self._jobs()), 0)

    def test_entry_with_no_graph_is_skipped_not_crashed(self):
        self._run({"nograph": {"status": {"status_str": "success"}, "outputs": {"7": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}}})
        self.assertEqual(len(self._jobs()), 0)

    def test_unreachable_server_exits_with_clear_message(self):
        import requests as real_requests

        with patch.object(mod, "requests") as fake_requests:
            fake_requests.exceptions.RequestException = real_requests.exceptions.RequestException
            fake_requests.get.side_effect = real_requests.exceptions.ConnectionError("boom")
            sys.argv = ["import_server_history.py", SERVER, self.db_path]
            with self.assertRaises(SystemExit):
                mod.main()

    def test_creates_db_file_on_dry_run_if_missing(self):
        self.assertFalse(Path(self.db_path).exists())
        self._run({}, extra_args=["--dry-run"])
        self.assertTrue(Path(self.db_path).exists())


if __name__ == "__main__":
    unittest.main()
