import asyncio
import json
import os
import sqlite3
import tempfile
import unittest

import folder_chat_search as fcs


def chat_json(*texts):
    return json.dumps({"history": {"messages": {str(i): {"role": "user", "content": t} for i, t in enumerate(texts)}}})


class SearchTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        c = sqlite3.connect(self.path)
        c.execute("CREATE TABLE folder (id TEXT, name TEXT, parent_id TEXT, user_id TEXT)")
        c.execute("CREATE TABLE chat (id TEXT, user_id TEXT, title TEXT, updated_at INT, archived INT, chat TEXT, meta TEXT, folder_id TEXT)")
        c.executemany("INSERT INTO folder VALUES (?,?,?,?)", [
            ("f1", "Companion", None, "u1"), ("f2", "Sub", "f1", "u1"), ("f3", "Secret", None, "u1"), ("f4", "Companion", None, "u2"),
        ])
        rows = [
            ("a", "u1", "Morning", 100, 0, chat_json("we talked about the blue guitar"), "{}", "f1"),
            ("b", "u1", "Evening", 200, 0, chat_json("guitar strings and drums"), "{}", "f2"),
            ("c", "u1", "Hidden", 300, 0, chat_json("guitar in the secret folder"), "{}", "f3"),
            ("d", "u1", "Loose", 400, 0, chat_json("guitar with no folder"), "{}", None),
            ("e", "u2", "Other user", 500, 0, chat_json("guitar of someone else"), "{}", "f4"),
            ("f", "u1", "Old", 50, 1, chat_json("archived guitar"), "{}", "f1"),
            ("g", "u1", "Tick", 600, 0, chat_json("guitar tick"), '{"internal": true}', "f1"),
        ]
        c.executemany("INSERT INTO chat VALUES (?,?,?,?,?,?,?,?)", rows)
        c.commit()
        c.close()
        self.addCleanup(os.remove, self.path)

    def run_tool(self, query="guitar", user="u1", **kw):
        t = fcs.Tools()
        t.valves.CHATS_DB_PATH = self.path
        t.valves.FOLDERS = kw.pop("folders", "companion")
        for k, val in kw.pop("valves", {}).items():
            setattr(t.valves, k, val)
        res = asyncio.run(t.search_folder_chats(query, __user__={"id": user}, **kw))
        return json.loads(res)

    def ids(self, out):
        return [r["id"] for r in out["results"]]


class FolderScopeTests(SearchTestCase):
    def test_only_configured_folder_and_subfolders(self):
        self.assertEqual(self.ids(self.run_tool()), ["b", "a"])

    def test_subfolders_off(self):
        self.assertEqual(self.ids(self.run_tool(valves={"INCLUDE_SUBFOLDERS": False})), ["a"])

    def test_folder_by_id(self):
        self.assertEqual(self.ids(self.run_tool(folders="f3")), ["c"])

    def test_other_users_folder_never_searched(self):
        self.assertEqual(self.ids(self.run_tool(user="u2")), ["e"])

    def test_empty_valve_refuses(self):
        self.assertIn("not set up", self.run_tool(folders="")["error"])

    def test_unknown_folder(self):
        self.assertIn("were found", self.run_tool(folders="Nope")["error"])

    def test_archived_and_internal(self):
        self.assertNotIn("f", self.ids(self.run_tool()))
        self.assertNotIn("g", self.ids(self.run_tool()))
        self.assertIn("f", self.ids(self.run_tool(valves={"INCLUDE_ARCHIVED": True})))

    def test_current_chat_excluded(self):
        t = fcs.Tools(); t.valves.CHATS_DB_PATH = self.path; t.valves.FOLDERS = "companion"
        out = json.loads(asyncio.run(t.search_folder_chats("guitar", __user__={"id": "u1"}, __chat_id__="b")))
        self.assertEqual(self.ids(out), ["a"])

    def test_model_cannot_pass_a_folder(self):
        with self.assertRaises(TypeError):
            self.run_tool(folder="Secret")


class CapTests(SearchTestCase):
    def test_cap_beats_count(self):
        out = self.run_tool(count="50", valves={"MAX_RESULTS": 1})
        self.assertEqual(out["returned"], 1)
        self.assertEqual(out["total_matches"], 2)

    def test_count_below_cap(self):
        self.assertEqual(self.run_tool(count="1")["returned"], 1)

    def test_default_count_also_capped(self):
        self.assertEqual(self.run_tool(valves={"DEFAULT_COUNT": 9, "MAX_RESULTS": 1})["returned"], 1)

    def test_bad_count_uses_default(self):
        self.assertEqual(self.run_tool(count="0")["returned"], 2)

    def test_count_not_a_number(self):
        self.assertIn("count must be a number", self.run_tool(count="lots")["error"])


class QueryTests(SearchTestCase):
    def test_all_words_then_any(self):
        out = self.run_tool("guitar drums")
        self.assertEqual((out["matched"], self.ids(out)), ("all", ["b"]))
        out = self.run_tool("drums OR blue")
        self.assertEqual((out["matched"], sorted(self.ids(out))), ("any", ["a", "b"]))
        self.assertIn("note", out)

    def test_title_match_snippet(self):
        out = self.run_tool("morning")
        self.assertEqual(out["results"][0]["id"], "a")

    def test_snippet_and_folder_name(self):
        r = self.run_tool("blue")["results"][0]
        self.assertIn("blue guitar", r["snippet"])
        self.assertEqual(r["folder"], "Companion")

    def test_no_match(self):
        out = self.run_tool("zebra")
        self.assertEqual((out["returned"], out["results"]), (0, []))

    def test_empty_query(self):
        self.assertIn("query is required", self.run_tool("OR")["error"])

    def test_time_window(self):
        self.assertEqual(self.ids(self.run_tool(start_timestamp="150")), ["b"])
        self.assertEqual(self.ids(self.run_tool(end_timestamp="150")), ["a"])

    def test_missing_db(self):
        out = self.run_tool(valves={"CHATS_DB_PATH": "/nonexistent/x.db"})
        self.assertIn("error", out)


class HelperTests(unittest.TestCase):
    def test_message_texts_list_content_and_old_layout(self):
        chat = {"messages": [{"content": [{"type": "text", "text": "hi"}, {"type": "image_url"}]}, {"content": "yo"}]}
        self.assertEqual(fcs.message_texts(chat), ["hi", "yo"])

    def test_search_words(self):
        self.assertEqual(fcs.search_words("Cats, OR dogs AND cats"), ["cats", "dogs"])


if __name__ == "__main__":
    unittest.main()
