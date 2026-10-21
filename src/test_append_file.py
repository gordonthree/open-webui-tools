import asyncio
import json
import types
import unittest

import requests as real_requests

import append_file as a

ROOT = "/companion/drafts"
NOTES = ROOT + "/lighthouse/lighthouse-notes.md"


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeTerminal:
    """Behaves like Open Terminal's file API as seen on 2026-10-21: 404 for a missing file, total_lines on read, size on write."""

    def __init__(self, files=None, key="k"):
        self.files = dict(files or {})
        self.key = key
        self.calls = []
        self.headers_seen = []
        self.write_size_offset = 0   # to simulate a bad write
        self.truncate_reads = False  # to simulate a read that returns less than the file
        self.fail_writes = False
        self.down = False
        self.RequestException = real_requests.RequestException

    def _auth(self, headers):
        return headers.get("Authorization") == f"Bearer {self.key}"

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        self.headers_seen.append(dict(headers or {}))
        if self.down:
            raise real_requests.ConnectionError("down")
        if not self._auth(headers or {}):
            return Resp(401, {"detail": "Invalid API key"})
        p = params["path"]
        if p not in self.files:
            return Resp(404, {"detail": "File not found"})
        content = self.files[p]
        lines = content.splitlines()
        shown = "\n".join(lines[:-1]) + "\n" if self.truncate_reads and len(lines) > 1 else content
        return Resp(200, {"path": p, "content": shown, "total_lines": len(lines)})

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        self.headers_seen.append(dict(headers or {}))
        if not self._auth(headers or {}):
            return Resp(401, {"detail": "Invalid API key"})
        if self.fail_writes:
            return Resp(500, {"detail": "boom"})
        self.files[json["path"]] = json["content"]
        return Resp(200, {"path": json["path"], "size": len(json["content"].encode("utf-8")) + self.write_size_offset})


def make(files=None, **valves):
    fake = FakeTerminal(files)
    a.requests = fake
    t = a.Tools()
    t.valves.API_KEY = "k"
    for k, val in valves.items():
        setattr(t.valves, k, val)
    return t, fake


def run(t, path, text, user=None):
    return json.loads(asyncio.run(t.append_file(path, text, __user__=user)))


class TestPaths(unittest.TestCase):
    def test_accepts_files_under_the_root(self):
        self.assertEqual(a.clean_path(NOTES, [ROOT]), (NOTES, None))
        self.assertEqual(a.clean_path(ROOT + "/x.md", [ROOT])[0], ROOT + "/x.md")

    def test_normalises_dots_that_stay_inside(self):
        self.assertEqual(a.clean_path(ROOT + "/lighthouse/../lighthouse/./n.md", [ROOT])[0], ROOT + "/lighthouse/n.md")

    def test_refuses_traversal_out_of_the_root(self):
        for bad in (ROOT + "/../autonomy/stories/dreams/the-lighthouse.md", ROOT + "/lighthouse/../../charter.md", "/etc/hostname"):
            self.assertIsNone(a.clean_path(bad, [ROOT])[0], bad)

    def test_refuses_a_sibling_that_shares_the_prefix(self):
        self.assertIsNone(a.clean_path("/companion/drafts-old/x.md", [ROOT])[0])
        self.assertIsNone(a.clean_path("/companion/drafts2/x.md", [ROOT])[0])

    def test_refuses_the_root_a_folder_and_relative_or_empty_paths(self):
        for bad in (ROOT, ROOT + "/", ROOT + "/lighthouse/", "lighthouse/notes.md", "", "   ", None):
            self.assertIsNone(a.clean_path(bad, [ROOT])[0], repr(bad))


class TestJoin(unittest.TestCase):
    def test_blank_line_between_old_and_new(self):
        self.assertEqual(a.join_text("one\n", "two"), "one\n\ntwo\n")
        self.assertEqual(a.join_text("one", "two"), "one\n\ntwo\n")
        self.assertEqual(a.join_text("one\n\n", "two\n\n"), "one\n\ntwo\n")

    def test_existing_text_is_kept_exactly(self):
        old = "## Step 1\nline  with  spaces  \n\n\n"
        self.assertTrue(a.join_text(old, "x").startswith(old))

    def test_empty_file_gets_just_the_text(self):
        self.assertEqual(a.join_text("", "hello\n\n"), "hello\n")


class TestAppend(unittest.TestCase):
    def test_appends_to_an_existing_file(self):
        t, fake = make({NOTES: "# Notes\n\n## Step 1\nread it\n"})
        r = run(t, NOTES, "## Step 5: Beats\n- one\n- two")
        self.assertTrue(r["success"]); self.assertFalse(r["created"])
        self.assertEqual(fake.files[NOTES], "# Notes\n\n## Step 1\nread it\n\n## Step 5: Beats\n- one\n- two\n")
        self.assertEqual(r["size_after"], len(fake.files[NOTES].encode()))
        self.assertNotIn("warning", r)

    def test_the_old_text_is_untouched(self):
        old = "# Notes\n\nthe tea — too hot, 'never' cools\n"
        t, fake = make({NOTES: old})
        run(t, NOTES, "more")
        self.assertTrue(fake.files[NOTES].startswith(old))

    def test_two_appends_keep_both(self):
        t, fake = make({NOTES: "a\n"})
        run(t, NOTES, "b"); run(t, NOTES, "c")
        self.assertEqual(fake.files[NOTES], "a\n\nb\n\nc\n")

    def test_creates_a_missing_file(self):
        t, fake = make()
        r = run(t, ROOT + "/new/story-notes.md", "# New\n")
        self.assertTrue(r["success"]); self.assertTrue(r["created"])
        self.assertEqual(fake.files[ROOT + "/new/story-notes.md"], "# New\n")

    def test_missing_file_is_an_error_when_creating_is_off(self):
        t, fake = make(CREATE_IF_MISSING=False)
        r = run(t, NOTES, "x")
        self.assertFalse(r["success"]); self.assertEqual(fake.files, {})

    def test_non_ascii_text_survives(self):
        t, fake = make({NOTES: "x\n"})
        run(t, NOTES, "em dash — and “quotes” and é")
        self.assertIn("em dash — and “quotes” and é", fake.files[NOTES])


class TestRefusals(unittest.TestCase):
    def test_outside_the_root_nothing_is_sent(self):
        t, fake = make({"/etc/hostname": "h\n"})
        r = run(t, "/etc/hostname", "x")
        self.assertFalse(r["success"]); self.assertEqual(fake.calls, [])
        r = run(t, ROOT + "/../autonomy/stories/backstory/x.md", "x")
        self.assertFalse(r["success"]); self.assertEqual(fake.calls, [])

    def test_empty_or_oversize_text(self):
        t, fake = make({NOTES: "x\n"})
        self.assertFalse(run(t, NOTES, "  \n ")["success"])
        t.valves.MAX_APPEND_CHARS = 10
        self.assertFalse(run(t, NOTES, "x" * 11)["success"])
        self.assertEqual(fake.files[NOTES], "x\n")

    def test_a_short_read_never_leads_to_a_write(self):
        t, fake = make({NOTES: "l1\nl2\nl3\n"})
        fake.truncate_reads = True
        r = run(t, NOTES, "x")
        self.assertFalse(r["success"])
        self.assertEqual(fake.files[NOTES], "l1\nl2\nl3\n")
        self.assertFalse([c for c in fake.calls if c[0] == "POST"])

    def test_a_very_long_file_is_left_alone(self):
        t, fake = make({NOTES: "x\n" * 10}, MAX_FILE_CHARS=5)
        self.assertFalse(run(t, NOTES, "y")["success"])
        self.assertFalse([c for c in fake.calls if c[0] == "POST"])

    def test_no_key_and_bad_key(self):
        t, fake = make({NOTES: "x\n"}); t.valves.API_KEY = ""
        self.assertIn("API key", run(t, NOTES, "y")["error"]); self.assertEqual(fake.calls, [])
        t.valves.API_KEY = "wrong"
        r = run(t, NOTES, "y")
        self.assertFalse(r["success"]); self.assertIn("refused", r["error"])
        self.assertEqual(fake.files[NOTES], "x\n")

    def test_server_down_and_failed_write(self):
        t, fake = make({NOTES: "x\n"}); fake.down = True
        r = run(t, NOTES, "y")
        self.assertFalse(r["success"]); self.assertIn("reach", r["error"])
        fake.down = False; fake.fail_writes = True
        r = run(t, NOTES, "y")
        self.assertFalse(r["success"]); self.assertIn("unchanged", r["error"])

    def test_a_wrong_saved_size_is_flagged(self):
        t, fake = make({NOTES: "x\n"}); fake.write_size_offset = 3
        r = run(t, NOTES, "y")
        self.assertTrue(r["success"]); self.assertIn("warning", r)


class TestFolders(unittest.TestCase):
    def test_parse_list(self):
        self.assertEqual(a.parse_list(" /a/b , ,/c/d,"), ["/a/b", "/c/d"])
        self.assertEqual(a.parse_list(""), [])
        self.assertEqual(a.parse_list(None), [])

    def test_several_folders(self):
        roots = ["/companion/drafts", "/companion/collaboration"]
        self.assertEqual(a.clean_path("/companion/collaboration/plan.md", roots)[0], "/companion/collaboration/plan.md")
        self.assertEqual(a.clean_path("/companion/drafts/x.md", roots)[0], "/companion/drafts/x.md")
        self.assertIsNone(a.clean_path("/companion/dreams/x.md", roots)[0])
        self.assertIn("/companion/collaboration/", a.clean_path("/companion/dreams/x.md", roots)[1])

    def test_traversal_between_allowed_folders_is_still_checked(self):
        roots = ["/companion/drafts"]
        self.assertIsNone(a.clean_path("/companion/drafts/../collaboration/x.md", roots)[0])

    def test_no_usable_folder_means_nothing_is_allowed(self):
        self.assertIsNone(a.clean_path(NOTES, [])[0])
        self.assertIsNone(a.clean_path(NOTES, ["drafts"])[0])

    def test_valve_with_a_list_end_to_end(self):
        other = "/companion/collaboration/plan.md"
        t, fake = make({other: "p\n"}, ALLOWED_ROOTS="/companion/drafts, /companion/collaboration")
        self.assertTrue(run(t, other, "q")["success"])
        self.assertFalse(run(t, "/companion/dreams/x.md", "q")["success"])


class TestProtected(unittest.TestCase):
    def test_a_name_matches_in_any_folder_and_any_case(self):
        p = ["charter.md"]
        for f in ("/companion/autonomy/charter.md", "/companion/drafts/charter.md", "/companion/drafts/Charter.MD"):
            self.assertTrue(a.is_protected(f, p), f)
        self.assertFalse(a.is_protected("/companion/drafts/charter.md.bak", p))
        self.assertFalse(a.is_protected("/companion/drafts/my-charter.md", p))

    def test_a_full_path_matches_only_that_file(self):
        p = ["/companion/drafts/lighthouse/final.md"]
        self.assertTrue(a.is_protected("/companion/drafts/lighthouse/final.md", p))
        self.assertFalse(a.is_protected("/companion/drafts/other/final.md", p))

    def test_protected_file_inside_an_allowed_folder_is_refused_without_any_call(self):
        f = ROOT + "/charter.md"
        t, fake = make({f: "keep\n"})
        r = run(t, f, "x")
        self.assertFalse(r["success"]); self.assertIn("protected", r["error"])
        self.assertEqual(fake.calls, []); self.assertEqual(fake.files[f], "keep\n")

    def test_a_dotted_route_to_a_protected_file_is_refused(self):
        f = ROOT + "/charter.md"
        t, fake = make({f: "keep\n"})
        self.assertFalse(run(t, ROOT + "/lighthouse/../charter.md", "x")["success"])
        self.assertEqual(fake.files[f], "keep\n")

    def test_the_list_is_a_valve_and_can_be_empty_or_longer(self):
        f = ROOT + "/charter.md"
        t, fake = make({f: "a\n", ROOT + "/seed.md": "s\n"}, PROTECTED_FILES="")
        self.assertTrue(run(t, f, "x")["success"])
        t, fake = make({f: "a\n", ROOT + "/seed.md": "s\n"}, PROTECTED_FILES="charter.md, seed.md")
        self.assertFalse(run(t, ROOT + "/seed.md", "x")["success"])


class TestUser(unittest.TestCase):
    def test_the_callers_id_is_sent_as_x_user_id(self):
        t, fake = make({NOTES: "x\n"})
        run(t, NOTES, "y", user={"id": "c63c2a9b-uuid", "name": "Gordon"})
        self.assertTrue(fake.headers_seen)
        self.assertTrue(all(h.get("X-User-Id") == "c63c2a9b-uuid" for h in fake.headers_seen))

    def test_no_user_no_header_and_the_valve_can_switch_it_off(self):
        t, fake = make({NOTES: "x\n"})
        run(t, NOTES, "y")
        self.assertTrue(all("X-User-Id" not in h for h in fake.headers_seen))
        t, fake = make({NOTES: "x\n"}, SEND_USER_ID=False)
        run(t, NOTES, "y", user={"id": "abc"})
        self.assertTrue(all("X-User-Id" not in h for h in fake.headers_seen))


if __name__ == "__main__":
    unittest.main()
