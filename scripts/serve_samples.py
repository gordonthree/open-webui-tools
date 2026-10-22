#!/usr/bin/env python3
"""
Serve a folder of audio samples (and other files) over HTTP, so they can be played or downloaded from a browser
instead of hunting through a server's file system. Read-only, standard library only.

    python3 scripts/serve_samples.py                 # serves tmp/samples on 0.0.0.0:8899
    python3 scripts/serve_samples.py --dir ~/clips --port 9000

By default it listens on every network interface (0.0.0.0), so it is reachable from the LAN and over a Tailscale
network. There is no login: do not forward the port to the Internet. To keep it to this machine only, pass
--host 127.0.0.1 and reach it through an SSH tunnel (ssh -L 8899:127.0.0.1:8899 <this-host>).

The index page lists every file under the folder, newest first. Audio files get a player and a download link; a
text file with the same name as an audio file (clip.mp3 + clip.txt) is shown under its player as a caption.
Range requests are supported, so a browser can seek inside long recordings. Only GET and HEAD are accepted, and a
path that leaves the folder (including through a symlink) is refused.
"""

import argparse
import html
import mimetypes
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit, parse_qs

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO / "tmp" / "samples"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8899
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac", ".aac", ".webm"}
CHUNK = 64 * 1024

mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/ogg", ".opus")


def parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """(first, last) byte positions for a single 'bytes=' range, or None if the header is absent or not a simple range.

    Raises ValueError when the range cannot be satisfied (the caller answers 416).
    """
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":  # the last N bytes
        n = int(m.group(2))
        if n == 0 or size == 0:
            raise ValueError("unsatisfiable")
        return max(0, size - n), size - 1
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) else size - 1
    if first >= size or last < first:
        raise ValueError("unsatisfiable")
    return first, min(last, size - 1)


def list_files(root: Path) -> List[Path]:
    """Every regular file under root, newest first. Symlinks that lead outside root are skipped."""
    root = root.resolve()
    found = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath, name)
            try:
                if p.is_file() and p.resolve().is_relative_to(root):
                    found.append(p)
            except OSError:
                continue
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def render_index(root: Path) -> str:
    import time

    root = root.resolve()
    rows = []
    for p in list_files(root):
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() == ".txt" and any(p.with_suffix(s).exists() for s in AUDIO_SUFFIXES):
            continue  # shown as the caption of its audio file
        href = quote(rel)
        st = p.stat()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        meta = f"{html.escape(when)} &middot; {human_size(st.st_size)}"
        links = f'<a href="{href}">open</a> &middot; <a href="{href}?download=1">download</a>'
        if p.suffix.lower() in AUDIO_SUFFIXES:
            caption = ""
            for s in (".txt", ".md"):
                side = p.with_suffix(s)
                if side.is_file():
                    try:
                        caption = f"<pre>{html.escape(side.read_text(errors='replace')[:4000])}</pre>"
                    except OSError:
                        pass
                    break
            rows.append(
                f'<section><h2>{html.escape(rel)}</h2><p class="meta">{meta} &middot; {links}</p>'
                f'<audio controls preload="none" src="{href}"></audio>{caption}</section>'
            )
        else:
            rows.append(f'<section><h2>{html.escape(rel)}</h2><p class="meta">{meta} &middot; {links}</p></section>')
    body = "\n".join(rows) or "<p>No files yet.</p>"
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Samples</title><style>"
        ":root{color-scheme:light dark;font-family:system-ui,sans-serif}body{max-width:46rem;margin:0 auto;padding:1rem 16px}"
        "section{border-bottom:1px solid #8884;padding:.6rem 0}h2{font-size:1rem;margin:.2rem 0;word-break:break-all}"
        ".meta{margin:.1rem 0 .4rem;font-size:.85rem;opacity:.75}audio{width:100%}pre{white-space:pre-wrap;font-size:.85rem;opacity:.85}"
        f"</style></head><body><h1>Samples</h1><p class=\"meta\">{html.escape(str(root))} &middot; newest first</p>{body}</body></html>"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "serve-samples"
    root: Path = DEFAULT_DIR

    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send_bytes(self, status: int, body: bytes, ctype: str, head: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _fail(self, status: int, head: bool, message: str = "") -> None:
        self._send_bytes(status, (message or HTTPStatus(status).phrase).encode(), "text/plain; charset=utf-8", head)

    def _target(self, url_path: str) -> Optional[Path]:
        """The file under root for a URL path, or None if it is missing or outside root."""
        rel = unquote(url_path).lstrip("/")
        if "\x00" in rel:
            return None
        root = self.root.resolve()
        try:
            candidate = (root / rel).resolve()
        except (OSError, ValueError):
            return None
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate

    def _serve(self, head: bool) -> None:
        parts = urlsplit(self.path)
        if parts.path in ("", "/"):
            return self._send_bytes(200, render_index(self.root).encode(), "text/html; charset=utf-8", head)
        target = self._target(parts.path)
        if target is None:
            return self._fail(404, head)
        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            rng = parse_range(self.headers.get("Range", ""), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        first, last = rng if rng else (0, size - 1)
        length = 0 if size == 0 else last - first + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        disposition = "attachment" if parse_qs(parts.query).get("download") else "inline"
        self.send_header("Content-Disposition", f'{disposition}; filename="{quote(target.name)}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head or length == 0:
            return
        with open(target, "rb") as f:
            f.seek(first)
            left = length
            while left > 0:
                data = f.read(min(CHUNK, left))
                if not data:
                    break
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the player closed the connection (seeking does this); not an error
                left -= len(data)

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

    def _refuse(self):
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse


def make_server(root: Path, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"root": Path(root)})
    return ThreadingHTTPServer((host, port), handler)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="folder to serve (default: tmp/samples in the repo)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST, help="address to listen on (default 0.0.0.0 = every interface; 127.0.0.1 = this machine only)")
    return ap


def tailscale_address() -> str:
    """This machine's Tailscale IPv4 address, or "" if Tailscale is not installed or not running."""
    import subprocess

    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.split() else ""


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    server = make_server(root, args.host, args.port)
    port = server.server_address[1]
    print(f"Serving {root.resolve()}  (Ctrl+C to stop)")
    if args.host in ("127.0.0.1", "localhost"):
        print(f"This machine only: http://127.0.0.1:{port}/   (from elsewhere: ssh -L {port}:127.0.0.1:{port} <this-host>)")
    else:
        print(f"Listening on {args.host}:{port} with no login; keep the port off the Internet.")
        ts = tailscale_address()
        if ts:
            print(f"Tailscale: http://{ts}:{port}/")
        print(f"This machine: http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
