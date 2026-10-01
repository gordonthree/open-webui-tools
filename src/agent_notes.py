"""
title: Agent Notes
author: Gordon
version: 1.2.0
description: A persistent notebook for agents, easier to use than Open WebUI's built-in note tool.
    A note is a name plus an append-only log of short numbered entries (500 characters each), kept
    in its own SQLite database. To add to a note, append an entry - nothing is ever rewritten or
    diffed. Notes are addressed by their plain-language name, entries by their number.
    list_notes/read_note/search_notes browse; create_note/append_note add; edit_entry/
    delete_entry/update_note/delete_note maintain. Every delete is permanent.

    Every note and entry records its author: pass author_name when writing, or the tool uses the
    name of the model Open WebUI says is calling it. Entries are stamped with the current time
    unless the caller passes created_at, which lets old notes be moved in with their real dates.

    Limits (also stated in each writing method's docstring, which is what the model actually
    sees): note name 60 characters, note comment 500, each entry's text 500. Longer content goes
    in several entries, not one long one.
"""

import asyncio
import contextlib
import datetime
import difflib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("agent_notes")
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# SQLite note store - its own database file (NOT the ComfyUI job database), living in the same
# folder. A note is one row in note_id; its content is the rows of note_data that point at it,
# kept in chronological order. Adding to a note inserts a row; nothing is rewritten.
# --------------------------------------------------------------------------- #

NOTE_NAME_MAX = 60
NOTE_COMMENT_MAX = 500
NOTE_TEXT_MAX = 500
AUTHOR_NAME_MAX = 60
LEGACY_AUTHOR = "Mara Voss"  # author #1: seeded into every notes database, and given every row that predates authors
FALLBACK_AUTHOR = "Unknown agent"  # used only when the caller gave no author_name and Open WebUI named no model

NOTES_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS note_author (
    author_id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_name TEXT NOT NULL CHECK (length(author_name) BETWEEN 1 AND {AUTHOR_NAME_MAX}),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_author_name ON note_author(lower(author_name));
CREATE TABLE IF NOT EXISTS note_id (
    note_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    note_name TEXT NOT NULL CHECK (length(note_name) BETWEEN 1 AND {NOTE_NAME_MAX}),
    note_comment TEXT NOT NULL DEFAULT '' CHECK (length(note_comment) <= {NOTE_COMMENT_MAX}),
    next_entry_no INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    author_id INTEGER REFERENCES note_author(author_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_note_name ON note_id(lower(note_name));
CREATE TABLE IF NOT EXISTS note_data (
    note_pk INTEGER NOT NULL REFERENCES note_id(note_pk) ON DELETE CASCADE,
    entry_no INTEGER NOT NULL,
    note_text TEXT NOT NULL CHECK (length(note_text) BETWEEN 1 AND {NOTE_TEXT_MAX}),
    created_at TEXT NOT NULL,
    edited_at TEXT,
    author_id INTEGER REFERENCES note_author(author_id),
    PRIMARY KEY (note_pk, entry_no)
);
CREATE INDEX IF NOT EXISTS idx_note_data_time ON note_data(note_pk, created_at);
"""

DB_BUSY_TIMEOUT_S = 5.0
_schema_ready: set = set()  # db_path values confirmed (this process) to have the schema


class NoteError(ValueError):
    """A problem the calling model can fix; its message is returned to the model verbatim."""


def notes_db_connect(db_path: str) -> sqlite3.Connection:
    """A short-lived connection with the schema ensured. Caller must close() it. Transactions are
    managed explicitly (isolation_level=None) so writers can take the lock up front."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=DB_BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")  # off by default in SQLite; delete_note relies on the cascade
    conn.execute("PRAGMA journal_mode=WAL;")
    if db_path not in _schema_ready:
        conn.executescript(NOTES_SCHEMA)
        _migrate_authors(conn)
        _schema_ready.add(db_path)
    return conn


@contextlib.contextmanager
def _write_txn(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _migrate_authors(conn: sqlite3.Connection) -> None:
    """Bring a database created before authors existed up to date, and make sure author #1 exists.

    The author_id columns are nullable (SQLite requires a NULL default when adding a column that
    has a REFERENCES clause), which also keeps an older copy of the tool, one that doesn't know
    about authors, able to write to an upgraded database - its rows just have no author. Only
    rows that were already there when the column was added are attributed to author #1; later
    NULLs are never rewritten. Done in one write transaction so two processes can't both ALTER."""
    with _write_txn(conn):
        added = []
        for table in ("note_id", "note_data"):
            if "author_id" not in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN author_id INTEGER REFERENCES note_author(author_id)")
                added.append(table)
        conn.execute(
            "INSERT INTO note_author (author_id, author_name, created_at) SELECT 1, ?, ? WHERE NOT EXISTS (SELECT 1 FROM note_author)",
            (LEGACY_AUTHOR, _now_iso()),
        )
        for table in added:
            conn.execute(f"UPDATE {table} SET author_id = 1")


def _author_id(conn: sqlite3.Connection, author_name: str, now: str) -> int:
    """The id of this author (matched ignoring case), adding them - timestamped - if new. Call inside a write transaction."""
    row = conn.execute("SELECT author_id FROM note_author WHERE lower(author_name) = lower(?)", (author_name,)).fetchone()
    if row:
        return row["author_id"]
    return conn.execute("INSERT INTO note_author (author_name, created_at) VALUES (?, ?)", (author_name, now)).lastrowid


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

_UNSET_STRINGS = {"", "default", "auto", "none", "null", "undefined", "n/a"}


def is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def _cell(text: str, limit: int = 120) -> str:
    return _truncate((text or "").replace("\n", " "), limit).replace("|", "\\|")


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def _to_int(value: Any, default: Optional[int], label: str, minimum: int = 1) -> Optional[int]:
    if is_unset(value):
        return default
    try:
        number = int(str(value).strip().lstrip("#"))
    except ValueError:
        raise NoteError(f"{label} must be a whole number, got {value!r}.")
    if number < minimum:
        raise NoteError(f"{label} must be at least {minimum}, got {number}.")
    return number


def validate_name(name: Any) -> str:
    cleaned = " ".join(str(name or "").split())  # trim, collapse whitespace runs/newlines
    if not cleaned:
        raise NoteError("A note name is required.")
    if len(cleaned) > NOTE_NAME_MAX:
        raise NoteError(f"Note name is {len(cleaned)} characters; the limit is {NOTE_NAME_MAX}. Choose a shorter name.")
    return cleaned


def validate_author(name: Any) -> str:
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise NoteError("An author name is required.")
    if len(cleaned) > AUTHOR_NAME_MAX:
        raise NoteError(f"Author name is {len(cleaned)} characters; the limit is {AUTHOR_NAME_MAX}. Use a shorter name.")
    return cleaned


def resolve_author(author_name: Any, model: Any) -> str:
    """The author to record: what the caller said, else the model Open WebUI says is calling, else a placeholder."""
    if not is_unset(author_name):
        return validate_author(author_name)
    if isinstance(model, dict):
        label = model.get("name") or model.get("id")
        if not is_unset(label):
            return validate_author(str(label)[:AUTHOR_NAME_MAX])
    return FALLBACK_AUTHOR


def validate_timestamp(value: Any) -> Optional[str]:
    """None if unset; else a UTC ISO-8601 string. Accepts ISO-8601 text (a date alone, or with a time
    and optional offset/'Z'; no offset means UTC) or a Unix epoch in seconds, milliseconds,
    microseconds or nanoseconds (Open WebUI's own notes use nanoseconds), as a number or digit
    string. Refuses anything unparseable or more than a day in the future, which mostly means a
    unit mix-up."""
    if is_unset(value):
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"-?\d+(\.\d+)?", text):
            number = float(text)
            for floor, divisor in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):  # ns, us, ms; anything smaller is seconds
                if abs(number) >= floor:
                    number /= divisor
                    break
            moment = datetime.datetime.fromtimestamp(number, datetime.timezone.utc)
        else:
            moment = datetime.datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in "Zz" else text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise NoteError(
            f"created_at {text!r} isn't a timestamp I can read. Use ISO 8601 like 2026-03-01T14:30:00Z (or just 2026-03-01), "
            "or a Unix epoch number. Pass \"\" (or omit) to use the current time."
        )
    moment = moment.astimezone(datetime.timezone.utc)
    if moment > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1):
        raise NoteError(f"created_at {text!r} is in the future ({moment.isoformat()}). Check the date, or the epoch's unit.")
    if moment.year < 1970:
        raise NoteError(f"created_at {text!r} is before 1970 ({moment.isoformat()}). Check the date, or the epoch's unit.")
    return moment.isoformat()


def validate_comment(comment: Any) -> str:
    cleaned = "" if is_unset(comment) else str(comment).strip()
    if len(cleaned) > NOTE_COMMENT_MAX:
        raise NoteError(f"Note comment is {len(cleaned)} characters; the limit is {NOTE_COMMENT_MAX}. Shorten it by {len(cleaned) - NOTE_COMMENT_MAX}.")
    return cleaned


def validate_text(text: Any) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        raise NoteError("Entry text is required and can't be empty.")
    if len(cleaned) > NOTE_TEXT_MAX:
        raise NoteError(
            f"Entry text is {len(cleaned)} characters; the limit is {NOTE_TEXT_MAX} (over by {len(cleaned) - NOTE_TEXT_MAX}). "
            "Shorten it, or split it across several entries with append_note."
        )
    return cleaned


# --------------------------------------------------------------------------- #
# Database operations (synchronous; the tool methods run them via asyncio.to_thread)
# --------------------------------------------------------------------------- #

def _not_found(conn: sqlite3.Connection, name: str) -> NoteError:
    names = [r["note_name"] for r in conn.execute("SELECT note_name FROM note_id")]
    if not names:
        return NoteError(f"No note named {name!r}, and no notes exist yet - use create_note to start one.")
    by_lower = {n.lower(): n for n in names}
    close = [by_lower[m] for m in difflib.get_close_matches(name.lower(), list(by_lower), n=3, cutoff=0.5)]
    hint = f" Did you mean: {', '.join(repr(c) for c in close)}?" if close else " Use list_notes to see what exists."
    return NoteError(f"No note named {name!r}.{hint}")


def _get_note(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM note_id WHERE lower(note_name) = lower(?)", (name,)).fetchone()
    if row is None:
        raise _not_found(conn, name)
    return row


def _get_entry(conn: sqlite3.Connection, note: sqlite3.Row, entry_no: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM note_data WHERE note_pk = ? AND entry_no = ?", (note["note_pk"], entry_no)).fetchone()
    if row is None:
        have = [r["entry_no"] for r in conn.execute("SELECT entry_no FROM note_data WHERE note_pk = ? ORDER BY entry_no", (note["note_pk"],))]
        listing = f"It has entries {have[0]}-{have[-1]}" + ("" if len(have) == have[-1] - have[0] + 1 else f" (numbers in use: {have})") if have else "It has no entries"
        raise NoteError(f"Note {note['note_name']!r} has no entry #{entry_no}. {listing}.")
    return row


def _name_taken_error(name: str) -> NoteError:
    return NoteError(f"A note named {name!r} already exists (names are matched ignoring case).")


def create_note_db(db_path: str, name: str, text: str, comment: str, author: str, created_at: Optional[str] = None) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        now = created_at or _now_iso()  # the note's and first entry's time; the new author row always gets the real time
        try:
            with _write_txn(conn):
                existing = conn.execute(
                    "SELECT n.note_name, (SELECT COUNT(*) FROM note_data d WHERE d.note_pk = n.note_pk) AS entries "
                    "FROM note_id n WHERE lower(n.note_name) = lower(?)",
                    (name,),
                ).fetchone()
                if existing:
                    raise NoteError(
                        f"A note named {existing['note_name']!r} already exists ({existing['entries']} entries). "
                        "Use append_note to add to it, or choose a different name."
                    )
                author_id = _author_id(conn, author, _now_iso())
                cur = conn.execute(
                    "INSERT INTO note_id (note_name, note_comment, next_entry_no, created_at, updated_at, author_id) VALUES (?, ?, 2, ?, ?, ?)",
                    (name, comment, now, now, author_id),
                )
                conn.execute(
                    "INSERT INTO note_data (note_pk, entry_no, note_text, created_at, author_id) VALUES (?, 1, ?, ?, ?)",
                    (cur.lastrowid, text, now, author_id),
                )
        except sqlite3.IntegrityError:  # lost a race with another writer creating the same name
            raise _name_taken_error(name)
        return {"note_name": name, "entry_no": 1, "created_at": now, "author": author}
    finally:
        conn.close()


def append_entry_db(db_path: str, name: str, text: str, author: str, created_at: Optional[str] = None) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            now = created_at or _now_iso()
            entry_no = note["next_entry_no"]
            conn.execute(
                "INSERT INTO note_data (note_pk, entry_no, note_text, created_at, author_id) VALUES (?, ?, ?, ?, ?)",
                (note["note_pk"], entry_no, text, now, _author_id(conn, author, _now_iso())),
            )
            # a backdated entry never moves the note's last-updated time backwards
            conn.execute(
                "UPDATE note_id SET next_entry_no = ?, updated_at = max(updated_at, ?) WHERE note_pk = ?", (entry_no + 1, now, note["note_pk"])
            )
        return {"note_name": note["note_name"], "entry_no": entry_no, "created_at": now, "author": author}
    finally:
        conn.close()


def read_note_db(db_path: str, name: str, limit: int) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        note = _get_note(conn, name)
        total = conn.execute("SELECT COUNT(*) FROM note_data WHERE note_pk = ?", (note["note_pk"],)).fetchone()[0]
        rows = conn.execute(
            "SELECT d.entry_no, d.note_text, d.created_at, d.edited_at, a.author_name FROM note_data d "
            "LEFT JOIN note_author a ON a.author_id = d.author_id WHERE d.note_pk = ? "
            "ORDER BY d.created_at DESC, d.entry_no DESC LIMIT ?",
            (note["note_pk"], limit),
        ).fetchall()
        starter = conn.execute("SELECT author_name FROM note_author WHERE author_id = ?", (note["author_id"],)).fetchone()
        return {"note": note, "started_by": starter["author_name"] if starter else None, "total": total, "entries": list(reversed(rows))}
    finally:
        conn.close()


def list_notes_db(db_path: str, limit: int) -> "tuple[int, List[Any]]":
    conn = notes_db_connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM note_id").fetchone()[0]
        rows = conn.execute(
            "SELECT n.note_name, n.note_comment, n.updated_at, COUNT(d.entry_no) AS entries, a.author_name "
            "FROM note_id n LEFT JOIN note_data d ON d.note_pk = n.note_pk LEFT JOIN note_author a ON a.author_id = n.author_id "
            "GROUP BY n.note_pk ORDER BY n.updated_at DESC, n.note_pk DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return total, rows
    finally:
        conn.close()


def search_notes_db(db_path: str, query: str, limit: int) -> "tuple[List[Any], List[Any]]":
    like = f"%{_like_escape(query)}%"
    conn = notes_db_connect(db_path)
    try:
        notes = conn.execute(
            "SELECT note_name, note_comment FROM note_id "
            "WHERE note_name LIKE ? ESCAPE '\\' OR note_comment LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        entries = conn.execute(
            "SELECT n.note_name, d.entry_no, d.note_text, a.author_name FROM note_data d JOIN note_id n ON n.note_pk = d.note_pk "
            "LEFT JOIN note_author a ON a.author_id = d.author_id "
            "WHERE d.note_text LIKE ? ESCAPE '\\' ORDER BY n.updated_at DESC, d.entry_no LIMIT ?",
            (like, limit),
        ).fetchall()
        return notes, entries
    finally:
        conn.close()


def edit_entry_db(db_path: str, name: str, entry_no: int, text: str) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            _get_entry(conn, note, entry_no)
            now = _now_iso()
            conn.execute("UPDATE note_data SET note_text = ?, edited_at = ? WHERE note_pk = ? AND entry_no = ?", (text, now, note["note_pk"], entry_no))
            conn.execute("UPDATE note_id SET updated_at = ? WHERE note_pk = ?", (now, note["note_pk"]))
        return {"note_name": note["note_name"], "entry_no": entry_no, "edited_at": now}
    finally:
        conn.close()


def delete_entry_db(db_path: str, name: str, entry_no: int) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            _get_entry(conn, note, entry_no)
            conn.execute("DELETE FROM note_data WHERE note_pk = ? AND entry_no = ?", (note["note_pk"], entry_no))
            conn.execute("UPDATE note_id SET updated_at = ? WHERE note_pk = ?", (_now_iso(), note["note_pk"]))
            remaining = conn.execute("SELECT COUNT(*) FROM note_data WHERE note_pk = ?", (note["note_pk"],)).fetchone()[0]
        return {"note_name": note["note_name"], "entry_no": entry_no, "remaining": remaining}
    finally:
        conn.close()


def update_note_db(db_path: str, name: str, new_name: Optional[str], comment: Optional[str]) -> Dict[str, Any]:
    """new_name/comment of None mean 'leave unchanged'; pass comment='' to clear it."""
    conn = notes_db_connect(db_path)
    try:
        try:
            with _write_txn(conn):
                note = _get_note(conn, name)
                final_name = note["note_name"] if new_name is None else new_name
                final_comment = note["note_comment"] if comment is None else comment
                if final_name != note["note_name"]:
                    clash = conn.execute(
                        "SELECT 1 FROM note_id WHERE lower(note_name) = lower(?) AND note_pk != ?", (final_name, note["note_pk"])
                    ).fetchone()
                    if clash:
                        raise _name_taken_error(final_name)
                conn.execute(
                    "UPDATE note_id SET note_name = ?, note_comment = ?, updated_at = ? WHERE note_pk = ?",
                    (final_name, final_comment, _now_iso(), note["note_pk"]),
                )
        except sqlite3.IntegrityError:
            raise _name_taken_error(new_name or name)
        return {"old_name": note["note_name"], "note_name": final_name, "note_comment": final_comment}
    finally:
        conn.close()


def delete_note_db(db_path: str, name: str) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            entries = conn.execute("SELECT COUNT(*) FROM note_data WHERE note_pk = ?", (note["note_pk"],)).fetchone()[0]
            conn.execute("DELETE FROM note_id WHERE note_pk = ?", (note["note_pk"],))  # cascades to note_data
        return {"note_name": note["note_name"], "entries_removed": entries}
    finally:
        conn.close()


def _error(e: Exception) -> Dict[str, Any]:
    if isinstance(e, sqlite3.Error):
        logger.exception("agent_notes database error")
        return {"success": False, "error": f"Database error: {e}"}
    return {"success": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# Open WebUI tool
# --------------------------------------------------------------------------- #

class Tools:
    class Valves(BaseModel):
        NOTES_DB_PATH: str = Field(
            default="/app/backend/data/comfy_outputs/agent_notes.sqlite3",
            description="SQLite database file for agent notes. Deliberately separate from the ComfyUI job database; keep it in the same folder.",
        )
        MAX_READ_ENTRIES: int = Field(default=50, description="Caps how many of a note's most recent entries read_note returns at once.")
        MAX_LIST_RESULTS: int = Field(default=50, description="Caps list_notes results.")
        MAX_SEARCH_RESULTS: int = Field(default=30, description="Caps each of search_notes' two result lists (matching notes, matching entries).")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def list_notes(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        List the notes that exist, most recently changed first, as a Markdown table (name, number
        of entries, last updated, comment). Start here to see whether a note already exists
        before creating one. Call read_note to see a note's contents.

        :param limit: Maximum notes returned, default 20. Pass "" (or omit) for the default.
        """
        v = self.valves
        try:
            cap = min(_to_int(limit, 20, "limit"), v.MAX_LIST_RESULTS)
            total, rows = await asyncio.to_thread(list_notes_db, v.NOTES_DB_PATH, cap)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        if not rows:
            return {"success": True, "count": 0, "total_notes": 0, "table": "No notes yet - use create_note to start one."}
        lines = ["| Note | Entries | Last updated (UTC) | Started by | Comment |", "|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {_cell(r['note_name'], NOTE_NAME_MAX)} | {r['entries']} | {r['updated_at'][:16].replace('T', ' ')} "
                f"| {_cell(r['author_name'] or 'unknown', AUTHOR_NAME_MAX)} | {_cell(r['note_comment'])} |"
            )
        out: Dict[str, Any] = {"success": True, "count": len(rows), "total_notes": total, "table": "\n".join(lines)}
        if total > len(rows):
            out["note"] = f"Showing {len(rows)} of {total} notes; pass a larger limit (max {v.MAX_LIST_RESULTS}) for more."
        return out

    async def read_note(self, name: str, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Read a note: its comment and its entries in chronological order, each with its entry
        number. If the note has more entries than fit, you get the most recent ones and the
        result says so.

        :param name: The note's name, as shown by list_notes (case doesn't matter).
        :param limit: How many of the most recent entries to return. Pass "" (or omit) for the maximum allowed.
        """
        v = self.valves
        try:
            cap = min(_to_int(limit, v.MAX_READ_ENTRIES, "limit"), v.MAX_READ_ENTRIES)
            result = await asyncio.to_thread(read_note_db, v.NOTES_DB_PATH, validate_name(name), cap)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        entries = []
        for r in result["entries"]:
            item = {"entry_no": r["entry_no"], "author": r["author_name"] or "unknown", "created_at": r["created_at"], "text": r["note_text"]}
            if r["edited_at"]:
                item["edited_at"] = r["edited_at"]
            entries.append(item)
        out: Dict[str, Any] = {
            "success": True,
            "note_name": result["note"]["note_name"],
            "note_comment": result["note"]["note_comment"],
            "started_by": result["started_by"] or "unknown",
            "total_entries": result["total"],
            "entries": entries,
        }
        if result["total"] > len(entries):
            out["note"] = (
                f"Showing the {len(entries)} most recent of {result['total']} entries. "
                f"Pass a larger limit (max {v.MAX_READ_ENTRIES}) to see more."
            )
        return out

    async def create_note(
        self, name: str, text: str, comment: Optional[str] = None, author_name: Optional[str] = None,
        created_at: Optional[str] = None, __model__: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Start a brand-new note with its first entry. Names are unique (ignoring case): if a note
        with this name already exists, nothing is created - use append_note to add to it instead.

        LIMITS (a call over any of them is refused, nothing is saved): note name 60 characters,
        comment 500, each entry's text 500. Keep entries short and self-contained; put longer
        material in several entries (append_note) rather than one long one.

        :param name: A short, descriptive name (at most 60 characters), e.g. "character-bios" or "Chapter 3 plot points". This is how you and other agents refer to the note from now on.
        :param text: The first entry's text, at most 500 characters.
        :param comment: Optional one-line description of what the note is for, at most 500 characters. Pass "" (or omit) for none.
        :param author_name: Who is writing this, at most 60 characters. Pass "" (or omit) to be recorded under your model's name.
        :param created_at: Only when copying in an older note: when it was originally written, as ISO 8601 (2026-03-01T14:30:00Z, or just 2026-03-01) or a Unix epoch number. Pass "" (or omit) for the current time, which is right for anything new.
        """
        try:
            result = await asyncio.to_thread(
                create_note_db,
                self.valves.NOTES_DB_PATH,
                validate_name(name),
                validate_text(text),
                validate_comment(comment),
                resolve_author(author_name, __model__),
                validate_timestamp(created_at),
            )
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result, "note": "Created. Use append_note to add more entries."}

    async def append_note(
        self, name: str, text: str, author_name: Optional[str] = None, created_at: Optional[str] = None, __model__: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Add a new entry to the end of an existing note. This never changes earlier entries - use
        it for anything new you want to record. To fix an earlier entry use edit_entry; to
        start a new note use create_note.

        LIMIT: each entry's text is at most 500 characters (a longer one is refused, nothing is
        saved). For longer content, make several append_note calls, one idea per entry.

        :param name: The note's name, as shown by list_notes (case doesn't matter).
        :param text: The new entry's text, at most 500 characters. Longer content: split it across several append_note calls.
        :param author_name: Who is writing this, at most 60 characters. Pass "" (or omit) to be recorded under your model's name.
        :param created_at: Only when copying in an older note: when this entry was originally written, as ISO 8601 (2026-03-01T14:30:00Z, or just 2026-03-01) or a Unix epoch number. Pass "" (or omit) for the current time, which is right for anything new. Entries read back in time order, so a backdated entry appears before newer ones even though its number is higher.
        """
        try:
            result = await asyncio.to_thread(
                append_entry_db,
                self.valves.NOTES_DB_PATH,
                validate_name(name),
                validate_text(text),
                resolve_author(author_name, __model__),
                validate_timestamp(created_at),
            )
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def edit_entry(self, name: str, entry_no: int, text: str) -> Dict[str, Any]:
        """
        Replace the text of one existing entry, keeping its number and its place in the note.

        LIMIT: the new text is at most 500 characters (a longer one is refused, the entry is left
        as it was). If the rewrite won't fit, shorten it and put the rest in append_note.

        :param name: The note's name, as shown by list_notes (case doesn't matter).
        :param entry_no: The entry's number, as shown by read_note (e.g. 3 or "#3").
        :param text: The entry's new text, at most 500 characters.
        """
        try:
            number = _to_int(entry_no, None, "entry_no")
            if number is None:
                raise NoteError("entry_no is required - read_note shows each entry's number.")
            result = await asyncio.to_thread(edit_entry_db, self.valves.NOTES_DB_PATH, validate_name(name), number, validate_text(text))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def delete_entry(self, name: str, entry_no: int) -> Dict[str, Any]:
        """
        Permanently delete one entry from a note. The other entries keep their numbers (numbers
        are never reused).

        :param name: The note's name, as shown by list_notes (case doesn't matter).
        :param entry_no: The entry's number, as shown by read_note (e.g. 3 or "#3").
        """
        try:
            number = _to_int(entry_no, None, "entry_no")
            if number is None:
                raise NoteError("entry_no is required - read_note shows each entry's number.")
            result = await asyncio.to_thread(delete_entry_db, self.valves.NOTES_DB_PATH, validate_name(name), number)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def update_note(
        self, name: str, new_name: Optional[str] = None, comment: Optional[str] = None, clear_comment: bool = False
    ) -> Dict[str, Any]:
        """
        Rename a note and/or change its comment. Entries are untouched. Give at least one of
        new_name, comment, or clear_comment.

        LIMITS: note name 60 characters, comment 500 (a call over either is refused, nothing is
        changed).

        :param name: The note's current name, as shown by list_notes (case doesn't matter).
        :param new_name: The new name (at most 60 characters; must not match another note's name). Pass "" (or omit) to keep the current name.
        :param comment: The new comment (at most 500 characters). Pass "" (or omit) to keep the current comment.
        :param clear_comment: true to erase the comment entirely. Pass false (the normal choice) otherwise.
        """
        try:
            wants_name = not is_unset(new_name)
            wants_comment = not is_unset(comment)
            clearing = _to_bool(clear_comment)
            if not (wants_name or wants_comment or clearing):
                raise NoteError("Nothing to update - give new_name, comment, or clear_comment=true.")
            if clearing and wants_comment:
                raise NoteError("Give either comment or clear_comment=true, not both.")
            result = await asyncio.to_thread(
                update_note_db,
                self.valves.NOTES_DB_PATH,
                validate_name(name),
                validate_name(new_name) if wants_name else None,
                "" if clearing else (validate_comment(comment) if wants_comment else None),
            )
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def delete_note(self, name: str) -> Dict[str, Any]:
        """
        Permanently delete a note and every entry in it. This cannot be undone and asks for no
        confirmation, so be sure - read_note first if unsure.

        :param name: The note's name, as shown by list_notes (case doesn't matter).
        """
        try:
            result = await asyncio.to_thread(delete_note_db, self.valves.NOTES_DB_PATH, validate_name(name))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def search_notes(self, query: str, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Search every note for some text (case-insensitive substring). Matches note names and
        comments, and the text of individual entries; returns both lists so you can find which
        note holds something without reading them all.

        :param query: The text to look for.
        :param limit: Maximum matches in each list, default 10. Pass "" (or omit) for the default.
        """
        v = self.valves
        try:
            if not query or not str(query).strip():
                raise NoteError("query is required.")
            cap = min(_to_int(limit, 10, "limit"), v.MAX_SEARCH_RESULTS)
            notes, entries = await asyncio.to_thread(search_notes_db, v.NOTES_DB_PATH, str(query).strip(), cap)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {
            "success": True,
            "matching_notes": [{"note_name": r["note_name"], "note_comment": r["note_comment"]} for r in notes],
            "matching_entries": [
                {"note_name": r["note_name"], "entry_no": r["entry_no"], "author": r["author_name"] or "unknown", "text": r["note_text"]}
                for r in entries
            ],
        }
