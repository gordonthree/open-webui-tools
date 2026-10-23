"""Tests for the pure helpers. They need fastapi and playwright importable, so run them inside the image:
   docker run --rm -v $PWD/docker/research-browser/service:/app -w /app research-browser-service python -m unittest test_app -v
"""
import unittest

import app


class CheckUrl(unittest.TestCase):
    def test_public_pages_pass(self):
        self.assertEqual(app.check_url("https://en.wikipedia.org/wiki/Crow"), "https://en.wikipedia.org/wiki/Crow")
        self.assertEqual(app.check_url("example.org/a?b=1"), "https://example.org/a?b=1")
        self.assertEqual(app.check_url("http://example.org:80/"), "http://example.org:80/")

    def test_refused(self):
        for bad in ["", "file:///etc/passwd", "javascript:alert(1)", "ftp://example.org/", "http://localhost/", "http://127.0.0.1/",
                    "http://192.168.1.1/", "http://10.0.0.5/", "http://172.20.0.2:3000/", "http://100.86.205.41/", "http://169.254.169.254/",
                    "http://[::1]/", "http://intranet/", "http://printer.local/", "http://nas.lan/", "https://u:p@example.org/",
                    "https://example.org:8080/", "http://0.0.0.0/"]:
            with self.subTest(bad):
                with self.assertRaises(app.Refused):
                    app.check_url(bad)


class Text(unittest.TestCase):
    def test_tidy(self):
        self.assertEqual(app.tidy("a    b\n \n\n\n\n c"), "a b\n\nc")

    def test_clip_pages_through_the_text(self):
        text = ("Sentence number one is here. " * 40 + "\n\n") * 5
        seen, start, pages = "", 0, 0
        while start is not None:
            piece, start = app.clip(text, start, 300)
            seen += piece + " "
            pages += 1
            self.assertLess(pages, 100)
        self.assertGreater(pages, 5)
        self.assertEqual(app.clip("short", 0, 300), ("short", None))

    def test_clip_start_beyond_end(self):
        self.assertEqual(app.clip("abc", 50, 300), ("", None))


class Status(unittest.TestCase):
    def test_usable_and_unusable(self):
        self.assertIsNone(app.status_problem(200, 5000))
        self.assertIsNone(app.status_problem(None, 0))
        self.assertIsNone(app.status_problem(404, 5000))               # a real "not found" page with content is fine to read
        self.assertIn("too many requests", app.status_problem(429, 0))
        self.assertIn("HTTP 403", app.status_problem(403, 9000))
        self.assertIn("HTTP 500", app.status_problem(500, 0))
        self.assertIn("not found", app.status_problem(404, 10))


class Links(unittest.TestCase):
    def test_same_site_links_are_short(self):
        self.assertEqual(app.show_href("/wiki/Crow", "https://en.wikipedia.org/wiki/Corvus"), "/wiki/Crow")
        self.assertEqual(app.show_href("https://example.org/a", "https://en.wikipedia.org/x"), "example.org/a")
        self.assertEqual(app.show_href("javascript:void(0)", "https://example.org/"), "")

    def test_duckduckgo_redirect_shows_its_target(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.bbc.com%2Fnews%2Fx&rut=abc"
        self.assertEqual(app.show_href(href, "https://html.duckduckgo.com/html/?q=x"), "www.bbc.com/news/x")

    def test_format_elements(self):
        lines = app.format_elements([{"ref": 1, "kind": "link", "label": "Crow", "href": "/wiki/Crow"},
                                     {"ref": 2, "kind": "field", "label": "Search", "type": "search"},
                                     {"ref": 3, "kind": "button", "label": "Go"}], "https://en.wikipedia.org/")
        self.assertEqual(lines, ['[1] link "Crow" -> /wiki/Crow', '[2] field "Search" -> search', '[3] button "Go"'])


if __name__ == "__main__":
    unittest.main()
