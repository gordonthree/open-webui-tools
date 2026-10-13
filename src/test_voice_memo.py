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
    def __init__(self, tts=None, upload=None, chat=None):
        self.tts = tts or Resp(200, b"ID3audio", headers={"Content-Type": "audio/mpeg"})
        self.upload = upload or Resp(200, payload={"id": "abc123"})
        self.chat = chat
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if url.endswith("/chat/completions"):
            if isinstance(self.chat, Exception):
                raise self.chat
            return self.chat
        return self.tts if url.endswith(("/audio/speech", "/v1/tts")) else self.upload

    def tts_input(self):
        return next(kw["json"]["input"] for url, kw in self.calls if url.endswith("/audio/speech"))


def chat_reply(text, status=200):
    if status != 200:
        return Resp(status, payload={"error": {"message": "nope"}})
    return Resp(200, payload={"choices": [{"message": {"content": text}}]})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tool = mod.Tools()
        self.tool.valves.MEMO_DIR = str(Path(self.tmp.name) / "memos")
        self.tool.valves.OPENROUTER_API_KEY = "or-key"
        self.tool.valves.OPEN_WEBUI_API_KEY = "owui-key"
        self.tool.valves.FORMATTER_MODEL = ""

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


class EventLoopTests(Base):
    def test_blocking_http_calls_do_not_run_on_the_event_loop(self):
        import threading

        seen = []

        def fake(url, **kw):
            seen.append(threading.current_thread() is threading.main_thread())
            return FakeNet()(url, **kw)

        with mock.patch.object(mod.requests, "post", fake):
            asyncio.run(self.tool.record_voice_memo("Hi", "T"))
        self.assertEqual(len(seen), 2)
        self.assertFalse(any(seen), "requests.post ran on the event loop thread")


class FormatterTests(Base):
    def setUp(self):
        super().setUp()
        self.tool.valves.FORMATTER_MODEL = "mistralai/mistral-small-24b-instruct-2501"

    def record(self, fake, text="Hey there. I miss you a lot."):
        out = self.run_tool(fake, self.tool.record_voice_memo, text, "T")
        return out

    def test_tagged_text_is_what_gets_spoken_and_original_is_kept(self):
        fake = FakeNet(chat=chat_reply("[sad] Hey there. [sighing] I miss you a lot."))
        out = self.record(fake)
        self.assertEqual(fake.tts_input(), "[sad] Hey there. [sighing] I miss you a lot.")
        meta = json.loads(next(Path(self.tool.valves.MEMO_DIR).glob("*.json")).read_text())
        self.assertEqual(meta["text"], "Hey there. I miss you a lot.")
        self.assertEqual(meta["spoken_text"], "[sad] Hey there. [sighing] I miss you a lot.")
        self.assertNotIn("[sad]", out)  # the result must not hand the model tagged text to imitate

    def test_formatter_request_shape(self):
        fake = FakeNet(chat=chat_reply("[sad] Hey there. I miss you a lot."))
        self.record(fake)
        url, kw = fake.calls[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(kw["json"]["model"], "mistralai/mistral-small-24b-instruct-2501")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer or-key")
        self.assertEqual(kw["json"]["messages"][0]["role"], "system")
        self.assertIn("[whispering]", kw["json"]["messages"][0]["content"])
        self.assertEqual(kw["json"]["messages"][1]["content"], "Hey there. I miss you a lot.")

    def test_custom_prompt_replaces_builtin(self):
        self.tool.valves.FORMATTER_PROMPT = "Custom rules."
        fake = FakeNet(chat=chat_reply("[sad] Hey there. I miss you a lot."))
        self.record(fake)
        self.assertEqual(fake.calls[0][1]["json"]["messages"][0]["content"], "Custom rules.")

    def test_off_when_model_empty_and_no_call_made(self):
        self.tool.valves.FORMATTER_MODEL = ""
        fake = FakeNet()
        self.record(fake)
        self.assertEqual([u for u, _ in fake.calls if u.endswith("/chat/completions")], [])

    def test_already_tagged_text_is_not_sent_to_formatter(self):
        fake = FakeNet(chat=chat_reply("never used"))
        self.record(fake, "[happy] Hello there.")
        self.assertEqual([u for u, _ in fake.calls if u.endswith("/chat/completions")], [])
        self.assertEqual(fake.tts_input(), "[happy] Hello there.")

    def test_formatter_http_error_falls_back_to_original(self):
        fake = FakeNet(chat=chat_reply("", status=429))
        out = self.record(fake)
        self.assertEqual(fake.tts_input(), "Hey there. I miss you a lot.")
        self.assertIn("Recorded", out)

    def test_formatter_network_error_and_garbage_fall_back(self):
        for chat in (mod.requests.ConnectionError("down"), Resp(200, payload={"unexpected": 1}), chat_reply("")):
            fake = FakeNet(chat=chat)
            self.record(fake)
            self.assertEqual(fake.tts_input(), "Hey there. I miss you a lot.")

    def test_rewritten_words_fall_back_to_original(self):
        fake = FakeNet(chat=chat_reply("[sad] Oh hello. Truly I am missing you terribly, darling."))
        self.record(fake)
        self.assertEqual(fake.tts_input(), "Hey there. I miss you a lot.")

    def test_runaway_output_falls_back(self):
        fake = FakeNet(chat=chat_reply("[sad] Hey there. I miss you a lot. " + "[sighing] " * 40))
        self.record(fake)
        self.assertEqual(fake.tts_input(), "Hey there. I miss you a lot.")

    def test_fences_quotes_are_stripped(self):
        fake = FakeNet(chat=chat_reply('```\n"[sad] Hey there. I miss you a lot."\n```'))
        self.record(fake)
        self.assertEqual(fake.tts_input(), "[sad] Hey there. I miss you a lot.")

    def test_asterisk_action_turned_into_tag_is_accepted(self):
        fake = FakeNet(chat=chat_reply("[laughing] Hey there. I miss you a lot."))
        self.record(fake, "*soft laugh* Hey there. I miss you a lot.")
        self.assertEqual(fake.tts_input(), "[laughing] Hey there. I miss you a lot.")

    def test_strict_setting_rejects_any_word_change(self):
        self.tool.valves.FORMATTER_MIN_WORD_MATCH = 1.0
        fake = FakeNet(chat=chat_reply("[sad] Hey there. I really miss you a lot."))
        self.record(fake)
        self.assertEqual(fake.tts_input(), "Hey there. I miss you a lot.")

    def test_formatter_runs_off_the_event_loop(self):
        import threading

        seen = []

        def fake(url, **kw):
            seen.append(threading.current_thread() is threading.main_thread())
            return FakeNet(chat=chat_reply("[sad] Hey there. I miss you a lot."))(url, **kw)

        with mock.patch.object(mod.requests, "post", fake):
            asyncio.run(self.tool.record_voice_memo("Hey there. I miss you a lot.", "T"))
        self.assertEqual(len(seen), 3)
        self.assertFalse(any(seen))


class MultiTTS(FakeNet):
    """Fake network that returns different audio per voice so joined order can be checked."""

    def __call__(self, url, **kw):
        if url.endswith("/audio/speech"):
            self.calls.append((url, kw))
            return Resp(200, b"\xff\xfb" + kw["json"]["voice"][:4].encode() + b"|", headers={"Content-Type": "audio/mpeg"})
        return super().__call__(url, **kw)


def fish_calls(fake):
    return [kw for url, kw in fake.calls if url.endswith("/v1/tts")]


MARA, HANNAH = "67f1f9b08fef4fbc957cd2a93c45918c", "cffb6be7e50f40e2999b224b16bd4cc0"


class ConversationTests(Base):
    def convo(self, script, fake=None, title=""):
        fake = fake or MultiTTS()
        out = self.run_tool(fake, self.tool.record_conversation, script, title)
        return fake, out

    def speech_calls(self, fake):
        return [kw["json"] for url, kw in fake.calls if url.endswith("/audio/speech")]

    def test_each_speaker_gets_own_voice_and_clips_are_joined_in_order(self):
        fake, out = self.convo("Mara: Hi there.\nHannah: Hello!\nMara: Bye now.")
        calls = self.speech_calls(fake)
        self.assertEqual([c["voice"] for c in calls], [MARA, HANNAH, MARA])
        self.assertEqual([c["input"] for c in calls], ["Hi there.", "Hello!", "Bye now."])
        mp3 = next(Path(self.tool.valves.MEMO_DIR).glob("*.mp3")).read_bytes()
        self.assertEqual(mp3, b"\xff\xfb67f1|\xff\xfbcffb|\xff\xfb67f1|")
        self.assertIn("[Play conversation](/api/v1/files/abc123/content)", out)

    def test_same_speaker_neighbours_merge_and_continuation_lines_join(self):
        fake, _ = self.convo("Mara: One.\nTwo.\nMara: Three.\nHannah: Four.")
        calls = self.speech_calls(fake)
        self.assertEqual([c["input"] for c in calls], ["One. Two. Three.", "Four."])

    def test_markdown_labels_and_case_are_tolerated(self):
        fake, _ = self.convo("**Mara:** Hi.\n- HANNAH: Yo.")
        self.assertEqual([c["voice"] for c in self.speech_calls(fake)], [MARA, HANNAH])

    def test_unknown_speaker_uses_default_voice_with_note(self):
        fake, out = self.convo("Bob: Hi.\nMara: Hey.")
        self.assertEqual(self.speech_calls(fake)[0]["voice"], self.tool.valves.DEFAULT_VOICE)
        self.assertIn("no voice is set for Bob", out)

    def test_script_without_names_is_rejected_with_format_hint(self):
        fake, out = self.convo("Just some words with no speaker.")
        self.assertIn("first line has no speaker name", out)
        self.assertIn("Mara: Hello there.", out)
        self.assertEqual(self.speech_calls(fake), [])

    def test_too_many_turns_or_characters(self):
        self.tool.valves.MAX_CONVERSATION_LINES = 2
        _, out = self.convo("Mara: a.\nHannah: b.\nMara: c.")
        self.assertIn("3 turns; the limit is 2", out)
        self.tool.valves.MAX_CHARS = 5
        _, out = self.convo("Mara: far too long")
        self.assertIn("limit is 5", out)

    def test_failure_names_the_line_and_saves_nothing(self):
        class Fail(MultiTTS):
            def __call__(self, url, **kw):
                if url.endswith("/audio/speech") and len([c for c in self.calls if c[0].endswith("/audio/speech")]) == 1:
                    self.calls.append((url, kw))
                    return Resp(429, payload={"error": {"message": "Too many requests", "code": 429}})
                return super().__call__(url, **kw)

        _, out = self.convo("Mara: a.\nHannah: b.", Fail())
        self.assertIn("line 2 of 2 (Hannah)", out)
        self.assertIn("Retry once", out)
        self.assertFalse(Path(self.tool.valves.MEMO_DIR).exists())

    def test_bad_speaker_voices_valve_is_a_config_error(self):
        self.tool.valves.SPEAKER_VOICES = "not json"
        _, out = self.convo("Mara: hi")
        self.assertIn("SPEAKER_VOICES", out)
        self.assertIn("configuration problem", out)

    def test_formatter_runs_per_line_and_sidecar_keeps_both_texts(self):
        self.tool.valves.FORMATTER_MODEL = "m"
        fake = MultiTTS(chat=chat_reply("[happy] Hi there."))
        self.convo("Mara: Hi there.", fake)
        self.assertEqual(self.speech_calls(fake)[0]["input"], "[happy] Hi there.")
        meta = json.loads(next(Path(self.tool.valves.MEMO_DIR).glob("*.json")).read_text())
        self.assertEqual(meta["text"], "Mara: Hi there.")
        self.assertEqual(meta["spoken_text"], "Mara: [happy] Hi there.")
        self.assertEqual(meta["speakers"], {"Mara": MARA})

    def test_help_lists_conversation_and_speakers(self):
        out = asyncio.run(self.tool.voice_memo_help())
        self.assertIn("record_conversation", out)
        self.assertIn("mara, hannah", out)


class Mp3JoinTests(unittest.TestCase):
    def test_id3_tags_are_stripped_before_joining(self):
        id3v2 = b"ID3\x04\x00\x00\x00\x00\x00\x04" + b"JUNK"
        id3v1 = b"TAG" + b"\x00" * 125
        joined = mod.join_mp3([b"\xff\xfbA" + id3v1, id3v2 + b"\xff\xfbB"])
        self.assertEqual(joined, b"\xff\xfbA\xff\xfbB")

    def test_runs_off_event_loop(self):
        import threading

        seen = []

        def fake(url, **kw):
            seen.append(threading.current_thread() is threading.main_thread())
            return MultiTTS()(url, **kw)

        tool = mod.Tools()
        with tempfile.TemporaryDirectory() as d:
            tool.valves.MEMO_DIR = d
            tool.valves.OPENROUTER_API_KEY = "k"
            tool.valves.OPEN_WEBUI_API_KEY = "k"
            tool.valves.FORMATTER_MODEL = ""
            with mock.patch.object(mod.requests, "post", fake):
                asyncio.run(tool.record_conversation("Mara: a.\nHannah: b."))
        self.assertEqual(len(seen), 3)
        self.assertFalse(any(seen))


class FishBackendTests(Base):
    def setUp(self):
        super().setUp()
        self.tool.valves.FISH_API_KEY = "fish-key"

    def test_single_memo_uses_fish_request_shape(self):
        fake = FakeNet()
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hello there.", "T")
        self.assertEqual([u for u, _ in fake.calls if "openrouter" in u], [])
        kw = fish_calls(fake)[0]
        self.assertEqual(fake.calls[0][0], "https://api.fish.audio/v1/tts")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer fish-key")
        self.assertEqual(kw["headers"]["model"], "s2.1-pro-free")
        self.assertEqual(kw["json"], {"text": "Hello there.", "format": "mp3", "reference_id": self.tool.valves.DEFAULT_VOICE})
        self.assertIn("[Play T](/api/v1/files/abc123/content)", out)
        meta = json.loads(next(Path(self.tool.valves.MEMO_DIR).glob("*.json")).read_text())
        self.assertIn("Fish Audio API", meta["model"])

    def test_no_fish_key_keeps_openrouter_path(self):
        self.tool.valves.FISH_API_KEY = ""
        fake = FakeNet()
        self.run_tool(fake, self.tool.record_voice_memo, "Hello there.", "T")
        self.assertEqual(fish_calls(fake), [])
        self.assertTrue(fake.calls[0][0].endswith("/audio/speech"))

    def test_fish_only_works_without_openrouter_key(self):
        self.tool.valves.OPENROUTER_API_KEY = ""
        out = self.run_tool(FakeNet(), self.tool.record_voice_memo, "Hello there.", "T")
        self.assertIn("Recorded", out)

    def test_conversation_is_one_request_with_tags_and_voice_array(self):
        fake = FakeNet()
        out = self.run_tool(fake, self.tool.record_conversation, "Mara: Hi.\nHannah: Hello!\nMara: Bye.")
        calls = fish_calls(fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["json"]["text"], "<|speaker:0|>Hi. <|speaker:1|>Hello! <|speaker:0|>Bye.")
        self.assertEqual(calls[0]["json"]["reference_id"], [MARA, HANNAH])
        meta = json.loads(next(Path(self.tool.valves.MEMO_DIR).glob("*.json")).read_text())
        self.assertEqual(meta["mode"], "one request")
        self.assertIn("[Play conversation]", out)

    def test_speaker_case_is_one_speaker(self):
        fake = FakeNet()
        self.run_tool(fake, self.tool.record_conversation, "Mara: Hi.\nHannah: Yo.\nMARA: Bye.")
        self.assertEqual(fish_calls(fake)[0]["json"]["text"], "<|speaker:0|>Hi. <|speaker:1|>Yo. <|speaker:0|>Bye.")
        self.assertEqual(len(fish_calls(fake)[0]["json"]["reference_id"]), 2)

    def test_unknown_speaker_gets_default_voice_in_array(self):
        fake = FakeNet()
        out = self.run_tool(fake, self.tool.record_conversation, "Mara: Hi.\nBob: Hey.")
        self.assertEqual(fish_calls(fake)[0]["json"]["reference_id"], [MARA, self.tool.valves.DEFAULT_VOICE])
        self.assertIn("no voice is set for Bob", out)

    def test_native_off_joins_clips_through_fish(self):
        self.tool.valves.NATIVE_MULTI_SPEAKER = False
        fake = FakeNet()
        self.run_tool(fake, self.tool.record_conversation, "Mara: Hi.\nHannah: Yo.")
        calls = fish_calls(fake)
        self.assertEqual([c["json"]["text"] for c in calls], ["Hi.", "Yo."])
        self.assertEqual([c["json"]["reference_id"] for c in calls], [MARA, HANNAH])

    def test_one_speaker_and_five_speakers_do_not_use_native(self):
        fake = FakeNet()
        self.run_tool(fake, self.tool.record_conversation, "Mara: Hi.\nMara: Again.")
        self.assertEqual([c["json"]["text"] for c in fish_calls(fake)], ["Hi. Again."])
        fake = FakeNet()
        self.run_tool(fake, self.tool.record_conversation, "A: 1.\nB: 2.\nC: 3.\nD: 4.\nE: 5.")
        self.assertEqual(len(fish_calls(fake)), 5)

    def test_formatter_tags_each_turn_inside_the_speaker_markers(self):
        self.tool.valves.FORMATTER_MODEL = "m"
        fake = FakeNet(chat=chat_reply("[happy] Hi."))
        self.run_tool(fake, self.tool.record_conversation, "Mara: Hi.\nHannah: Hi.")
        self.assertEqual(fish_calls(fake)[0]["json"]["text"], "<|speaker:0|>[happy] Hi. <|speaker:1|>[happy] Hi.")

    def test_errors_name_fish_audio(self):
        fake = FakeNet(tts=Resp(402, payload={"status": 402, "message": "Insufficient balance"}))
        out = self.run_tool(fake, self.tool.record_voice_memo, "Hi")
        self.assertIn("Fish Audio HTTP 402: Insufficient balance", out)
        self.assertIn("The Fish Audio account is out of credits", out)
        fake = FakeNet(tts=Resp(503, payload={"status": 503, "message": "High load"}))
        out = self.run_tool(fake, self.tool.record_conversation, "Mara: a.\nHannah: b.")
        self.assertIn("Fish Audio side", out)
        self.assertIn("Retry once", out)
        self.assertFalse(Path(self.tool.valves.MEMO_DIR).exists())

    def test_fish_calls_stay_off_the_event_loop(self):
        import threading

        seen = []

        def fake(url, **kw):
            seen.append(threading.current_thread() is threading.main_thread())
            return FakeNet()(url, **kw)

        with mock.patch.object(mod.requests, "post", fake):
            asyncio.run(self.tool.record_conversation("Mara: a.\nHannah: b."))
        self.assertEqual(len(seen), 2)
        self.assertFalse(any(seen))


class LeadInTests(unittest.TestCase):
    HDR = bytes([0xFF, 0xFB, 0x90, 0xC4])  # MPEG-1 Layer III, 128 kbps, 44.1 kHz, mono: 417-byte frames

    def clip(self):
        silent = self.HDR + b"\x00" * 413
        speech = self.HDR + b"\x8b" * 413
        return silent + speech

    def test_silence_is_added_by_repeating_the_silent_first_frame(self):
        out = mod.add_lead_in(self.clip(), 250)
        frames = 0.250 / (1152 / 44100)
        added = (len(out) - len(self.clip())) // 417
        self.assertEqual(added, round(frames))
        self.assertTrue(out.endswith(self.clip()))
        self.assertTrue(all(not any(out[i * 417 + 4 : (i + 1) * 417]) for i in range(added)))

    def test_off_when_zero_or_not_mp3_or_first_frame_not_silent(self):
        self.assertEqual(mod.add_lead_in(self.clip(), 0), self.clip())
        self.assertEqual(mod.add_lead_in(b"ID3audio", 250), b"ID3audio")
        loud = self.HDR + b"\x8b" * 413
        self.assertEqual(mod.add_lead_in(loud, 250), loud)
        self.assertEqual(mod.add_lead_in(self.HDR + b"\x00" * 10, 250), self.HDR + b"\x00" * 10)

    def test_saved_memo_gets_the_lead_in_and_valve_turns_it_off(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = mod.Tools()
        tool.valves.MEMO_DIR = tmp.name
        tool.valves.OPENROUTER_API_KEY = "k"
        tool.valves.OPEN_WEBUI_API_KEY = "k"
        tool.valves.FORMATTER_MODEL = ""
        resp = Resp(200, self.clip(), headers={"Content-Type": "audio/mpeg"})
        with mock.patch.object(mod.requests, "post", FakeNet(tts=resp)):
            asyncio.run(tool.record_voice_memo("Hi", "T"))
        saved = next(Path(tmp.name).glob("*.mp3")).read_bytes()
        self.assertGreater(len(saved), len(self.clip()))
        self.assertTrue(saved.endswith(self.clip()))
        for f in Path(tmp.name).iterdir():
            f.unlink()
        tool.valves.LEAD_IN_MS = 0
        with mock.patch.object(mod.requests, "post", FakeNet(tts=resp)):
            asyncio.run(tool.record_voice_memo("Hi", "T"))
        self.assertEqual(next(Path(tmp.name).glob("*.mp3")).read_bytes(), self.clip())


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


class ShowLastTests(Base):
    def test_nothing_recorded(self):
        self.assertIn("No voice memos", asyncio.run(self.tool.show_last_memo()))

    def test_shows_newest_with_both_texts_and_link(self):
        self.run_tool(FakeNet(), self.tool.record_voice_memo, "First one.", "First")
        self.tool.valves.FORMATTER_MODEL = "m"
        fake = FakeNet(chat=chat_reply("[sad] Second one here."))
        mod_time = mod.datetime
        with mock.patch.object(mod, "datetime", wraps=mod_time) as dt:
            dt.now.return_value = mod_time(2099, 1, 1, 0, 0, 0).astimezone()
            self.run_tool(fake, self.tool.record_voice_memo, "Second one here.", "Second")
        out = asyncio.run(self.tool.show_last_memo())
        self.assertIn("Title: Second", out)
        self.assertIn("Text given:\nSecond one here.", out)
        self.assertIn("[sad] Second one here.", out)
        self.assertIn("[Play](/api/v1/files/abc123/content)", out)
        self.assertIn("https://open-webui.dimension-x.net/api/v1/files/abc123/content", out)
        self.assertNotIn("First one", out)

    def test_same_text_says_no_tags_were_added(self):
        self.run_tool(FakeNet(), self.tool.record_voice_memo, "Plain words.", "P")
        out = asyncio.run(self.tool.show_last_memo())
        self.assertIn("the same as the text given", out)

    def test_help_mentions_it(self):
        self.assertIn("show_last_memo", asyncio.run(self.tool.voice_memo_help()))


class HelpTests(Base):
    def test_help_mentions_methods_and_limit(self):
        out = asyncio.run(self.tool.voice_memo_help())
        self.assertIn("record_voice_memo", out)
        self.assertIn("list_voice_memos", out)
        self.assertIn(str(self.tool.valves.MAX_CHARS), out)

    def test_help_teaches_bracket_tags_not_asterisks(self):
        out = asyncio.run(self.tool.voice_memo_help())
        self.assertIn("[happy]", out)
        self.assertIn("[whispering]", out)
        self.assertIn("Never use *asterisks*", out)


class SlugTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(mod.slugify("Hello, World!"), "hello-world")
        self.assertEqual(mod.slugify("!!!"), "memo")


if __name__ == "__main__":
    unittest.main()
