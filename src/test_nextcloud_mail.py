"""
Tests for nextcloud_mail.py. Run with: python -m unittest test_nextcloud_mail -v
(no Nextcloud needed - requests.request is replaced with a fake).
"""

import unittest
from unittest import mock

import nextcloud_mail as mod


class Resp:
    def __init__(self, data=None, status=200):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return {"ocs": {"meta": {}, "data": self._data}}


class FakeNC:
    """Records calls; serves one mailbox with a set of unread messages."""

    def __init__(self, unread=(1, 2, 3), fail_flag_ids=()):
        self.unread = list(unread)
        self.fail = set(fail_flag_ids)
        self.calls = []
        self.sync_status = 200

    def __call__(self, method, url, params=None, json=None, **kw):
        self.calls.append((method, url, params, json))
        if url.endswith("/account/list"):
            return Resp([{"id": 7, "email": "me@x.org"}])
        if url.endswith("/ocs/mailboxes"):
            return Resp([{"name": "INBOX", "databaseId": 11, "unread": len(self.unread)}])
        if url.endswith("/ocs/mailboxes/11/messages"):
            return Resp(
                [{"databaseId": i, "flags": {"seen": False}} for i in self.unread[:50]]
            )
        if "/message/" in url and method == "GET":
            return Resp({"subject": "Hi", "body": "hello", "from": [], "to": []})
        if url.endswith("/sync") and method == "POST":
            if self.sync_status >= 400:
                return Resp("x", self.sync_status)
            return Resp("ok")
        if url.endswith("/flags") and method == "PUT":
            mid = int(url.split("/messages/")[1].split("/")[0])
            if mid in self.fail:
                return Resp("nope", 500)
            if json["flags"]["seen"] and mid in self.unread:
                self.unread.remove(mid)
            return Resp("ok")
        raise AssertionError(f"unexpected call {method} {url}")

    def flag_calls(self):
        return [c for c in self.calls if c[1].endswith("/flags")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tool = mod.Tools()
        self.tool.valves.NEXTCLOUD_USER = "u"
        self.tool.valves.NEXTCLOUD_APP_PASSWORD = "p"

    def run_with(self, fake):
        patcher = mock.patch.object(mod.requests, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class ReadEmailTests(Base):
    def test_marks_read_by_default(self):
        fake = self.run_with(FakeNC())
        out = self.tool.read_email("5")  # string id, like a small model sends
        self.assertIn("hello", out)
        calls = fake.flag_calls()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith("/messages/5/flags"))
        self.assertEqual(calls[0][3], {"flags": {"seen": True}})

    def test_mark_read_false_leaves_it_alone(self):
        fake = self.run_with(FakeNC())
        self.tool.read_email(5, mark_read=False)
        self.assertEqual(fake.flag_calls(), [])

    def test_valve_disables_marking(self):
        fake = self.run_with(FakeNC())
        self.tool.valves.MARK_READ_ON_OPEN = False
        self.tool.read_email(5)
        self.assertEqual(fake.flag_calls(), [])

    def test_flag_failure_still_returns_the_email(self):
        self.run_with(FakeNC(fail_flag_ids=[5]))
        out = self.tool.read_email(5)
        self.assertIn("hello", out)
        self.assertIn("could not mark", out)


class MarkTests(Base):
    def test_mark_unread(self):
        fake = self.run_with(FakeNC())
        self.assertIn("unread", self.tool.mark_read(2, read=False))
        self.assertEqual(fake.flag_calls()[0][3], {"flags": {"seen": False}})

    def test_mark_all_read(self):
        fake = self.run_with(FakeNC(unread=range(1, 121)))
        out = self.tool.mark_all_read()
        self.assertIn("Marked 120", out)
        self.assertEqual(fake.unread, [])

    def test_mark_all_read_reports_failures_and_terminates(self):
        fake = self.run_with(FakeNC(unread=[1, 2, 3], fail_flag_ids=[2]))
        out = self.tool.mark_all_read()
        self.assertIn("Marked 2", out)
        self.assertIn("1 failed", out)
        self.assertEqual(fake.unread, [2])

    def test_mark_all_read_limit(self):
        self.run_with(FakeNC(unread=range(1, 21)))
        self.tool.valves.MARK_ALL_LIMIT = 5
        out = self.tool.mark_all_read()
        self.assertIn("Marked 5", out)
        self.assertIn("limit", out)

    def test_unknown_mailbox(self):
        self.run_with(FakeNC())
        self.assertIn("not found", self.tool.mark_all_read("Nope"))


class GuideTests(Base):
    def test_guide_names_every_tool_and_send_state(self):
        g = self.tool.mail_guide()
        for name in ("list_emails", "read_email", "mark_read", "mark_all_read", "send_email"):
            self.assertIn(name, g)
        self.assertIn("DISABLED", g)
        self.tool.valves.ALLOW_SEND = True
        self.assertIn("ENABLED", self.tool.mail_guide())


class ListSyncTests(Base):
    def synced_before_list(self, fake):
        urls = [c[1] for c in fake.calls]
        sync = next(i for i, u in enumerate(urls) if u.endswith("/sync"))
        lst = next(i for i, u in enumerate(urls) if u.endswith("/messages"))
        return sync < lst

    def test_syncs_before_listing(self):
        fake = self.run_with(FakeNC())
        out = self.tool.list_emails()
        self.assertTrue(self.synced_before_list(fake))
        self.assertNotIn("Note:", out)

    def test_sync_failure_is_reported_but_list_still_returned(self):
        fake = self.run_with(FakeNC())
        fake.sync_status = 500
        out = self.tool.list_emails()
        self.assertIn("could not refresh", out)
        self.assertIn("id 1", out.replace("id 1 ", "id 1 "))

    def test_valve_disables_sync(self):
        fake = self.run_with(FakeNC())
        self.tool.valves.SYNC_BEFORE_LIST = False
        self.tool.list_emails()
        self.assertFalse([c for c in fake.calls if c[1].endswith("/sync")])


class ErrorTests(Base):
    def flag_fails(self, status):
        class F(FakeNC):
            def __call__(self, method, url, params=None, json=None, **kw):
                if url.endswith("/flags"):
                    return Resp("x", status)
                return super().__call__(method, url, params, json, **kw)

        self.run_with(F())

    def test_unknown_id_hint(self):
        self.flag_fails(400)
        out = self.tool.mark_read(1)
        self.assertIn("HTTP 400", out)
        self.assertIn("list_emails", out)

    def test_auth_hint(self):
        self.flag_fails(401)
        self.assertIn("valves", self.tool.mark_read(1))

    def test_server_error_hint(self):
        self.flag_fails(502)
        self.assertIn("retry", self.tool.mark_read(1))

    def test_non_numeric_id_hint(self):
        self.run_with(FakeNC())
        out = self.tool.read_email("abc")
        self.assertIn("plain number", out)

    def test_network_hint(self):
        def boom(*a, **k):
            raise mod.requests.ConnectionError("down")

        self.run_with(boom)
        self.assertIn("What to do", self.tool.list_mailboxes())

    def test_send_unknown_account(self):
        self.run_with(FakeNC())
        self.tool.valves.ALLOW_SEND = True
        self.tool.valves.ACCOUNT_ID = 99
        self.assertIn("ACCOUNT_ID", self.tool.send_email("a@b.c", "s", "b"))

    def test_missing_credentials(self):
        self.tool.valves.NEXTCLOUD_USER = ""
        self.assertIn("credentials", self.tool.mark_read(1))

    def test_network_error_is_reported(self):
        def boom(*a, **k):
            raise mod.requests.ConnectionError("down")

        self.run_with(boom)
        self.assertIn("Could not reach", self.tool.read_email(1))


if __name__ == "__main__":
    unittest.main()
