import asyncio
import base64
import json
import unittest
from io import BytesIO
from unittest.mock import patch

import describe_image as di
import vision_describe as vd


def png(size=(40, 30), color=(200, 30, 30), mode="RGB"):
    from PIL import Image

    out = BytesIO()
    Image.new(mode, size, color).save(out, format="PNG")
    return out.getvalue()


def data_url(raw):
    return "data:image/png;base64," + base64.b64encode(raw).decode()


class FakeResp:
    def __init__(self, status=200, body=None, text="", content=b""):
        self.status_code, self._body, self.text, self.content = status, body, text or json.dumps(body or {}), content

    def json(self):
        return self._body


class Recorder:
    """Stands in for requests.post/get on the vision module."""

    def __init__(self, answer="A red square.", status=200, text=""):
        self.answer, self.status, self.text, self.calls = answer, status, text, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self.status != 200:
            return FakeResp(self.status, text=self.text)
        return FakeResp(200, {"message": {"role": "assistant", "content": self.answer}})


def run(coro):
    return asyncio.run(coro)


class HelperTests(unittest.TestCase):
    def test_tidy(self):
        self.assertEqual(vd.tidy("<think>hmm</think>**A** red `square`.", 100), "A red square.")
        long = "One sentence here. " * 20
        cut = vd.tidy(long, 100)
        self.assertTrue(cut.endswith("."))
        self.assertLessEqual(len(cut), 100)

    def test_jpeg_is_shrunk_and_transparency_flattened(self):
        from PIL import Image

        small = base64.b64decode(vd.to_jpeg_b64(png((2000, 1000), mode="RGB"), 500, 85))
        with Image.open(BytesIO(small)) as im:
            self.assertEqual((im.format, max(im.size)), ("JPEG", 500))
        rgba = base64.b64decode(vd.to_jpeg_b64(png((10, 10), (0, 0, 0, 0), "RGBA"), 500, 85))
        with Image.open(BytesIO(rgba)) as im:
            self.assertEqual(im.getpixel((5, 5))[0] > 240, True)  # transparent became white

    def test_unreadable_bytes_are_passed_through(self):
        self.assertEqual(base64.b64decode(vd.to_jpeg_b64(b"not an image", 500, 85)), b"not an image")

    def test_data_url(self):
        self.assertEqual(vd.decode_data_url(data_url(b"abc")), b"abc")
        self.assertIsNone(vd.decode_data_url("/api/v1/files/x/content"))


class FilterTests(unittest.TestCase):
    def setUp(self):
        vd._cache.clear()
        self.filter = vd.Filter()
        self.filter.valves.SHOW_STATUS = False

    def body(self, *urls, text="What do you think?"):
        return {"messages": [{"role": "system", "content": "be nice"},
                             {"role": "user", "content": [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in urls]}]}

    def test_image_is_replaced_by_its_description_and_content_becomes_a_string(self):
        rec = Recorder("A red square.")
        with patch.object(vd.requests, "post", rec.post):
            out = run(self.filter.inlet(self.body(data_url(png()))))
        self.assertEqual(out["messages"][1]["content"], "What do you think?\n\n[Image: A red square.]")
        url, sent = rec.calls[0]
        self.assertTrue(url.endswith("/api/chat"))
        self.assertEqual((sent["model"], sent["think"], sent["options"]["num_ctx"], sent["stream"]), (self.filter.valves.VISION_MODEL, False, 4096, False))
        self.assertEqual(len(sent["messages"][0]["images"]), 1)

    def test_the_same_image_is_only_described_once(self):
        rec = Recorder()
        with patch.object(vd.requests, "post", rec.post):
            run(self.filter.inlet(self.body(data_url(png()))))
            run(self.filter.inlet(self.body(data_url(png()))))
        self.assertEqual(len(rec.calls), 1)

    def test_messages_without_images_are_untouched_and_nothing_is_called(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        rec = Recorder()
        with patch.object(vd.requests, "post", rec.post):
            self.assertEqual(run(self.filter.inlet(body)), {"messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(rec.calls, [])

    def test_an_unreachable_vision_server_leaves_a_note_not_an_error(self):
        def boom(url, json=None, timeout=None):
            raise vd.requests.ConnectionError("down")

        with patch.object(vd.requests, "post", boom):
            out = run(self.filter.inlet(self.body(data_url(png()))))
        text = out["messages"][1]["content"]
        self.assertIn("could not be viewed", text)
        self.assertIn("could not be reached", text)

    def test_http_error_and_empty_answer(self):
        with patch.object(vd.requests, "post", Recorder(status=500, text="model not found").post):
            self.assertIn("HTTP 500", run(self.filter.inlet(self.body(data_url(png()))))["messages"][1]["content"])
        vd._cache.clear()
        with patch.object(vd.requests, "post", Recorder(answer="  ").post):
            self.assertIn("returned nothing", run(self.filter.inlet(self.body(data_url(png()))))["messages"][1]["content"])

    def test_think_option_is_dropped_for_a_model_that_rejects_it(self):
        calls = []

        def post(url, json=None, timeout=None):
            calls.append(dict(json))
            if "think" in json:
                return FakeResp(400, text='{"error":"\\"x\\" does not support thinking"}')
            return FakeResp(200, {"message": {"content": "ok then"}})

        with patch.object(vd.requests, "post", post):
            out = run(self.filter.inlet(self.body(data_url(png()))))
        self.assertIn("ok then", out["messages"][1]["content"])
        self.assertEqual(["think" in c for c in calls], [True, False])

    def test_only_the_newest_images_are_described(self):
        self.filter.valves.MAX_IMAGES = 2
        rec = Recorder()
        urls = [data_url(png(color=(i * 40, 0, 0))) for i in range(4)]
        with patch.object(vd.requests, "post", rec.post):
            out = run(self.filter.inlet(self.body(*urls)))
        self.assertEqual(len(rec.calls), 2)
        text = out["messages"][1]["content"]
        self.assertEqual(text.count("[An earlier image was shared"), 2)
        self.assertEqual(text.count("[Image:"), 2)

    def test_unreadable_image_reference(self):
        out = run(self.filter.inlet(self.body("/api/v1/files/abc/content")))  # outside Open WebUI there is nothing to fetch it with
        self.assertIn("the image could not be read", out["messages"][1]["content"])

    def test_assistant_and_system_messages_are_left_alone(self):
        body = {"messages": [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": data_url(png())}}]}]}
        rec = Recorder()
        with patch.object(vd.requests, "post", rec.post):
            run(self.filter.inlet(body))
        self.assertEqual(rec.calls, [])

    def test_template_valves(self):
        self.filter.valves.IMAGE_TEMPLATE = "(she sees: {description})"
        with patch.object(vd.requests, "post", Recorder("a cat").post):
            out = run(self.filter.inlet(self.body(data_url(png()))))
        self.assertTrue(out["messages"][1]["content"].endswith("(she sees: a cat)"))


class ToolTests(unittest.TestCase):
    def setUp(self):
        di._cache.clear()
        self.tool = di.Tools()

    def test_data_link_with_a_question(self):
        rec = Recorder("A black dress.")
        with patch.object(di.requests, "post", rec.post):
            out = run(self.tool.describe_image(data_url(png()), "What is she wearing?"))
        self.assertEqual(out, "A black dress.")
        self.assertIn("What is she wearing?", rec.calls[0][1]["messages"][0]["content"])

    def test_default_prompt_without_a_question_and_caching(self):
        rec = Recorder()
        with patch.object(di.requests, "post", rec.post):
            run(self.tool.describe_image(data_url(png()), ""))
            run(self.tool.describe_image(data_url(png()), None))
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0][1]["messages"][0]["content"], di.DEFAULT_PROMPT)

    def test_reference_is_found_inside_markdown(self):
        self.assertEqual(di.pick_reference("![x](/api/v1/files/abc/content)"), "/api/v1/files/abc/content")
        self.assertEqual(di.pick_reference("see https://example.com/a.png now"), "https://example.com/a.png")
        self.assertEqual(di.pick_reference(" 'sdxl_simple_00012_.png' "), "sdxl_simple_00012_.png")

    def test_comfy_file_name_is_fetched_from_the_listed_servers(self):
        fetched = []

        def get(url, timeout=None):
            fetched.append(url)
            return FakeResp(404) if "gpu-a" in url else FakeResp(200, content=png())

        self.tool.valves.COMFY_SERVERS = ["http://gpu-a:8188", "http://gpu-b:8188"]
        rec = Recorder("A render.")
        with patch.object(di.requests, "get", get), patch.object(di.requests, "post", rec.post):
            out = run(self.tool.describe_image("sdxl_simple_00012_.png"))
        self.assertEqual(out, "A render.")
        self.assertIn("filename=sdxl_simple_00012_.png", fetched[1])
        self.assertIn("type=output", fetched[1])

    def test_comfy_file_not_found(self):
        self.tool.valves.COMFY_SERVERS = ["http://gpu-a:8188"]
        with patch.object(di.requests, "get", lambda url, timeout=None: FakeResp(404)):
            self.assertIn("was not found on the ComfyUI servers", run(self.tool.describe_image("missing_00001_.png")))

    def test_no_image_given_and_none_in_the_conversation(self):
        self.assertIn("no image to look at", run(self.tool.describe_image("", None, None, [{"role": "user", "content": "hi"}])))

    def test_blank_image_uses_the_newest_in_the_conversation(self):
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url(png())}}]}]
        with patch.object(di.requests, "post", Recorder("From the chat.").post):
            self.assertEqual(run(self.tool.describe_image("", None, None, messages)), "From the chat.")

    def test_failures_are_explained_not_raised(self):
        def boom(url, json=None, timeout=None):
            raise di.requests.Timeout()

        with patch.object(di.requests, "post", boom):
            out = run(self.tool.describe_image(data_url(png())))
        self.assertTrue(out.startswith("Could not look at the image"))
        self.assertIn("did not answer", out)
        self.assertIn("could not be fetched", run(self.tool.describe_image("/api/v1/files/zzz/content")))


if __name__ == "__main__":
    unittest.main()
