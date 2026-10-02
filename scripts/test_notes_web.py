"""
Tests for notes_web.py. Run from scripts/ with: python -m unittest test_notes_web -v
(needs pydantic, like the agent_notes tests; no network beyond a loopback socket).
"""

import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import notes_web as web


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "notes.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path, **form):
        return web.handle_post(self.db, path, form)

    def get(self, path, **query):
        return web.handle_get(self.db, path, {k: [v] for k, v in query.items()})

    def test_empty_index_has_create_form(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("0 notes", body)
        self.assertIn("action='/create'", body)

    def test_create_append_edit_delete_entry_flow(self):
        status, headers, _ = self.post("/create", name="Ideas", text="first", comment="c")
        self.assertEqual((status, headers["Location"]), (303, "/note?name=Ideas"))
        self.post("/append", name="ideas", text="second")
        self.post("/edit", name="Ideas", entry_no="2", text="second, revised")
        _, _, body = self.get("/note", name="ideas")
        self.assertIn("second, revised", body)
        self.assertIn("edited", body)
        status, _, _ = self.post("/delete_entry", name="Ideas", entry_no="1")
        self.assertEqual(status, 303)
        _, _, body = self.get("/note", name="Ideas")
        self.assertNotIn("<pre>first</pre>", body)

    def test_rename_and_comment_then_delete_note(self):
        self.post("/create", name="a", text="x")
        status, headers, _ = self.post("/update", name="a", new_name="b", comment="hello")
        self.assertEqual(headers["Location"], "/note?name=b")
        _, _, body = self.get("/note", name="b")
        self.assertIn("hello", body)
        status, headers, _ = self.post("/delete_note", name="b")
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertEqual(self.get("/note", name="b")[0], 404)

    def test_search_finds_notes_and_entries(self):
        self.post("/create", name="recipes", text="add more garlic")
        _, _, body = self.get("/", q="garlic")
        self.assertIn("recipes #1", body)
        _, _, body = self.get("/", q="zzz")
        self.assertIn("Nothing matched", body)

    def test_validation_errors_are_shown_not_raised(self):
        status, _, body = self.post("/create", name="n", text="x" * 501)
        self.assertEqual(status, 400)
        self.assertIn("limit is 500", body)
        self.assertEqual(self.post("/create", name="", text="x")[0], 400)
        self.post("/create", name="dup", text="x")
        status, _, body = self.post("/create", name="DUP", text="y")
        self.assertEqual(status, 400)
        self.assertIn("already exists", body)
        self.assertEqual(self.post("/append", name="nope", text="y")[0], 400)
        self.assertEqual(self.post("/edit", name="dup", entry_no="abc", text="y")[0], 400)

    def test_user_text_is_html_escaped(self):
        self.post("/create", name="<b>n</b>", text="<script>alert(1)</script>")
        _, _, body = self.get("/note", name="<b>n</b>")
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)
        _, _, index = self.get("/")
        self.assertNotIn("<b>n</b>", index)

    def test_authors_are_recorded_and_shown(self):
        web.handle_post(self.db, "/create", {"name": "n", "text": "x"}, author="Gordon")
        web.handle_post(self.db, "/append", {"name": "n", "text": "y"}, author="Hand Typed")
        _, _, body = self.get("/note", name="n")
        self.assertIn("started by Gordon", body)
        self.assertIn("#2 &middot; Hand Typed", body)
        self.assertIn("<td>Gordon</td>", self.get("/")[2])

    def test_summary_is_shown_with_stale_flag(self):
        self.post("/create", name="n", text="x")
        self.assertNotIn("Summary by", self.get("/note", name="n")[2])
        web.notes.update_summary_db(self.db, "n", "the gist", "Nightly")
        body = self.get("/note", name="n")[2]
        self.assertIn("Summary by Nightly", body)
        self.assertIn("the gist", body)
        self.assertNotIn("stale", body)
        self.post("/append", name="n", text="y")
        self.assertIn("stale: the note changed since", self.get("/note", name="n")[2])

    def test_tag_flow(self):
        self.post("/create", name="Ideas", text="x")
        self.post("/create", name="Other", text="y")
        status, headers, _ = self.post("/add_tag", name="Ideas", tags="Comfy, SDXL")
        self.assertEqual(status, 303)
        _, _, body = self.get("/note", name="Ideas")
        self.assertIn("action='/remove_tag'", body)
        self.assertIn("/?tag=comfy", body)
        _, _, body = self.get("/", tag="comfy")
        self.assertIn("1 note tagged comfy", body)
        self.assertNotIn("Other</a></td>", body)
        self.post("/add_tag", name="Other", tags="sdxl-poses")  # a person may create a near-duplicate
        status, headers, _ = self.post("/rename_tag", tag="sdxl-poses", new_tag="sdxl")
        self.assertEqual(headers["Location"], "/?tag=sdxl")
        self.assertIn("sdxl", self.get("/tags")[2])
        status, _, _ = self.post("/remove_tag", name="Ideas", tag="comfy")
        self.assertEqual(status, 303)
        self.assertEqual(self.post("/remove_tag", name="Ideas", tag="comfy")[0], 400)

    def test_author_can_be_changed_from_the_note_page(self):
        self.post("/create", name="Ideas", text="x")
        status, _, _ = self.post("/update", name="Ideas", new_name="Ideas", comment="", author="Someone Else")
        self.assertEqual(status, 303)
        self.assertIn("started by Someone Else", self.get("/note", name="Ideas")[2])

    def test_unknown_routes(self):
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.post("/nope", name="x")[0], 404)


class LiveServerTest(unittest.TestCase):
    def test_form_post_round_trip_over_http(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(str(Path(tmp) / "n.sqlite3")))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        data = urllib.parse.urlencode({"name": "wire test", "text": "héllo", "comment": ""}).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "/create", data=data)) as resp:  # follows the 303
            page = resp.read().decode("utf-8")
        self.assertIn("wire test", page)
        self.assertIn("héllo", page)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(base + "/note?name=missing")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
