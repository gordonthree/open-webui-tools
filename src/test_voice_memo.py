"""
Tests for voice_memo.py. Run with: python -m unittest test_voice_memo -v
(no OpenRouter or Open WebUI needed - requests.post is replaced with a fake).
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_memo as mod


class Resp:
    def __init__(self, status=200, content=b"", payload=None, headers=None):
        self.status_code = status
        self.content = content
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mod.requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeNet:
    def __init__(self, tts=None, upload=None):
        self.tts = tts or Resp(200, b"ID3audio", headers={"Content-Type": "audio/mpeg"})
        self.upload = upload or Resp(200, payload={"id": "abc123"})
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        return self.tts if url.endswith("/audio/speech") else self.upload


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tool = mod.Tools()
        self.tool.valves.MEMO_DIR = str(Path(self.tmp.name) / "memos")
        self.tool.valves.OPENROUTER_API_KEY = "or-key"
        self.tool.valves.OPEN_WEBUI_API_KEY = "owui-key"

    def run_tool(self, fake, coro_fn, *a, **kw):
        with mock.patch.object(mod.requests, "post", fake):
            return asyncio.run(coro_fn(*a, **kw))


class RecordTests(Base):
    def test_success_returns_link_and_saves_files(self):
        fake = FakeNet()
        out = self.run_tool(fake, self.tool.record_voice_memo, "Remember the milk.", "Milk")
        self.assertIn("[Play Milk](/api/v1/files/abc123/content)", out)
        files = sorted(Path(self.tool.valves.MEMO_DIR).iterdir())
        self.assertEqual([f.suffix for f in files], [".json", ".mp3"])
        self.assertEqual(files[1].read_bytes(), b"ID3audio")
        meta = json.loads(files[0].read_text())
        self.assertEqual(meta["text"], "Remember the milk.")
        self.assertEqual(meta["url"], "/api/v1/files/abc123/content")

    def test_invented_voice_name_falls_back_to_default(self):
        fake = FakeNet()
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hi", "", "hannah-lee-calm")
        self.assertEqual(fake.calls[0][1]["json"]["voice"], self.tool.valves.DEFAULT_VOICE)
        self.assertIn("not a valid voice id", out)
        self.assertIn("hannah-lee-calm", out)

    def test_valid_voice_id_is_lowercased_and_no_note(self):
        fake = FakeNet()
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hi", "", "DEADBEEFDEADBEEFDEADBEEFDEADBEEF")
        self.assertEqual(fake.calls[0][1]["json"]["voice"], "deadbeef" * 4)
        self.assertNotIn("not a valid voice id", out)

    def test_memo_is_attached_to_chat_files(self):
        events = []

        async def emit(e):
            events.append(e)

        self.run_tool(FakeNet(), self.tool.record_voice_memo, "Hi", "T", "", emit)
        files = [e for e in events if e["type"] == "files"]
        self.assertEqual(len(files), 1)
        f = files[0]["data"]["files"][0]
        self.assertEqual((f["id"], f["url"]), ("abc123", "/api/v1/files/abc123/content"))
        self.assertTrue(f["name"].endswith("-t.mp3"))

    def test_no_attach_when_valve_off_or_upload_failed(self):
        events = []

        async def emit(e):
            events.append(e)

        self.tool.valves.ATTACH_TO_CHAT = False
        self.run_tool(FakeNet(), self.tool.record_voice_memo, "Hi", "T", "", emit)
        self.tool.valves.ATTACH_TO_CHAT = True
        self.run_tool(FakeNet(upload=Resp(401, payload={})), self.tool.record_voice_memo, "Hi", "T", "", emit)
        self.assertFalse([e for e in events if e["type"] == "files"])

    def test_full_link_for_email(self):
        out = self.run_tool(FakeNet(), self.tool.record_voice_memo, "Hi", "T")
        self.assertIn("https://open-webui.dimension-x.net/api/v1/files/abc123/content", out)
        self.assertIn("logged in", out)

    def test_full_link_omitted_when_base_empty(self):
        self.tool.valves.PUBLIC_BASE_URL = ""
        out = self.run_tool(FakeNet(), self.tool.record_voice_memo, "Hi", "T")
        self.assertNotIn("Full link", out)

    def test_request_uses_bare_model_default_voice_and_mp3(self):
        fake = FakeNet()
        self.run_tool(fake, self.tool.record_voice_memo, "Hi", "", "none")
        url, kw = fake.calls[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/audio/speech")
        self.assertEqual(kw["json"]["model"], "fish-audio/s2.1-pro-free:free")
        self.assertEqual(kw["json"]["voice"], self.tool.valves.DEFAULT_VOICE)
        self.assertEqual(kw["json"]["response_format"], "mp3")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer or-key")

    def test_explicit_voice_is_passed(self):
        fake = FakeNet()
        self.run_tool(fake, self.tool.record_voice_memo, "Hi", "", "deadbeef" * 4)
        self.assertEqual(fake.calls[0][1]["json"]["voice"], "deadbeef" * 4)

    def test_empty_and_too_long_text(self):
        self.assertIn("text is empty", self.run_tool(FakeNet(), self.tool.record_voice_memo, "  "))
        self.tool.valves.MAX_CHARS = 5
        out = self.run_tool(FakeNet(), self.tool.record_voice_memo, "far too long")
        self.assertIn("limit is 5", out)

    def test_tts_400_explains_retry_without_voice(self):
        fake = FakeNet(tts=Resp(400, payload={"error": {"message": "Model x does not exist", "code": 400}}))
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hi")
        self.assertIn("Model x does not exist", out)
        self.assertIn("without it", out)
        self.assertFalse(Path(self.tool.valves.MEMO_DIR).exists())

    def test_daily_free_limit_says_do_not_retry(self):
        msg = "Rate limit exceeded: free-models-per-day-high-balance"
        fake = FakeNet(tts=Resp(429, payload={"error": {"message": msg, "code": 429}}))
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hi")
        self.assertIn(msg, out)
        self.assertIn("Do not retry", out)

    def test_plain_429_still_retries_once(self):
        fake = FakeNet(tts=Resp(429, payload={"error": {"message": "Too many requests", "code": 429}}))
        self.assertIn("Retry once", self.run_tool(fake, self.tool.record_voice_memo, "Hi"))

    def test_missing_openrouter_key(self):
        self.tool.valves.OPENROUTER_API_KEY = ""
        out = self.run_tool(FakeNet(), self.tool.record_voice_memo, "Hi")
        self.assertIn("OPENROUTER_API_KEY", out)
        self.assertIn("configuration problem", out)

    def test_upload_failure_still_saves_and_says_so(self):
        fake = FakeNet(upload=Resp(401, payload={"detail": "bad key"}))
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hi")
        self.assertIn("No playable link", out)
        self.assertEqual(len(list(Path(self.tool.valves.MEMO_DIR).glob("*.mp3"))), 1)

    def test_upload_disabled(self):
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False
        fake = FakeNet()
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hi")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("turned off", out)


class ListTests(Base):
    def test_empty_then_listing(self):
        self.assertIn("No voice memos", asyncio.run(self.tool.list_voice_memos()))
        self.run_tool(FakeNet(), self.tool.record_voice_memo, "First thing", "First")
        out = asyncio.run(self.tool.list_voice_memos("5"))
        self.assertIn("First", out)
        self.assertIn("[play](/api/v1/files/abc123/content)", out)
        self.assertIn("full link: https://open-webui.dimension-x.net/api/v1/files/abc123/content", out)

    def test_bad_limit_falls_back(self):
        self.run_tool(FakeNet(), self.tool.record_voice_memo, "x", "T")
        self.assertIn("T", asyncio.run(self.tool.list_voice_memos("lots")))


class HelpTests(Base):
    def test_help_mentions_methods_and_limit(self):
        out = asyncio.run(self.tool.voice_memo_help())
        self.assertIn("record_voice_memo", out)
        self.assertIn("list_voice_memos", out)
        self.assertIn(str(self.tool.valves.MAX_CHARS), out)


class SlugTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(mod.slugify("Hello, World!"), "hello-world")
        self.assertEqual(mod.slugify("!!!"), "memo")


if __name__ == "__main__":
    unittest.main()
