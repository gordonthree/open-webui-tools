"""
Tests for cleanup_noop_chats.py. Run with:  python -m unittest test_cleanup_noop_chats -v
(no Open WebUI server needed - HTTP is faked).
"""
import tempfile
import time
import unittest
from pathlib import Path

import cleanup_noop_chats as mod

FOLDER = "folder-1"
NOW = 1_800_000_000


def make_chat(chat_id, final_text, done=True, role="assistant", folder_id=FOLDER, age_min=120):
    return {
        "id": chat_id,
        "title": f"tick {chat_id}",
        "folder_id": folder_id,
        "updated_at": NOW - age_min * 60,
        "chat": {"history": {"currentId": "m2", "messages": {
            "m1": {"id": "m1", "role": "user", "content": "tick"},
            "m2": {"id": "m2", "role": role, "content": final_text, "done": done},
        }}},
    }


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def raise_for_status(self):
        assert self.status_code == 200

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, chats, folders=None):
        self.chats = {c["id"]: c for c in chats}
        self.folders = folders if folders is not None else [{"id": FOLDER, "name": "Companion ticks"}]
        self.deleted = []

    def get(self, url, **_):
        if url.endswith("/api/v1/folders/"):
            return FakeResponse(self.folders)
        if "/api/v1/chats/folder/" in url:
            fid = url.rsplit("/", 1)[1]
            return FakeResponse([{"id": c["id"], "title": c["title"], "updated_at": c["updated_at"]}
                                 for c in self.chats.values() if c["folder_id"] == fid])
        return FakeResponse(self.chats[url.rsplit("/", 1)[1]])

    def delete(self, url, **_):
        self.deleted.append(url.rsplit("/", 1)[1])
        return FakeResponse(True)


class IsNoopTests(unittest.TestCase):
    def test_exact_noop(self):
        self.assertTrue(mod.is_noop(make_chat("a", "NOOP")))

    def test_whitespace_and_case(self):
        self.assertTrue(mod.is_noop(make_chat("a", "  noop\n")))

    def test_explanation_is_not_noop(self):
        self.assertFalse(mod.is_noop(make_chat("a", "NOOP - nothing to do")))
        self.assertFalse(mod.is_noop(make_chat("a", "NOOP.")))

    def test_unfinished_message_is_kept(self):
        self.assertFalse(mod.is_noop(make_chat("a", "", done=False)))
        self.assertFalse(mod.is_noop(make_chat("a", "NOOP", done=False)))

    def test_user_reply_after_noop_is_kept(self):
        self.assertFalse(mod.is_noop(make_chat("a", "NOOP", role="user")))

    def test_missing_history_is_kept(self):
        self.assertFalse(mod.is_noop({"chat": {}}))
        self.assertFalse(mod.is_noop({}))


class FindTests(unittest.TestCase):
    def test_finds_only_old_noop_chats_in_folder(self):
        s = FakeSession([
            make_chat("noop-old", "NOOP"),
            make_chat("noop-new", "NOOP", age_min=5),
            make_chat("acted", "I tidied my notes."),
            make_chat("noop-elsewhere", "NOOP", folder_id="other"),
        ])
        found = mod.find_noop_chats(s, "http://x", FOLDER, 3600, NOW)
        self.assertEqual([c["id"] for c in found], ["noop-old"])

    def test_chat_whose_record_disagrees_about_folder_is_kept(self):
        s = FakeSession([make_chat("odd", "NOOP")])
        real_get = s.get

        def get(url, **kw):
            r = real_get(url, **kw)
            if url.endswith("/odd"):
                r._payload = dict(r._payload, folder_id=None)
            return r
        s.get = get
        self.assertEqual(mod.find_noop_chats(s, "http://x", FOLDER, 3600, NOW), [])

    def test_folder_lookup(self):
        s = FakeSession([])
        self.assertEqual(mod.find_folder_id(s, "http://x", "companion TICKS"), FOLDER)
        with self.assertRaises(SystemExit):
            mod.find_folder_id(FakeSession([], folders=[]), "http://x", "Companion ticks")
        dup = [{"id": "1", "name": "Companion ticks"}, {"id": "2", "name": "companion ticks"}]
        with self.assertRaises(SystemExit):
            mod.find_folder_id(FakeSession([], folders=dup), "http://x", "Companion ticks")


class MainTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.secrets = Path(d) / "secrets.md"
        self.secrets.write_text("OWUI_BASE_URL: http://owui\nOWUI_API_KEY: k\n")

    def run_main(self, session, *extra):
        # chats are stamped relative to NOW; pretend a long time has passed so they're old enough
        return mod.main(["--secrets", str(self.secrets), *extra], session=session)

    def make_session(self):
        now = time.time()
        chats = [make_chat("n1", "NOOP"), make_chat("keep", "hello Gordon")]
        for c in chats:
            c["updated_at"] = now - 7200
        return FakeSession(chats)

    def test_dry_run_deletes_nothing(self):
        s = self.make_session()
        self.assertEqual(self.run_main(s, "--dry-run"), 0)
        self.assertEqual(s.deleted, [])

    def test_yes_deletes_only_noop(self):
        s = self.make_session()
        self.assertEqual(self.run_main(s, "--yes"), 0)
        self.assertEqual(s.deleted, ["n1"])

    def test_no_tty_without_yes_aborts(self):
        s = self.make_session()
        self.assertEqual(self.run_main(s), 1)
        self.assertEqual(s.deleted, [])


if __name__ == "__main__":
    unittest.main()
