"""
Tests for serve_samples.py. Run from scripts/: python3 -m unittest test_serve_samples -v
(starts the server on a free localhost port in a thread; no network beyond that).
"""

import http.client
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import serve_samples as mod


class ParseRangeTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(mod.parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(mod.parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(mod.parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(mod.parse_range("bytes=50-500", 100), (50, 99))

    def test_ignored_when_absent_or_not_simple(self):
        for h in ("", "items=0-5", "bytes=0-5,10-15", "bytes=-"):
            self.assertIsNone(mod.parse_range(h, 100), h)

    def test_unsatisfiable(self):
        for h in ("bytes=100-", "bytes=200-300", "bytes=9-3", "bytes=-0"):
            with self.assertRaises(ValueError, msg=h):
                mod.parse_range(h, 100)
        with self.assertRaises(ValueError):
            mod.parse_range("bytes=-5", 0)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name, "samples")
        cls.root.mkdir()
        cls.outside = Path(cls.tmp.name, "secret.txt")
        cls.outside.write_text("do not serve")
        cls.audio = bytes(range(256)) * 40  # 10240 bytes
        (cls.root / "clip.mp3").write_bytes(cls.audio)
        (cls.root / "clip.txt").write_text("The words <b>spoken</b> & more")
        (cls.root / "sub").mkdir()
        (cls.root / "sub" / "notes.md").write_text("# notes")
        (cls.root / "<b>odd name.wav").write_bytes(b"RIFFxxxx")
        os.symlink(cls.outside, cls.root / "link.txt")
        old = time.time() - 1000
        os.utime(cls.root / "sub" / "notes.md", (old, old))
        cls.server = mod.make_server(cls.root, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r, body

    def test_index_lists_players_captions_and_escapes(self):
        r, body = self.request("GET", "/")
        text = body.decode()
        self.assertEqual(r.status, 200)
        self.assertIn('<audio controls preload="none" src="clip.mp3">', text)
        self.assertIn("The words &lt;b&gt;spoken&lt;/b&gt; &amp; more", text)
        self.assertNotIn("<b>spoken</b>", text)
        self.assertIn("&lt;b&gt;odd name.wav", text)
        self.assertIn("%3Cb%3Eodd%20name.wav", text)
        self.assertIn("sub/notes.md", text)
        self.assertIn("clip.mp3?download=1", text)

    def test_caption_file_is_not_listed_separately_and_symlink_outside_is_hidden(self):
        _, body = self.request("GET", "/")
        text = body.decode()
        self.assertNotIn("<h2>clip.txt</h2>", text)
        self.assertNotIn("link.txt", text)

    def test_newest_first(self):
        _, body = self.request("GET", "/")
        text = body.decode()
        self.assertLess(text.index("clip.mp3"), text.index("sub/notes.md"))

    def test_file_served_whole_with_type_and_ranges_advertised(self):
        r, body = self.request("GET", "/clip.mp3")
        self.assertEqual((r.status, body), (200, self.audio))
        self.assertEqual(r.getheader("Content-Type"), "audio/mpeg")
        self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
        self.assertIn("inline", r.getheader("Content-Disposition"))

    def test_download_flag_sets_attachment(self):
        r, _ = self.request("GET", "/clip.mp3?download=1")
        self.assertIn("attachment", r.getheader("Content-Disposition"))

    def test_range_requests(self):
        r, body = self.request("GET", "/clip.mp3", {"Range": "bytes=100-199"})
        self.assertEqual((r.status, body), (206, self.audio[100:200]))
        self.assertEqual(r.getheader("Content-Range"), f"bytes 100-199/{len(self.audio)}")
        r, body = self.request("GET", "/clip.mp3", {"Range": "bytes=-16"})
        self.assertEqual((r.status, body), (206, self.audio[-16:]))
        r, _ = self.request("GET", "/clip.mp3", {"Range": "bytes=999999-"})
        self.assertEqual(r.status, 416)
        self.assertEqual(r.getheader("Content-Range"), f"bytes */{len(self.audio)}")

    def test_head_has_headers_but_no_body(self):
        r, body = self.request("HEAD", "/clip.mp3")
        self.assertEqual((r.status, body), (200, b""))
        self.assertEqual(r.getheader("Content-Length"), str(len(self.audio)))

    def test_paths_outside_the_folder_are_refused(self):
        for bad in ("/../secret.txt", "/%2e%2e/secret.txt", "/sub/../../secret.txt", "/link.txt", "/%2e%2e%2fsecret.txt", "/nope.mp3", "/sub"):
            r, body = self.request("GET", bad)
            self.assertEqual(r.status, 404, bad)
            self.assertNotIn(b"do not serve", body, bad)

    def test_only_get_and_head(self):
        for method in ("POST", "PUT", "DELETE"):
            r, _ = self.request(method, "/clip.mp3")
            self.assertEqual(r.status, 405, method)
            self.assertEqual(r.getheader("Allow"), "GET, HEAD")
        self.assertEqual(list(self.root.glob("*.new")), [])


class ListFilesTests(unittest.TestCase):
    def test_empty_folder_message(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("No files yet.", mod.render_index(Path(d)))

    def test_human_size(self):
        self.assertEqual(mod.human_size(500), "500 B")
        self.assertEqual(mod.human_size(2048), "2.0 KB")
        self.assertEqual(mod.human_size(5 * 1024 * 1024), "5.0 MB")


if __name__ == "__main__":
    unittest.main()
