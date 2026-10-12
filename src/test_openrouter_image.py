"""
Tests for openrouter_image.py. Run with: python -m unittest test_openrouter_image -v
(no OpenRouter or Open WebUI needed - requests.post/get are replaced with fakes).
"""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openrouter_image as mod

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


class Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mod.requests.HTTPError(f"HTTP {self.status_code}", response=self)


def ok_body(n=1, cost=0.0042):
    return {"id": "gen-1", "data": [{"b64_json": base64.b64encode(PNG).decode()} for _ in range(n)], "usage": {"cost": cost}}


class FakeNet:
    def __init__(self, images=None, upload=None):
        self.images = images or Resp(200, ok_body())
        self.upload = upload or Resp(200, {"id": "img123"})
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        return self.images if url.endswith("/images") else self.upload


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tool = mod.Tools()
        mod._CHAT_FILES.clear()
        self.tool.valves.OUTPUT_DIR = str(Path(self.tmp.name) / "imgs")
        self.tool.valves.OPENROUTER_API_KEY = "or-key"
        self.tool.valves.OPEN_WEBUI_API_KEY = "owui-key"

    def run_tool(self, fake, *a, **kw):
        with mock.patch.object(mod.requests, "post", fake):
            return asyncio.run(self.tool.generate_muse_image(*a, **kw))

    def run_adv(self, fake, *a, **kw):
        self.tool.valves.MODEL_CAN_SET_OPTIONS = True
        with mock.patch.object(mod.requests, "post", fake):
            return asyncio.run(self.tool.generate_muse_image_advanced(*a, **kw))


class GenerateTests(Base):
    def test_success(self):
        fake = FakeNet()
        out = self.run_tool(fake, "A red fox in snow")
        self.assertTrue(out["success"])
        self.assertIn("![generated image](/api/v1/files/img123/content)", out["image_markdown"])
        self.assertEqual(out["cost_usd"], 0.0042)
        self.assertEqual(out["share_url"], "https://open-webui.dimension-x.net/api/v1/files/img123/content")
        self.assertEqual(len(out["filenames"]), 1)
        files = sorted(p.suffix for p in Path(self.tool.valves.OUTPUT_DIR).iterdir())
        self.assertEqual(files, [".json", ".png"])
        meta = json.loads(next(Path(self.tool.valves.OUTPUT_DIR).glob("*.json")).read_text())
        self.assertEqual((meta["prompt"], meta["generation_id"]), ("A red fox in snow", "gen-1"))

    def test_request_body_and_headers(self):
        fake = FakeNet()
        self.run_tool(fake, "A fox")
        url, kw = fake.calls[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/images")
        self.assertEqual(kw["json"], {"model": "meta/muse-image", "prompt": "A fox"})
        self.assertEqual(kw["headers"]["Authorization"], "Bearer or-key")

    def test_request_extra_merged_but_cannot_override_model_or_prompt(self):
        self.tool.valves.REQUEST_EXTRA = '{"aspect_ratio": "16:9", "model": "evil", "prompt": "evil"}'
        fake = FakeNet()
        self.run_tool(fake, "A fox")
        self.assertEqual(fake.calls[0][1]["json"], {"aspect_ratio": "16:9", "model": "meta/muse-image", "prompt": "A fox"})

    def test_bad_request_extra(self):
        self.tool.valves.REQUEST_EXTRA = "{nope"
        fake = FakeNet()
        out = self.run_tool(fake, "A fox")
        self.assertFalse(out["success"])
        self.assertIn("REQUEST_EXTRA", out["error"])
        self.assertEqual(fake.calls, [])

    def test_url_style_response_is_downloaded(self):
        fake = FakeNet(images=Resp(200, {"data": [{"url": "https://cdn.example/x.png"}]}))
        with mock.patch.object(mod.requests, "get", return_value=Resp(200, content=PNG)):
            out = self.run_tool(fake, "A fox")
        self.assertTrue(out["success"])

    def test_empty_and_too_long_prompt(self):
        self.assertIn("prompt is empty", self.run_tool(FakeNet(), "  ")["error"])
        self.tool.valves.MAX_PROMPT_CHARS = 5
        self.assertIn("limit is 5", self.run_tool(FakeNet(), "far too long")["error"])

    def test_402_says_no_retry(self):
        fake = FakeNet(images=Resp(402, {"error": {"message": "Insufficient credits", "code": 402}}))
        out = self.run_tool(fake, "A fox")
        self.assertFalse(out["success"])
        self.assertIn("Insufficient credits", out["error"])
        self.assertIn("retrying will not help", out["error"])

    def test_400_content_block_allows_one_rewrite(self):
        fake = FakeNet(images=Resp(400, {"error": {"message": "blocked", "code": 400}}))
        self.assertIn("rewritten", self.run_tool(fake, "A fox")["error"])

    def test_missing_key(self):
        self.tool.valves.OPENROUTER_API_KEY = ""
        out = self.run_tool(FakeNet(), "A fox")
        self.assertIn("OPENROUTER_API_KEY", out["error"])

    def test_empty_data_is_an_error(self):
        out = self.run_tool(FakeNet(images=Resp(200, {"data": []})), "A fox")
        self.assertFalse(out["success"])
        self.assertIn("no image", out["error"])

    def test_upload_failure_warns_and_says_do_not_retry(self):
        fake = FakeNet(upload=Resp(401, {"detail": "bad key"}))
        out = self.run_tool(fake, "A fox")
        self.assertFalse(out["success"])
        self.assertIn("generated (and billed)", out["error"])
        self.assertIn("do not call this tool again", out["error"])
        self.assertEqual(len(list(Path(self.tool.valves.OUTPUT_DIR).glob("*.png"))), 1)

    def test_files_event_delivery(self):
        self.tool.valves.IMAGE_DELIVERY = "files_event"
        events = []

        async def emit(e):
            events.append(e)

        out = self.run_tool(FakeNet(), "A fox", __event_emitter__=emit)
        self.assertNotIn("image_markdown", out)
        files = [e for e in events if e["type"] == "files"]
        f = files[0]["data"]["files"][0]
        self.assertEqual((f["type"], f["id"], f["url"]), ("image", "img123", "/api/v1/files/img123/content"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("note", out)

    def test_no_local_copy(self):
        self.tool.valves.SAVE_LOCAL_COPY = False
        self.run_tool(FakeNet(), "A fox")
        self.assertFalse(Path(self.tool.valves.OUTPUT_DIR).exists())


class OptionTests(Base):
    def test_options_are_sent(self):
        fake = FakeNet()
        out = self.run_adv(fake, "A fox", "16:9", "2k", "JPG", "42")
        self.assertTrue(out["success"])
        self.assertEqual(
            fake.calls[0][1]["json"],
            {"aspect_ratio": "16:9", "resolution": "2K", "output_format": "jpeg", "seed": 42, "model": "meta/muse-image", "prompt": "A fox"},
        )

    def test_unset_options_are_not_sent(self):
        fake = FakeNet()
        self.run_adv(fake, "A fox", "none", "", "default", "null", "")
        self.assertEqual(fake.calls[0][1]["json"], {"model": "meta/muse-image", "prompt": "A fox"})

    def test_bad_option_lists_choices_and_makes_no_call(self):
        fake = FakeNet()
        out = self.run_adv(fake, "A fox", "wide")
        self.assertFalse(out["success"])
        self.assertIn("16:9", out["error"])
        self.assertEqual(fake.calls, [])
        self.assertIn("whole number", self.run_adv(fake, "A fox", "", "", "", "abc")["error"])

    def test_reference_from_owui_link_becomes_data_url(self):
        fake = FakeNet()
        with mock.patch.object(mod.requests, "get", return_value=Resp(200, content=PNG)) as g:
            self.run_adv(fake, "Make it night", "", "", "", "", "/api/v1/files/abc/content")
        self.assertTrue(g.call_args[0][0].endswith("/api/v1/files/abc/content"))
        ref = fake.calls[0][1]["json"]["input_references"][0]
        self.assertEqual(ref["type"], "image_url")
        self.assertTrue(ref["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_reference_from_local_filename_and_no_path_tricks(self):
        d = Path(self.tool.valves.OUTPUT_DIR)
        d.mkdir(parents=True)
        (d / "old.png").write_bytes(PNG)
        fake = FakeNet()
        self.run_adv(fake, "Edit", "", "", "", "", "../../etc/old.png")
        self.assertEqual(len(fake.calls[0][1]["json"]["input_references"]), 1)

    def test_missing_reference_is_a_clear_error(self):
        out = self.run_adv(FakeNet(), "Edit", "", "", "", "", "nope.png")
        self.assertFalse(out["success"])
        self.assertIn("not found", out["error"])

    def test_too_many_references(self):
        self.tool.valves.MAX_REFERENCES = 1
        out = self.run_adv(FakeNet(), "Edit", "", "", "", "", "a.png,b.png")
        self.assertIn("At most 1", out["error"])

    def test_sidecar_omits_reference_bytes(self):
        d = Path(self.tool.valves.OUTPUT_DIR)
        d.mkdir(parents=True)
        (d / "old.png").write_bytes(PNG)
        self.run_adv(FakeNet(), "Edit", "1:1", "", "", "", "old.png")
        meta = json.loads(next(p for p in d.glob("*.json")).read_text())
        self.assertEqual(meta["references"], 1)
        self.assertNotIn("input_references", meta["extra"])
        self.assertEqual(meta["extra"]["aspect_ratio"], "1:1")


class ValveTests(Base):
    def test_simple_method_takes_only_a_prompt(self):
        import inspect

        params = [p for p in inspect.signature(mod.Tools.generate_muse_image).parameters if not p.startswith("__") and p != "self"]
        self.assertEqual(params, ["prompt"])

    def test_usage_guidance_is_in_the_description_open_webui_keeps(self):
        import re

        # Open WebUI keeps only the docstring text before the first :param / :return: line.
        desc = re.split(":(param|return)", mod.Tools.generate_muse_image.__doc__, 1)[0]
        self.assertIn("image_markdown", desc)
        self.assertIn("share_url", desc)

    def test_default_valves_are_applied(self):
        v = self.tool.valves
        v.DEFAULT_ASPECT_RATIO, v.DEFAULT_RESOLUTION, v.DEFAULT_OUTPUT_FORMAT, v.DEFAULT_SEED = "3:2", "2K", "webp", "7"
        fake = FakeNet()
        self.run_tool(fake, "A fox")
        self.assertEqual(
            fake.calls[0][1]["json"],
            {"aspect_ratio": "3:2", "resolution": "2K", "output_format": "webp", "seed": 7, "model": "meta/muse-image", "prompt": "A fox"},
        )

    def test_default_reference_images_are_sent(self):
        d = Path(self.tool.valves.OUTPUT_DIR)
        d.mkdir(parents=True)
        (d / "style.png").write_bytes(PNG)
        self.tool.valves.DEFAULT_REFERENCE_IMAGES = "style.png"
        fake = FakeNet()
        self.run_tool(fake, "A fox")
        self.assertEqual(len(fake.calls[0][1]["json"]["input_references"]), 1)

    def test_call_values_override_valve_defaults(self):
        self.tool.valves.DEFAULT_ASPECT_RATIO = "3:2"
        self.tool.valves.DEFAULT_SEED = "7"
        fake = FakeNet()
        self.run_adv(fake, "A fox", "1:1", "", "", "9")
        body = fake.calls[0][1]["json"]
        self.assertEqual((body["aspect_ratio"], body["seed"]), ("1:1", 9))

    def test_advanced_refuses_when_valve_off(self):
        fake = FakeNet()
        with mock.patch.object(mod.requests, "post", fake):
            out = asyncio.run(self.tool.generate_muse_image_advanced("A fox", "1:1"))
        self.assertFalse(out["success"])
        self.assertIn("generate_muse_image", out["error"])
        self.assertEqual(fake.calls, [])

    def test_simple_method_works_while_advanced_is_off(self):
        self.assertFalse(self.tool.valves.MODEL_CAN_SET_OPTIONS)
        self.assertTrue(self.run_tool(FakeNet(), "A fox")["success"])

    def test_bad_valve_value_is_reported(self):
        self.tool.valves.DEFAULT_SEED = "abc"
        self.assertIn("whole number", self.run_tool(FakeNet(), "A fox")["error"])


class AttachTests(Base):
    def collect(self, **valves):
        for k, val in valves.items():
            setattr(self.tool.valves, k, val)
        events = []

        async def emit(e):
            events.append(e)

        out = self.run_tool(FakeNet(), "A fox", __event_emitter__=emit)
        return out, [e for e in events if e["type"] == "files"], events

    def test_image_is_attached_to_files_tab_by_default(self):
        out, files, _ = self.collect()
        self.assertEqual(len(files), 1)
        f = files[0]["data"]["files"][0]
        self.assertEqual((f["type"], f["id"], f["url"]), ("file", "img123", "/api/v1/files/img123/content"))
        self.assertTrue(f["name"].endswith(".png"))
        self.assertIn("image_markdown", out)  # model_embed still shows it in the reply

    def test_attach_off(self):
        _, files, _ = self.collect(ATTACH_TO_CHAT=False)
        self.assertEqual(files, [])

    def test_message_event_mode_attaches_and_appends_markdown(self):
        _, files, events = self.collect(IMAGE_DELIVERY="message_event")
        self.assertEqual(len(files), 1)
        self.assertEqual(len([e for e in events if e["type"] == "message"]), 1)

    def test_share_url_is_returned(self):
        out, _, _ = self.collect()
        self.assertEqual(out["share_url"], "https://open-webui.dimension-x.net/api/v1/files/img123/content")

    def test_result_is_small_and_has_no_note_or_embeddable_urls(self):
        out, _, _ = self.collect(IMAGE_DELIVERY="files_event")
        self.assertEqual(set(out), {"success", "filenames", "share_url", "cost_usd"})
        out, _, _ = self.collect(IMAGE_DELIVERY="model_embed")
        self.assertEqual(set(out), {"success", "filenames", "share_url", "cost_usd", "image_markdown"})

    def test_no_share_url_without_public_base(self):
        out, _, _ = self.collect(PUBLIC_BASE_URL="")
        self.assertNotIn("share_url", out)


class MultiImageTests(Base):
    def calls(self, ids):
        events = []

        async def emit(e):
            events.append(e)

        for mid in ids:
            self.run_tool(FakeNet(upload=Resp(200, {"id": f"img-{len(events)}"})), "A fox", __event_emitter__=emit, __message_id__=mid)
        return events

    def test_second_image_in_same_message_sends_the_full_list_live(self):
        events = self.calls(["m1", "m1"])
        saved = [e for e in events if e["type"] == "files"]
        live = [e for e in events if e["type"] == "chat:message:files"]
        self.assertEqual([len(e["data"]["files"]) for e in saved], [1, 1])  # backend merges these itself
        self.assertEqual(len(live), 1)
        self.assertEqual(len(live[0]["data"]["files"]), 2)
        self.assertEqual(events.index(live[0]) > events.index(saved[1]), True)  # full list comes after the saving event

    def test_third_image_sends_all_three(self):
        live = [e for e in self.calls(["m1"] * 3) if e["type"] == "chat:message:files"]
        self.assertEqual([len(e["data"]["files"]) for e in live], [2, 3])

    def test_different_messages_do_not_mix(self):
        events = self.calls(["m1", "m2"])
        self.assertFalse([e for e in events if e["type"] == "chat:message:files"])

    def test_no_message_id_still_attaches_without_live_list(self):
        events = self.calls([None, None])
        self.assertEqual(len([e for e in events if e["type"] == "files"]), 2)
        self.assertFalse([e for e in events if e["type"] == "chat:message:files"])

    def test_message_id_is_hidden_from_the_model(self):
        import inspect

        for fn in (mod.Tools.generate_muse_image, mod.Tools.generate_muse_image_advanced):
            self.assertIn("__message_id__", inspect.signature(fn).parameters)

    def test_memory_is_bounded(self):
        self.calls([f"m{i}" for i in range(mod._CHAT_FILES_KEEP + 10)])
        self.assertLessEqual(len(mod._CHAT_FILES), mod._CHAT_FILES_KEEP)


class HelperTests(unittest.TestCase):
    def test_image_ext(self):
        self.assertEqual(mod.image_ext(PNG)[0], "png")
        self.assertEqual(mod.image_ext(b"\xff\xd8\xff\xe0")[0], "jpg")
        self.assertEqual(mod.image_ext(b"RIFF\x00\x00\x00\x00WEBPxx")[0], "webp")

    def test_slugify(self):
        self.assertEqual(mod.slugify("A Red Fox, in snow!"), "a-red-fox-in-snow")
        self.assertEqual(mod.slugify("!!!"), "image")


if __name__ == "__main__":
    unittest.main()
