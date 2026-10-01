#!/usr/bin/env python3
"""
A small LAN web interface for the agent_notes database - browse, search, and maintain the notes an
agent keeps, from a browser, with no Open WebUI involved.

    python scripts/notes_web.py --db /path/to/agent_notes.sqlite3 [--host 0.0.0.0] [--port 8765]

Pages are plain server-rendered HTML with ordinary forms (no JavaScript, no build step). You can
list/search notes, read one, create a note, append/edit/delete entries, rename a note or change its
comment, and delete a note. All of it goes through the same *_db functions the agent's tool uses,
so the limits, case-insensitive name matching and write transactions are identical. Notes and
entries created here are attributed to --author (default "Gordon"); authors are shown everywhere. The database
runs in WAL mode, so using this while an agent writes is safe; refresh the page to see its changes.

There is NO authentication or CSRF protection - run it only on a trusted LAN. Every delete is
permanent, same as the tool.

Requirements:
    - Python 3.9+, stdlib only for the web part. But it imports ../src/agent_notes.py, which needs
      `pydantic` installed (the same package the tool tests need).
    - Run it from the docker host (or anywhere that can open the .sqlite3 file); the default path
      is the one inside the OWUI container, so you will almost always pass --db.
"""

import argparse
import html
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent_notes as notes  # noqa: E402

DEFAULT_AUTHOR = "Gordon"  # who notes/entries made through this page are attributed to (--author)
DEFAULT_DB = "/app/backend/data/comfy_outputs/agent_notes.sqlite3"
ALL = 10**9  # "no limit" for the *_db functions
MAX_BODY = 64 * 1024

STYLE = """
body{font:15px/1.45 system-ui,sans-serif;max-width:52rem;margin:1.5rem auto;padding:0 1rem;color:#222;background:#fafafa}
a{color:#0b5cad}h1{font-size:1.4rem}h1 a{color:inherit;text-decoration:none}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #ddd;vertical-align:top}
.muted{color:#777;font-size:.85em}.err{background:#fde8e8;border:1px solid #e0a0a0;padding:.6rem .8rem;border-radius:4px}
.entry{background:#fff;border:1px solid #ddd;border-radius:4px;padding:.5rem .8rem;margin:.6rem 0}
.entry pre{white-space:pre-wrap;word-wrap:break-word;margin:.3rem 0;font:inherit}
textarea,input[type=text]{width:100%;box-sizing:border-box;font:inherit;padding:.3rem}
form{margin:.4rem 0}button{font:inherit;padding:.25rem .7rem}.danger{color:#a00}
details{margin:.3rem 0}summary{cursor:pointer;color:#0b5cad}
"""


def e(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)}</title><style>{STYLE}</style></head><body>"
        f"<h1><a href='/'>Agent notes</a></h1>{body}</body></html>"
    )


def when(iso: Optional[str]) -> str:
    return (iso or "").replace("T", " ")[:19] + " UTC" if iso else ""


def note_url(name: str) -> str:
    return "/note?name=" + quote(name)


def search_form(q: str = "") -> str:
    return f"<form method='get' action='/'><input type='text' name='q' value='{e(q)}' placeholder='search notes and entries'></form>"


def render_index(db: str, q: str) -> str:
    body = search_form(q)
    if q:
        found_notes, found_entries = notes.search_notes_db(db, q, 200)
        body += f"<h2>Search: {e(q)}</h2>"
        if not found_notes and not found_entries:
            body += "<p class='muted'>Nothing matched.</p>"
        if found_notes:
            body += "<h3>Notes</h3><ul>" + "".join(
                f"<li><a href='{note_url(n['note_name'])}'>{e(n['note_name'])}</a> <span class='muted'>{e(n['note_comment'])}</span></li>"
                for n in found_notes
            ) + "</ul>"
        if found_entries:
            body += "<h3>Entries</h3><ul>" + "".join(
                f"<li><a href='{note_url(r['note_name'])}#e{r['entry_no']}'>{e(r['note_name'])} #{r['entry_no']}</a> <span class='muted'>({e(r['author_name'] or 'unknown')})</span>: {e(notes._truncate(r['note_text'], 200))}</li>"
                for r in found_entries
            ) + "</ul>"
        return page("Search", body)
    total, rows = notes.list_notes_db(db, ALL)
    body += f"<p class='muted'>{total} note{'s' if total != 1 else ''}, most recently updated first.</p>"
    if rows:
        body += "<table><tr><th>Note</th><th>Entries</th><th>Updated</th><th>Started by</th><th>Comment</th></tr>" + "".join(
            f"<tr><td><a href='{note_url(r['note_name'])}'>{e(r['note_name'])}</a></td><td>{r['entries']}</td>"
            f"<td class='muted'>{e(when(r['updated_at']))}</td><td>{e(r['author_name'] or 'unknown')}</td><td>{e(notes._truncate(r['note_comment'], 100))}</td></tr>"
            for r in rows
        ) + "</table>"
    body += (
        "<h2>New note</h2><form method='post' action='/create'>"
        f"<p><input type='text' name='name' maxlength='{notes.NOTE_NAME_MAX}' placeholder='name' required></p>"
        f"<p><input type='text' name='comment' maxlength='{notes.NOTE_COMMENT_MAX}' placeholder='comment (optional)'></p>"
        f"<p><textarea name='text' rows='3' maxlength='{notes.NOTE_TEXT_MAX}' placeholder='first entry' required></textarea></p>"
        "<button>Create</button></form>"
    )
    return page("Agent notes", body)


def render_note(db: str, name: str) -> str:
    result = notes.read_note_db(db, name, ALL)
    note, entries = result["note"], result["entries"]
    n = note["note_name"]
    hidden = f"<input type='hidden' name='name' value='{e(n)}'>"
    body = search_form() + f"<h2>{e(n)}</h2>"
    if note["note_comment"]:
        body += f"<p>{e(note['note_comment'])}</p>"
    body += f"<p class='muted'>{result['total']} entries; started by {e(result['started_by'] or 'unknown')}, created {e(when(note['created_at']))}, updated {e(when(note['updated_at']))}</p>"
    sm = result["summary"]
    if sm:
        flag = " &middot; <b>stale: the note changed since</b>" if sm["stale"] else ""
        body += (
            f"<div class='entry'><span class='muted'>Summary by {e(sm['author_name'] or 'unknown')} &middot; "
            f"{e(when(sm['last_summarized']))}{flag}</span><pre>{e(sm['summary_text'])}</pre></div>"
        )
    for r in entries:
        edited = f" &middot; edited {e(when(r['edited_at']))}" if r["edited_at"] else ""
        body += (
            f"<div class='entry' id='e{r['entry_no']}'><span class='muted'>#{r['entry_no']} &middot; {e(r['author_name'] or 'unknown')} &middot; {e(when(r['created_at']))}{edited}</span>"
            f"<pre>{e(r['note_text'])}</pre>"
            f"<details><summary>edit / delete</summary>"
            f"<form method='post' action='/edit'>{hidden}<input type='hidden' name='entry_no' value='{r['entry_no']}'>"
            f"<textarea name='text' rows='3' maxlength='{notes.NOTE_TEXT_MAX}'>{e(r['note_text'])}</textarea><button>Save</button></form>"
            f"<form method='post' action='/delete_entry'>{hidden}<input type='hidden' name='entry_no' value='{r['entry_no']}'>"
            f"<button class='danger'>Delete entry #{r['entry_no']}</button></form></details></div>"
        )
    body += (
        f"<h3>Append entry</h3><form method='post' action='/append'>{hidden}"
        f"<textarea name='text' rows='3' maxlength='{notes.NOTE_TEXT_MAX}' required></textarea><button>Append</button></form>"
        f"<details><summary>rename / change comment</summary><form method='post' action='/update'>{hidden}"
        f"<p><input type='text' name='new_name' value='{e(n)}' maxlength='{notes.NOTE_NAME_MAX}'></p>"
        f"<p><input type='text' name='comment' value='{e(note['note_comment'])}' maxlength='{notes.NOTE_COMMENT_MAX}'></p>"
        f"<button>Save</button></form></details>"
        f"<details><summary class='danger'>delete note</summary><form method='post' action='/delete_note'>{hidden}"
        f"<button class='danger'>Permanently delete '{e(n)}' and its {result['total']} entries</button></form></details>"
    )
    return page(n, body)


def error_page(message: str, back: str = "/") -> str:
    return page("Error", f"<p class='err'>{e(message)}</p><p><a href='{e(back)}'>&larr; back</a></p>")


Response = Tuple[int, Dict[str, str], str]


def redirect(url: str) -> Response:
    return 303, {"Location": url}, ""


def handle_get(db: str, path: str, query: Dict[str, list]) -> Response:
    first = lambda key: (query.get(key) or [""])[0].strip()  # noqa: E731
    try:
        if path == "/":
            return 200, {}, render_index(db, first("q"))
        if path == "/note":
            return 200, {}, render_note(db, notes.validate_name(first("name")))
    except (notes.NoteError, ValueError) as ex:
        return 404, {}, error_page(str(ex))
    return 404, {}, error_page("No such page.")


def handle_post(db: str, path: str, form: Dict[str, str], author: str = DEFAULT_AUTHOR) -> Response:
    back = note_url(form["name"]) if form.get("name") else "/"
    try:
        name = lambda: notes.validate_name(form.get("name"))  # noqa: E731
        if path == "/create":
            made = notes.create_note_db(db, name(), notes.validate_text(form.get("text")), notes.validate_comment(form.get("comment")), notes.validate_author(author))
            return redirect(note_url(made["note_name"]))
        if path == "/append":
            done = notes.append_entry_db(db, name(), notes.validate_text(form.get("text")), notes.validate_author(author))
            return redirect(note_url(done["note_name"]) + f"#e{done['entry_no']}")
        entry = notes._to_int(form.get("entry_no"), None, "Entry number") if "entry_no" in form else None
        if path == "/edit":
            done = notes.edit_entry_db(db, name(), entry, notes.validate_text(form.get("text")))
            return redirect(note_url(done["note_name"]) + f"#e{entry}")
        if path == "/delete_entry":
            done = notes.delete_entry_db(db, name(), entry)
            return redirect(note_url(done["note_name"]))
        if path == "/update":
            new_name = notes.validate_name(form.get("new_name"))
            done = notes.update_note_db(db, name(), new_name, notes.validate_comment(form.get("comment")))
            return redirect(note_url(done["note_name"]))
        if path == "/delete_note":
            notes.delete_note_db(db, name())
            return redirect("/")
    except (notes.NoteError, ValueError) as ex:
        return 400, {}, error_page(str(ex), back)
    except notes.sqlite3.Error as ex:
        return 500, {}, error_page(f"Database error: {ex}", back)
    return 404, {}, error_page("No such action.")


def make_handler(db: str, author: str = DEFAULT_AUTHOR):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, response: Response):
            status, headers, body = response
            data = body.encode("utf-8")
            self.send_response(status)
            if status != 303:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                self._send(handle_get(db, url.path, parse_qs(url.query)))
            except notes.sqlite3.Error as ex:
                self._send((500, {}, error_page(f"Database error: {ex}")))

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send((413, {}, error_page("Request too large.")))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True).items()}
            self._send(handle_post(db, urlparse(self.path).path, form, author))

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="LAN web interface for the agent_notes database.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"path to agent_notes.sqlite3 (default: {DEFAULT_DB})")
    ap.add_argument("--host", default="0.0.0.0", help="address to bind (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--author", default=DEFAULT_AUTHOR, help=f"author name recorded for notes and entries added here (default: {DEFAULT_AUTHOR})")
    args = ap.parse_args(argv)
    if not Path(args.db).exists():
        print(f"warning: {args.db} does not exist yet; an empty database will be created there", file=sys.stderr)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.db, notes.validate_author(args.author)))
    print(f"Serving {args.db} on http://{args.host}:{args.port}/  (no authentication - trusted LAN only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
