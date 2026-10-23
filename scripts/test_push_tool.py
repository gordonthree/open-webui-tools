"""
Tests for push_tool.py's create/update paths, with `requests` faked. Run from scripts/ with:
python3 -m unittest test_push_tool -v
"""

import unittest
from unittest import mock

import push_tool as pt

SOURCE = "title: Agent Notes Curation\nversion: 1\ndescription: The upkeep half.\n"


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload, self.text = status, payload, str(payload)

    def json(self):
        return self._payload


class PushTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(pt, "requests")
        self.requests = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(pt, "SRC_DIR")
        src = patcher.start()
        self.addCleanup(patcher.stop)
        fake = mock.MagicMock()
        fake.exists.return_value = True
        fake.read_text.return_value = SOURCE
        src.__truediv__.return_value = fake
        patcher = mock.patch.object(pt, "BACKUP_DIR")
        self.addCleanup(patcher.stop)
        patcher.start()

    def push(self, **kw):
        pt.push_one("agent_notes_curation", "https://owui", "key", "agent_notes_curation", False, **kw)

    def test_a_missing_tool_is_created_only_when_asked(self):
        self.requests.get.return_value = Resp(404, {"detail": "not found"})
        with self.assertRaises(SystemExit) as refused:
            self.push()
        self.assertIn("--create", str(refused.exception))
        self.requests.post.assert_not_called()

        self.requests.post.return_value = Resp(200, {"id": "agent_notes_curation", "name": "Agent Notes Curation"})
        self.push(create=True)
        url = self.requests.post.call_args.args[0]
        body = self.requests.post.call_args.kwargs["json"]
        self.assertEqual(url, "https://owui/api/v1/tools/create")
        self.assertEqual(body, {"id": "agent_notes_curation", "name": "Agent Notes Curation", "content": SOURCE,
                                "meta": {"description": "The upkeep half."}, "access_grants": []})

    def test_a_failed_create_is_reported(self):
        self.requests.get.return_value = Resp(401, {"detail": "nope"})
        self.requests.post.return_value = Resp(400, {"detail": "bad id"})
        with self.assertRaises(SystemExit) as failed:
            self.push(create=True)
        self.assertIn("Create failed (400)", str(failed.exception))

    def test_dry_run_never_creates(self):
        self.requests.get.return_value = Resp(404, {})
        pt.push_one("agent_notes_curation", "https://owui", "key", "agent_notes_curation", True, create=True)
        self.requests.post.assert_not_called()

    def test_an_existing_tool_is_updated_not_created_even_with_create(self):
        self.requests.get.return_value = Resp(200, {"name": "Agent Notes Curation", "content": "old", "meta": {}, "access_grants": []})
        self.requests.post.return_value = Resp(200, {"name": "Agent Notes Curation", "updated_at": 1})
        with mock.patch.object(pt, "backup_current_content", return_value="backup"):
            self.push(create=True)
        self.assertTrue(self.requests.post.call_args.args[0].endswith("/api/v1/tools/id/agent_notes_curation/update"))

    def test_ids_with_create(self):
        self.assertEqual(pt.resolve_tool_id({}, "local", "agent_notes_curation", "u", "k", create=True), "agent_notes_curation")
        self.assertEqual(pt.resolve_tool_id({"agent_notes_curation": "abc"}, "local", "agent_notes_curation", "u", "k", create=True), "abc")
        with self.assertRaises(SystemExit):
            pt.resolve_tool_id({}, "local", "agent_notes_curation", "u", "k")
        self.requests.get.return_value = Resp(200, [{"id": "agent_notes"}])
        self.assertEqual(pt.resolve_tool_id({}, "hannah", "agent_notes_curation", "u", "k", create=True), "agent_notes_curation")
        with self.assertRaises(SystemExit) as missing:
            pt.resolve_tool_id({}, "hannah", "agent_notes_curation", "u", "k")
        self.assertIn("--create", str(missing.exception))

    def test_curation_is_a_known_tool_and_last_so_all_still_reaches_the_others(self):
        self.assertIn("agent_notes_curation", pt.TOOL_NAMES)
        self.assertEqual(pt.TOOL_NAMES[-1], "agent_notes_curation")


class ExplicitOnly(unittest.TestCase):
    def test_research_browser_is_known_but_not_part_of_all(self):
        self.assertNotIn("research_browser", pt.TOOL_NAMES)
        self.assertIn("research_browser", pt.EXPLICIT_TOOLS)
        self.assertFalse(set(pt.EXPLICIT_TOOLS) & set(pt.FUNCTION_NAMES))


if __name__ == "__main__":
    unittest.main()
