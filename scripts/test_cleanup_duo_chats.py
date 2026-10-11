"""
Tests for cleanup_duo_chats.py. Run with: python -m unittest test_cleanup_duo_chats -v
(no Open WebUI server needed - HTTP is faked).
"""
import tempfile
import time
import unittest
from pathlib import Path

import cleanup_duo_chats as mod

NOW = 1_800_000_000


def make_summary(chat_id, title, age_min, now=NOW):
    return {"id": chat_id, "title": title, "updated_at": now - age_min * 60}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def raise_for_status(self):
        assert self.status_code == 200

    def json(self):
        return self._payload


class FakeServer:
    """One simulated OWUI server's chat list and delete endpoint."""

    def __init__(self, chats):
        self.chats = list(chats)
        self.deleted = []

    def get(self, **_):
        return FakeResponse(self.chats)

    def delete(self, chat_id, **_):
        self.deleted.append(chat_id)
        return FakeResponse(True)


class FakeSession:
    """Routes a request to the FakeServer whose base URL it starts with, like the project's other
    multi-server fakes (see test_agent_duo.py's Router)."""

    def __init__(self, **servers):
        self.servers = servers  # base_url -> FakeServer

    def _pick(self, url):
        for base, srv in self.servers.items():
            if url.startswith(base):
                return srv
        raise AssertionError(f"unexpected URL {url}")

    def get(self, url, **kw):
        return self._pick(url).get(**kw)

    def delete(self, url, **kw):
        return self._pick(url).delete(url.rsplit("/", 1)[1], **kw)


class FindDoomedTests(unittest.TestCase):
    def test_matches_prefix_and_age(self):
        chats = [
            make_summary("a", "[agent_duo] Mara", 90),
            make_summary("b", "[agent_duo] Hannah", 5),   # too new
            make_summary("c", "some other chat", 200),    # wrong title
            make_summary("d", "[agent_duo] Mara", 61),
        ]
        found = mod.find_doomed(chats, "[agent_duo]", 60 * 60, NOW)
        self.assertEqual({c["id"] for c in found}, {"a", "d"})

    def test_missing_timestamp_is_kept_not_treated_as_ancient(self):
        chats = [{"id": "x", "title": "[agent_duo] Mara"}]
        self.assertEqual(mod.find_doomed(chats, "[agent_duo]", 60, NOW), [])

    def test_custom_prefix(self):
        chats = [make_summary("a", "[duo-test] x", 200)]
        self.assertEqual(len(mod.find_doomed(chats, "[duo-test]", 60, NOW)), 1)
        self.assertEqual(len(mod.find_doomed(chats, "[agent_duo]", 60, NOW)), 0)


def write_secrets(tmp_dir):
    path = Path(tmp_dir) / "secrets.md"
    path.write_text(
        "OWUI_BASE_URL: https://mara.test\n"
        "OWUI_API_KEY: mk\n"
        "OWUI_HANNAH_URL: https://hannah.test\n"
        "OWUI_HANNAH_KEY: hk\n"
    )
    return path


def live_summary(chat_id, title, age_min):
    """A chat summary aged relative to the real clock - for main()-level tests, since main() calls
    time.time() itself rather than taking it as a parameter."""
    return {"id": chat_id, "title": title, "updated_at": time.time() - age_min * 60}


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secrets = write_secrets(self.tmp.name)

    def test_dry_run_deletes_nothing_on_either_server(self):
        session = FakeSession(**{
            "https://mara.test": FakeServer([live_summary("a", "[agent_duo] Mara", 90)]),
            "https://hannah.test": FakeServer([live_summary("b", "[agent_duo] Hannah", 90)]),
        })
        rc = mod.main(["--dry-run", "--secrets", str(self.secrets)], session=session)
        self.assertEqual(rc, 0)
        self.assertEqual(session.servers["https://mara.test"].deleted, [])
        self.assertEqual(session.servers["https://hannah.test"].deleted, [])

    def test_yes_deletes_old_matches_on_both_servers_by_default(self):
        session = FakeSession(**{
            "https://mara.test": FakeServer([live_summary("a", "[agent_duo] Mara", 90), live_summary("b", "keep me", 90)]),
            "https://hannah.test": FakeServer([live_summary("c", "[agent_duo] Hannah", 90)]),
        })
        rc = mod.main(["--yes", "--secrets", str(self.secrets)], session=session)
        self.assertEqual(rc, 0)
        self.assertEqual(session.servers["https://mara.test"].deleted, ["a"])
        self.assertEqual(session.servers["https://hannah.test"].deleted, ["c"])

    def test_server_flag_restricts_to_one_server(self):
        session = FakeSession(**{
            "https://mara.test": FakeServer([live_summary("a", "[agent_duo] Mara", 90)]),
            "https://hannah.test": FakeServer([live_summary("c", "[agent_duo] Hannah", 90)]),
        })
        rc = mod.main(["--yes", "--server", "hannah", "--secrets", str(self.secrets)], session=session)
        self.assertEqual(rc, 0)
        self.assertEqual(session.servers["https://mara.test"].deleted, [])
        self.assertEqual(session.servers["https://hannah.test"].deleted, ["c"])

    def test_unknown_server_is_rejected(self):
        session = FakeSession(**{"https://mara.test": FakeServer([]), "https://hannah.test": FakeServer([])})
        with self.assertRaises(SystemExit):
            mod.main(["--yes", "--server", "nope", "--secrets", str(self.secrets)], session=session)

    def test_too_young_is_kept(self):
        session = FakeSession(**{
            "https://mara.test": FakeServer([live_summary("a", "[agent_duo] Mara", 5)]),
            "https://hannah.test": FakeServer([]),
        })
        rc = mod.main(["--yes", "--secrets", str(self.secrets)], session=session)
        self.assertEqual(rc, 0)
        self.assertEqual(session.servers["https://mara.test"].deleted, [])

    def test_failed_delete_is_reported_and_exit_is_nonzero(self):
        class FailingServer(FakeServer):
            def delete(self, chat_id, **_):
                return FakeResponse("boom", status=500)

        session = FakeSession(**{
            "https://mara.test": FailingServer([live_summary("a", "[agent_duo] Mara", 90)]),
            "https://hannah.test": FakeServer([]),
        })
        rc = mod.main(["--yes", "--secrets", str(self.secrets)], session=session)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
