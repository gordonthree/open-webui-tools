"""
Tests for comfy_sdxl_poses.py. Run with: python -m unittest test_comfy_sdxl_poses -v
(no real ComfyUI or Open WebUI server needed - HTTP is faked).
"""

import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import comfy_sdxl_poses as mod


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = content
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mod.RequestException(f"HTTP {self.status_code}")


class FakeComfy:
    """One simulated ComfyUI server's /view and /upload/image endpoints."""

    def __init__(self):
        self.files = {}  # (subfolder, filename) -> bytes
        self.uploads = []

    def seed(self, subfolder, filename, data):
        self.files[(subfolder, filename)] = data

    def get(self, url, timeout=None):
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/view":
            key = (query.get("subfolder", [""])[0], query.get("filename", [""])[0])
            data = self.files.get(key)
            return FakeResponse(200, content=data) if data is not None else FakeResponse(404)
        return FakeResponse(404)

    def post(self, url, json=None, timeout=None, headers=None, files=None, data=None, params=None):
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/upload/image":
            name, content, _ctype = files["image"]
            subfolder = (data or {}).get("subfolder", "")
            self.files[(subfolder, name)] = content
            self.uploads.append({"filename": name, "subfolder": subfolder})
            return FakeResponse(200, {"name": name, "subfolder": subfolder, "type": (data or {}).get("type", "output")})
        return FakeResponse(404)


class FakeOpenWebUI:
    def __init__(self):
        self.files = {}  # file_id -> bytes

    def seed(self, file_id, data):
        self.files[file_id] = data

    def get(self, url, headers=None, timeout=None):
        parsed = urllib.parse.urlparse(url)
        if parsed.path.startswith("/api/v1/files/") and parsed.path.endswith("/content"):
            file_id = parsed.path.split("/")[4]
            data = self.files.get(file_id)
            return FakeResponse(200, content=data) if data is not None else FakeResponse(404)
        return FakeResponse(404)


class MultiServerRouter:
    """Dispatches requests.get/post to the right FakeComfy/FakeOpenWebUI by host, so tests can
    register several fake servers (e.g. a home server and a target server) at once."""

    def __init__(self):
        self.by_host = {}

    def register(self, base_url, fake):
        self.by_host[urllib.parse.urlparse(base_url).netloc] = fake

    def get(self, url, **kwargs):
        return self.by_host[urllib.parse.urlparse(url).netloc].get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.by_host[urllib.parse.urlparse(url).netloc].post(url, **kwargs)


class PoseCatalogTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod._job_db_schema_ready.clear()
        self.tool = mod.Tools()
        self.tool.valves.GPU_SERVERS = ["http://gpu-a:8188", "http://gpu-b:8188"]
        self.tool.valves.DEFAULT_GPU_SERVER = "http://gpu-a:8188"
        self.tool.valves.OPEN_WEBUI_BASE_URL = "http://owui:8080"
        self.tool.valves.OPEN_WEBUI_API_KEY = "test-key"
        self.tmpdir = tempfile.mkdtemp()
        self.tool.valves.JOB_DB_PATH = str(Path(self.tmpdir) / "jobs.sqlite3")
        self.router = MultiServerRouter()
        self.comfy_a = FakeComfy()
        self.comfy_b = FakeComfy()
        self.owui = FakeOpenWebUI()
        self.router.register("http://gpu-a:8188", self.comfy_a)
        self.router.register("http://gpu-b:8188", self.comfy_b)
        self.router.register("http://owui:8080", self.owui)
        self.patcher_get = patch("comfy_sdxl_poses.requests.get", side_effect=self.router.get)
        self.patcher_post = patch("comfy_sdxl_poses.requests.post", side_effect=self.router.post)
        self.patcher_get.start()
        self.patcher_post.start()

    def tearDown(self):
        self.patcher_get.stop()
        self.patcher_post.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        mod._job_db_schema_ready.clear()

    async def _add(self, pose_id="standing_hands_hips", **kw):
        kw.setdefault("description", "Standing, hands on hips, facing camera.")
        kw.setdefault("kind", "skeleton")
        kw.setdefault("source_image", "poses/standing.png")
        return await self.tool.add_pose(pose_id, **kw)


class AddPoseTests(PoseCatalogTestCase):
    async def test_add_pose_from_existing_source_image(self):
        self.comfy_a.seed("poses", "standing.png", b"PNGDATA")
        res = await self._add(tags="Standing, Action")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["pose_id"], "standing_hands_hips")
        self.assertEqual(res["tags"], ["action", "standing"])
        self.assertEqual(res["server"], "http://gpu-a:8188")

    async def test_add_pose_rejects_missing_source_image(self):
        res = await self._add(source_image="poses/nope.png")
        self.assertFalse(res["success"])
        self.assertIn("not found", res["error"])

    async def test_add_pose_requires_exactly_one_of_source_image_or_openwebui_file(self):
        res = await self._add(source_image="", openwebui_file="")
        self.assertFalse(res["success"])
        self.assertIn("exactly one", res["error"])

        res2 = await self._add(source_image="poses/standing.png", openwebui_file="abc")
        self.assertFalse(res2["success"])
        self.assertIn("exactly one", res2["error"])

    async def test_add_pose_rejects_invalid_kind(self):
        res = await self._add(kind="not_a_kind")
        self.assertFalse(res["success"])
        self.assertIn("kind must be one of", res["error"])

    async def test_add_pose_from_openwebui_file(self):
        self.owui.seed("11111111-2222-3333-4444-555555555555", b"OWUIBYTES")
        res = await self._add(
            pose_id="reference_photo",
            kind="reference",
            source_image="",
            openwebui_file="http://owui:8080/api/v1/files/11111111-2222-3333-4444-555555555555/content",
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(self.comfy_a.uploads[0]["filename"], "pose_reference_photo.png")

    async def test_add_pose_openwebui_file_bare_id(self):
        self.owui.seed("11111111-2222-3333-4444-555555555555", b"OWUIBYTES")
        res = await self._add(
            pose_id="bare_id_pose",
            source_image="",
            openwebui_file="11111111-2222-3333-4444-555555555555",
        )
        self.assertTrue(res["success"], res)

    async def test_add_pose_duplicate_requires_overwrite(self):
        self.comfy_a.seed("poses", "standing.png", b"PNGDATA")
        first = await self._add()
        self.assertTrue(first["success"], first)
        second = await self._add()
        self.assertFalse(second["success"])
        self.assertIn("overwrite", second["error"])

        third = await self._add(overwrite=True, description="Updated description.")
        self.assertTrue(third["success"], third)
        self.assertEqual(third["description"], "Updated description.")

    async def test_add_pose_empty_string_sentinels_for_optional_args(self):
        self.comfy_a.seed("poses", "standing.png", b"PNGDATA")
        res = await self._add(gpu_server="", tags="", overwrite=False)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["tags"], [])


class ListGetDeletePoseTests(PoseCatalogTestCase):
    async def test_list_poses_empty(self):
        res = await self.tool.list_poses()
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 0)

    async def test_list_search_and_kind_filter(self):
        self.comfy_a.seed("poses", "standing.png", b"A")
        self.comfy_a.seed("poses", "sitting.png", b"B")
        await self._add(pose_id="standing_pose", kind="skeleton", source_image="poses/standing.png", tags="standing")
        await self._add(pose_id="sitting_pose", kind="reference", source_image="poses/sitting.png", tags="sitting")

        all_rows = await self.tool.list_poses()
        self.assertEqual(all_rows["count"], 2)

        skeleton_only = await self.tool.list_poses(kind="skeleton")
        self.assertEqual(skeleton_only["count"], 1)
        self.assertIn("standing_pose", skeleton_only["table"])

        by_search = await self.tool.list_poses(search="sitting")
        self.assertEqual(by_search["count"], 1)
        self.assertIn("sitting_pose", by_search["table"])

    async def test_list_poses_empty_string_sentinels_mean_no_filter(self):
        self.comfy_a.seed("poses", "standing.png", b"A")
        await self._add()
        res = await self.tool.list_poses(search="", tags="", kind="", limit="")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 1)

    async def test_get_pose_roundtrip_and_missing(self):
        self.comfy_a.seed("poses", "standing.png", b"PNGDATA")
        await self._add(tags="standing")
        got = await self.tool.get_pose("standing_hands_hips")
        self.assertTrue(got["success"], got)
        self.assertEqual(got["kind"], "skeleton")
        self.assertEqual(got["tags"], ["standing"])

        missing = await self.tool.get_pose("nope")
        self.assertFalse(missing["success"])
        self.assertIn("No pose named", missing["error"])

    async def test_delete_pose(self):
        self.comfy_a.seed("poses", "standing.png", b"PNGDATA")
        await self._add()
        res = await self.tool.delete_pose("standing_hands_hips")
        self.assertTrue(res["success"], res)
        missing = await self.tool.get_pose("standing_hands_hips")
        self.assertFalse(missing["success"])

        again = await self.tool.delete_pose("standing_hands_hips")
        self.assertFalse(again["success"])


class SelectPoseTests(PoseCatalogTestCase):
    async def test_select_pose_same_server_no_copy(self):
        self.comfy_a.seed("poses", "standing.png", b"PNGDATA")
        await self._add()
        res = await self.tool.select_pose("standing_hands_hips", gpu_server="http://gpu-a:8188")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["source_image"], "poses/standing.png")
        self.assertEqual(res["server"], "http://gpu-a:8188")
        self.assertNotIn("source_image_note", res)
        self.assertIn("no preprocessor needed", res["note"])

    async def test_select_pose_copies_across_servers(self):
        self.comfy_a.seed("poses", "standing.png", b"PNGDATA")
        await self._add()
        res = await self.tool.select_pose("standing_hands_hips", gpu_server="http://gpu-b:8188")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["server"], "http://gpu-b:8188")
        self.assertIn("Copied source image", res["source_image_note"])
        self.assertEqual(self.comfy_b.files[("poses", "standing.png")], b"PNGDATA")

    async def test_select_pose_reference_kind_note(self):
        self.comfy_a.seed("poses", "ref.png", b"PNGDATA")
        await self._add(pose_id="ref_pose", kind="reference", source_image="poses/ref.png")
        res = await self.tool.select_pose("ref_pose")
        self.assertTrue(res["success"], res)
        self.assertIn("preprocessor node", res["note"])

    async def test_select_pose_missing(self):
        res = await self.tool.select_pose("does_not_exist")
        self.assertFalse(res["success"])
        self.assertIn("No pose named", res["error"])

    async def test_select_pose_invalid_pose_id(self):
        res = await self.tool.select_pose("Not A Valid Id!")
        self.assertFalse(res["success"])
        self.assertIn("Invalid pose_id", res["error"])


class HelperTests(unittest.TestCase):
    def test_validate_pose_id(self):
        self.assertEqual(mod.validate_pose_id(" Standing_Pose-1 "), "standing_pose-1")
        with self.assertRaises(ValueError):
            mod.validate_pose_id("a")
        with self.assertRaises(ValueError):
            mod.validate_pose_id("-leading-hyphen")

    def test_parse_openwebui_file_id_variants(self):
        uid = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(mod.parse_openwebui_file_id(uid), uid)
        self.assertEqual(mod.parse_openwebui_file_id(f"/api/v1/files/{uid}/content"), uid)
        self.assertEqual(mod.parse_openwebui_file_id(f"http://owui.example.com/api/v1/files/{uid}/content"), uid)
        self.assertEqual(mod.parse_openwebui_file_id("bare-id-no-slashes"), "bare-id-no-slashes")
        with self.assertRaises(ValueError):
            mod.parse_openwebui_file_id("http://attacker.example.com/not/a/valid/ref with space")

    def test_is_unset(self):
        for v in (None, "", "  ", "none", "NULL", "default", "n/a"):
            self.assertTrue(mod.is_unset(v), v)
        for v in ("0", "skeleton", 0, False):
            self.assertFalse(mod.is_unset(v), v)


if __name__ == "__main__":
    unittest.main()
