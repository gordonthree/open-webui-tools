"""
title: Agent Notes
author: Gordon
version: 1.7.0
description: A persistent notebook for agents, easier to use than Open WebUI's built-in note tool.
    A note is a name plus an append-only log of short numbered entries (500 characters each), kept
    in its own SQLite database. To add to a note, append an entry - nothing is ever rewritten or
    diffed. Notes are addressed by their plain-language name, entries by their number.
    agent_notes_list/agent_notes_read/agent_notes_search browse; agent_notes_create/agent_notes_append add; agent_notes_edit_entry/
    agent_notes_delete_entry/agent_notes_update/agent_notes_delete maintain. Every delete is permanent.

    Each note can also carry one short summary (agent_notes_update_summary/agent_notes_search_summary/agent_notes_delete_summary),
    meant to be kept current by a scheduled sub agent so other agents can find the right note by
    searching summaries instead of reading everything.

    Notes can be tagged (agent_notes_add_tags/agent_notes_remove_tag/agent_notes_list_tags/agent_notes_rename_tag) so related notes can be found
    together. Tags are short, lowercase, and shared across notes; when a new tag looks like an
    existing one the tool says so instead of creating a near-duplicate.

    Every note and entry records its author: pass author_name when writing, or the tool uses the
    name of the model Open WebUI says is calling it. Entries are stamped with the current time
    unless the caller passes created_at, which lets old notes be moved in with their real dates.

    Limits (also stated in each writing method's docstring, which is what the model actually
    sees): note name 60 characters, note comment 500, each entry's text 500. Longer content goes
    in several entries, not one long one: continues=true on create/append does the splitting, verbatim.
"""

import asyncio
import contextlib
import datetime
import difflib
import json
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
SUMMARY_MAX = 500
CONTINUES_CHUNK_MAX = 470  # continues=true pieces: leaves room under NOTE_TEXT_MAX for " [continues at entry #12345]"
AUTHOR_NAME_MAX = 60
TAG_MAX = 40
MAX_TAGS_PER_NOTE = 10
TAGS_SHOWN = 40  # how many in-use tags agent_notes_list lists for the model to choose from
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
CREATE TABLE IF NOT EXISTS note_summary (
    note_pk INTEGER PRIMARY KEY REFERENCES note_id(note_pk) ON DELETE CASCADE,
    summary_text TEXT NOT NULL CHECK (length(summary_text) BETWEEN 1 AND {SUMMARY_MAX}),
    author_id INTEGER REFERENCES note_author(author_id),
    last_summarized TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
    source_updated_at TEXT NOT NULL,
    source_entries INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tag (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(tag_name) BETWEEN 1 AND {TAG_MAX})
);
CREATE TABLE IF NOT EXISTS note_tag (
    note_pk INTEGER NOT NULL REFERENCES note_id(note_pk) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tag(tag_id) ON DELETE CASCADE,
    PRIMARY KEY (note_pk, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_note_tag_tag ON note_tag(tag_id);
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
    conn.execute("PRAGMA foreign_keys=ON;")  # off by default in SQLite; agent_notes_delete relies on the cascade
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


def normalize_tag(text: Any) -> str:
    """Make any text a valid tag instead of refusing it: lowercase, '&' becomes 'and', apostrophes vanish, every
    other character outside letters/digits/. + # becomes a hyphen, and runs of hyphens collapse:
    'Habits & Interests' -> 'habits-and-interests', 'Comfy UI' -> 'comfy-ui'."""
    cleaned = str(text or "").strip().lower().replace("&", " and ")
    cleaned = re.sub(r"['\u2019`\"]", "", cleaned)
    cleaned = re.sub(r"-{2,}", "-", re.sub(r"[^\w.+#]+|_+", "-", cleaned)).strip("-")
    if not cleaned:
        raise NoteError(f"{str(text).strip()!r} has no letters or digits to make a tag from.")
    if len(cleaned) > TAG_MAX:
        raise NoteError(f"Tag {cleaned!r} is {len(cleaned)} characters; the limit is {TAG_MAX}. Use a shorter tag.")
    return cleaned


def tag_adjustments(value: Any) -> Dict[str, str]:
    """Tags whose characters had to be changed (beyond case and spacing), as {what was given: what it became}."""
    parts = value if isinstance(value, (list, tuple)) else str(value or "").replace(";", ",").split(",")
    return {str(p).strip(): normalize_tag(p) for p in parts if str(p).strip() and re.search(r"[^\w\s.+#-]", str(p))}


def split_tags(value: Any) -> List[str]:
    """One tag or several (comma-separated text, or a list), normalized and de-duplicated, in order."""
    parts = value if isinstance(value, (list, tuple)) else str(value or "").replace(";", ",").split(",")
    tags = list(dict.fromkeys(normalize_tag(p) for p in parts if str(p).strip()))
    if not tags:
        raise NoteError("At least one tag is required (several can be separated by commas).")
    if len(tags) > MAX_TAGS_PER_NOTE:
        raise NoteError(f"{len(tags)} tags in one call; a note holds at most {MAX_TAGS_PER_NOTE}.")
    return tags


def validate_summary(text: Any) -> str:
    cleaned = " ".join(str(text or "").split())  # a summary is one paragraph: collapse newlines and runs of spaces
    if not cleaned:
        raise NoteError("Summary text is required and can't be empty (use agent_notes_delete_summary to remove a summary).")
    if len(cleaned) > SUMMARY_MAX:
        raise NoteError(f"Summary is {len(cleaned)} characters; the limit is {SUMMARY_MAX} (over by {len(cleaned) - SUMMARY_MAX}). Tighten it.")
    return cleaned


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
            "Shorten it, or pass continues=true to have it saved as several entries, word for word."
        )
    return cleaned


def split_text(text: Any, max_parts: int) -> List[str]:
    """Cut text into pieces of at most CONTINUES_CHUNK_MAX characters for agent_notes_create/_append continues=true (the
    database layer then adds a "continues at" marker to all but the last). The words are kept exactly as given: a cut
    falls on a paragraph break if one is near, else a line break, else a space (only that break itself is dropped),
    and only in a word longer than a whole piece does it fall mid-word."""
    rest = str(text or "").strip()
    if not rest:
        raise NoteError("Entry text is required and can't be empty.")
    parts: List[str] = []
    while len(rest) > NOTE_TEXT_MAX:  # fits in one entry as is: no pieces, no marker
        window = rest[: CONTINUES_CHUNK_MAX + 1]
        cut = next((c for c in (window.rfind("\n\n"), window.rfind("\n"), window.rfind(" ")) if c >= CONTINUES_CHUNK_MAX // 2), CONTINUES_CHUNK_MAX)
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    parts.append(rest)
    if len(parts) > max_parts:
        raise NoteError(
            f"Entry text is {len(str(text).strip())} characters, which would take {len(parts)} entries; at most {max_parts} "
            f"(about {max_parts * CONTINUES_CHUNK_MAX} characters) can be saved in one call. Nothing was saved. "
            "Save the first part now and the rest in a later agent_notes_append call."
        )
    return [p for p in parts if p]


def entry_parts(text: Any, continues: Any, max_parts: int) -> List[str]:
    """The text as the entries to save: one, or with continues=true as many as it takes (up to max_parts)."""
    return split_text(text, max(1, max_parts)) if _to_bool(continues) else [validate_text(text)]


# --------------------------------------------------------------------------- #
# Database operations (synchronous; the tool methods run them via asyncio.to_thread)
# --------------------------------------------------------------------------- #

def _not_found(conn: sqlite3.Connection, name: str) -> NoteError:
    names = [r["note_name"] for r in conn.execute("SELECT note_name FROM note_id")]
    if not names:
        return NoteError(f"No note named {name!r}, and no notes exist yet - use agent_notes_create to start one.")
    by_lower = {n.lower(): n for n in names}
    close = [by_lower[m] for m in difflib.get_close_matches(name.lower(), list(by_lower), n=3, cutoff=0.5)]
    hint = f" Did you mean: {', '.join(repr(c) for c in close)}?" if close else " Use agent_notes_list to see what exists."
    return NoteError(f"No note named {name!r}.{hint}")


def _get_note(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM note_id WHERE lower(note_name) = lower(?)", (name,)).fetchone()
    if row is None:
        raise _not_found(conn, name)
    return row


def _get_entry(conn: sqlite3.Connection, note: sqlite3.Row, entry_no: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM note_data WHERE note_pk = ? AND entry_no = ?", (note["note_pk"], entry_no)).fetchone()
    if row is None:
        have = [r["entry_no"] for r in conn.execute("SELECT entry_no FROM note_data WHERE note_pk = ? ORDER BY entry_no", (note["note_pk"],))]
        listing = f"It has entries {have[0]}-{have[-1]}" + ("" if len(have) == have[-1] - have[0] + 1 else f" (numbers in use: {have})") if have else "It has no entries"
        raise NoteError(f"Note {note['note_name']!r} has no entry #{entry_no}. {listing}.")
    return row


def _name_taken_error(name: str) -> NoteError:
    return NoteError(f"A note named {name!r} already exists (names are matched ignoring case).")


# "This summary no longer matches the note": the note changed (updated_at moved, which covers edits and
# deletes) or has a different number of entries (which covers a backdated append, since that deliberately
# leaves updated_at alone). Needs the note_summary row as `s` and the note_id row as `n`.
_STALE_SQL = "(s.source_updated_at != n.updated_at OR s.source_entries != (SELECT COUNT(*) FROM note_data x WHERE x.note_pk = n.note_pk))"


def create_note_db(db_path: str, name: str, text: "str | List[str]", comment: str, author: str, created_at: Optional[str] = None) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        now = created_at or _now_iso()  # the note's and first entry's time; the new author row always gets the real time
        parts = _link_parts([text] if isinstance(text, str) else list(text), 1)
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
                        "Use agent_notes_append to add to it, or choose a different name."
                    )
                author_id = _author_id(conn, author, _now_iso())
                cur = conn.execute(
                    "INSERT INTO note_id (note_name, note_comment, next_entry_no, created_at, updated_at, author_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, comment, len(parts) + 1, now, now, author_id),
                )
                conn.executemany(
                    "INSERT INTO note_data (note_pk, entry_no, note_text, created_at, author_id) VALUES (?, ?, ?, ?, ?)",
                    [(cur.lastrowid, i, part, now, author_id) for i, part in enumerate(parts, 1)],
                )
        except sqlite3.IntegrityError:  # lost a race with another writer creating the same name
            raise _name_taken_error(name)
        return {"note_name": name, **_entry_numbers(1, len(parts)), "created_at": now, "author": author}
    finally:
        conn.close()


def _link_parts(parts: List[str], first_no: int) -> List[str]:
    """Several pieces of one long text: end each but the last with a pointer to the next, so a reader (or a search hit)
    in the middle can tell there's more. A single piece is left alone."""
    return [f"{p} [continues at entry #{first_no + i + 1}]" if i < len(parts) - 1 else p for i, p in enumerate(parts)]


def _entry_numbers(first: int, count: int) -> Dict[str, Any]:
    return {"entry_no": first} if count == 1 else {"entry_no": first, "entries_saved": count, "entry_numbers": f"{first}-{first + count - 1}"}


def append_entry_db(db_path: str, name: str, text: "str | List[str]", author: str, created_at: Optional[str] = None) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            now = created_at or _now_iso()
            entry_no = note["next_entry_no"]
            parts = _link_parts([text] if isinstance(text, str) else list(text), entry_no)
            author_id = _author_id(conn, author, _now_iso())
            conn.executemany(
                "INSERT INTO note_data (note_pk, entry_no, note_text, created_at, author_id) VALUES (?, ?, ?, ?, ?)",
                [(note["note_pk"], entry_no + i, part, now, author_id) for i, part in enumerate(parts)],
            )
            # a backdated entry never moves the note's last-updated time backwards
            conn.execute(
                "UPDATE note_id SET next_entry_no = ?, updated_at = max(updated_at, ?) WHERE note_pk = ?", (entry_no + len(parts), now, note["note_pk"])
            )
        return {"note_name": note["note_name"], **_entry_numbers(entry_no, len(parts)), "created_at": now, "author": author}
    finally:
        conn.close()


def update_summary_db(db_path: str, name: str, summary: str, author: str) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            now = _now_iso()
            entries = conn.execute("SELECT COUNT(*) FROM note_data WHERE note_pk = ?", (note["note_pk"],)).fetchone()[0]
            existed = conn.execute("SELECT 1 FROM note_summary WHERE note_pk = ?", (note["note_pk"],)).fetchone() is not None
            conn.execute(
                "INSERT INTO note_summary (note_pk, summary_text, author_id, last_summarized, source_updated_at, source_entries) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(note_pk) DO UPDATE SET summary_text = excluded.summary_text, "
                "author_id = excluded.author_id, last_summarized = excluded.last_summarized, "
                "source_updated_at = excluded.source_updated_at, source_entries = excluded.source_entries",
                (note["note_pk"], summary, _author_id(conn, author, now), now, note["updated_at"], entries),
            )
        return {"note_name": note["note_name"], "created": not existed, "last_summarized": now, "author": author, "entries_covered": entries}
    finally:
        conn.close()


def delete_summary_db(db_path: str, name: str) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            removed = conn.execute("DELETE FROM note_summary WHERE note_pk = ?", (note["note_pk"],)).rowcount
        if not removed:
            raise NoteError(f"Note {note['note_name']!r} has no summary to delete.")
        return {"note_name": note["note_name"]}
    finally:
        conn.close()


def search_summaries_db(db_path: str, query: str, limit: int) -> "tuple[List[Any], int]":
    """Match any word of the query against note names and summaries; most matching words first."""
    words = list(dict.fromkeys(query.lower().split()))[:8]
    if not words:
        raise NoteError("query is required.")
    likes = [f"%{_like_escape(w)}%" for w in words]
    score = " + ".join("(n.note_name LIKE ? ESCAPE '\\' OR s.summary_text LIKE ? ESCAPE '\\')" for _ in words)
    params: List[Any] = [x for like in likes for x in (like, like)]
    conn = notes_db_connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM (SELECT n.note_name, s.summary_text, s.last_summarized, a.author_name, {_STALE_SQL} AS stale, ({score}) AS score "
            "FROM note_summary s JOIN note_id n ON n.note_pk = s.note_pk LEFT JOIN note_author a ON a.author_id = s.author_id) "
            "WHERE score > 0 ORDER BY score DESC, last_summarized DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return rows, len(words)
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
        summary = conn.execute(
            f"SELECT s.summary_text, s.last_summarized, a.author_name, {_STALE_SQL} AS stale "
            "FROM note_summary s JOIN note_id n ON n.note_pk = s.note_pk LEFT JOIN note_author a ON a.author_id = s.author_id WHERE s.note_pk = ?",
            (note["note_pk"],),
        ).fetchone()
        return {
            "note": note,
            "started_by": starter["author_name"] if starter else None,
            "tags": _note_tags(conn, note["note_pk"]),
            "summary": summary,
            "total": total,
            "entries": list(reversed(rows)),
        }
    finally:
        conn.close()


def list_notes_db(db_path: str, limit: int, needs_summary: bool = False, tag: Optional[str] = None, min_summary_chars: int = 0) -> "tuple[int, List[Any]]":
    """min_summary_chars: a note with no summary whose entries total fewer characters than this is 'short' (cheap to just
    read, and a summary of it would only copy it) and isn't offered to a summarizer. 0 turns that off."""
    conn = notes_db_connect(db_path)
    try:
        if tag is not None:
            counts = _tag_counts(conn)
            match = tag if tag in counts else next((n for n in counts if _tag_key(n) == _tag_key(tag)), None)
            if match is None:
                similar = _similar_tags(tag, counts)
                raise NoteError(f"No note is tagged {tag!r}." + (f" Similar tags: {', '.join(repr(n) for n in similar)}." if similar else " Use agent_notes_list_tags to see the tags in use."))
            tag = match
        rows = conn.execute(
            "SELECT * FROM (SELECT n.note_pk, n.note_name, n.note_comment, n.updated_at, COUNT(d.entry_no) AS entries, a.author_name, "
            f"CASE WHEN s.note_pk IS NULL THEN CASE WHEN (SELECT COALESCE(SUM(length(x.note_text)), 0) FROM note_data x WHERE x.note_pk = n.note_pk) < {int(min_summary_chars)} THEN 'short' ELSE 'none' END "
            f"WHEN {_STALE_SQL} THEN 'stale' ELSE 'current' END AS summary_state, "
            "(SELECT group_concat(tag_name, ', ') FROM (SELECT t.tag_name FROM note_tag nt JOIN tag t ON t.tag_id = nt.tag_id WHERE nt.note_pk = n.note_pk ORDER BY t.tag_name)) AS tags "
            "FROM note_id n LEFT JOIN note_data d ON d.note_pk = n.note_pk LEFT JOIN note_author a ON a.author_id = n.author_id "
            "LEFT JOIN note_summary s ON s.note_pk = n.note_pk GROUP BY n.note_pk) "
            "AS x WHERE (? = 0 OR summary_state NOT IN ('current', 'short')) "
            "AND (? IS NULL OR EXISTS (SELECT 1 FROM note_tag nt JOIN tag t ON t.tag_id = nt.tag_id WHERE nt.note_pk = x.note_pk AND t.tag_name = ?)) "
            "ORDER BY updated_at DESC, note_pk DESC",
            (1 if needs_summary else 0, tag, tag),
        ).fetchall()
        return len(rows), rows[:limit]
    finally:
        conn.close()


def search_notes_db(db_path: str, query: str, limit: int) -> "tuple[List[Any], List[Any]]":
    like = f"%{_like_escape(query)}%"
    conn = notes_db_connect(db_path)
    try:
        notes = conn.execute(
            "SELECT note_name, note_comment FROM note_id "
            "WHERE note_name LIKE ? ESCAPE '\\' OR note_comment LIKE ? ESCAPE '\\' "
            "OR note_pk IN (SELECT nt.note_pk FROM note_tag nt JOIN tag t ON t.tag_id = nt.tag_id WHERE t.tag_name LIKE ? ESCAPE '\\') "
            "ORDER BY updated_at DESC LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
        entries = conn.execute(
            "SELECT n.note_name, d.entry_no, d.note_text, a.author_name FROM note_data d JOIN note_id n ON n.note_pk = d.note_pk "
            "LEFT JOIN note_author a ON a.author_id = d.author_id "
            "WHERE d.note_text LIKE ? ESCAPE '\\' ORDER BY d.created_at DESC, d.entry_no DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        return notes, entries
    finally:
        conn.close()


def edit_entry_db(db_path: str, name: str, entry_no: str, text: str) -> Dict[str, Any]:
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


def delete_entry_db(db_path: str, name: str, entry_no: str) -> Dict[str, Any]:
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


def update_note_db(db_path: str, name: str, new_name: Optional[str], comment: Optional[str], new_author: Optional[str] = None) -> Dict[str, Any]:
    """new_name/comment/new_author of None mean 'leave unchanged'; pass comment='' to clear it. new_author changes who
    the note is recorded as started by (its entries keep their own authors). A change of author alone doesn't touch
    updated_at, so it never makes the note's summary stale."""
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
                if new_name is not None or comment is not None:
                    conn.execute(
                        "UPDATE note_id SET note_name = ?, note_comment = ?, updated_at = ? WHERE note_pk = ?",
                        (final_name, final_comment, _now_iso(), note["note_pk"]),
                    )
                if new_author is not None:
                    conn.execute("UPDATE note_id SET author_id = ? WHERE note_pk = ?", (_author_id(conn, new_author, _now_iso()), note["note_pk"]))
        except sqlite3.IntegrityError:
            raise _name_taken_error(new_name or name)
        out = {"old_name": note["note_name"], "note_name": final_name, "note_comment": final_comment}
        if new_author is not None:
            out["author"] = new_author
        return out
    finally:
        conn.close()


def _tag_key(name: str) -> str:
    """What two spellings of one tag share: no hyphens, simple plurals folded ('comfy-ui', 'comfyui' and 'ComfyUIs' match)."""
    key = name.replace("-", "")
    if len(key) > 4 and key.endswith("ies"):
        return key[:-3] + "y"
    if len(key) > 3 and key.endswith("s") and not key.endswith("ss"):
        return key[:-1]
    return key


def _tag_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("SELECT t.tag_name, COUNT(nt.note_pk) AS uses FROM tag t JOIN note_tag nt ON nt.tag_id = t.tag_id GROUP BY t.tag_id ORDER BY uses DESC, t.tag_name")
    return {r["tag_name"]: r["uses"] for r in rows}


def _similar_tags(tag: str, counts: Dict[str, int]) -> List[str]:
    """Existing tags this one could be a variant of: spelled alike, or one contains the other."""
    close = difflib.get_close_matches(tag, list(counts), n=3, cutoff=0.7)
    contained = [n for n in counts if len(tag) >= 3 and len(n) >= 3 and (tag in n or n in tag)]
    return list(dict.fromkeys(close + contained))[:4]


def _note_tags(conn: sqlite3.Connection, note_pk: int) -> List[str]:
    return [r["tag_name"] for r in conn.execute("SELECT t.tag_name FROM note_tag nt JOIN tag t ON t.tag_id = nt.tag_id WHERE nt.note_pk = ? ORDER BY t.tag_name", (note_pk,))]


def _purge_unused_tags(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tag WHERE tag_id NOT IN (SELECT tag_id FROM note_tag)")


def add_tags_db(db_path: str, name: str, tags: List[str], create: bool = False) -> Dict[str, Any]:
    """Tag a note. A tag matching an existing one (ignoring case, hyphens and simple plurals) reuses it.
    A new tag that merely resembles existing ones is refused unless create is true; nothing is saved if any tag is refused."""
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            counts = _tag_counts(conn)
            by_key = {_tag_key(n): n for n in counts}
            have = set(_note_tags(conn, note["note_pk"]))
            use: List[str] = []  # the existing-or-new tag name each request resolves to
            reused: Dict[str, str] = {}
            doubts: List[str] = []
            for tag in tags:
                target = tag if tag in counts else by_key.get(_tag_key(tag))
                if target is None:
                    similar = _similar_tags(tag, counts)
                    if similar and not create:
                        doubts.append(f"{tag!r} is new, but similar tags exist: " + ", ".join(f"{n!r} ({counts[n]})" for n in similar))
                        continue
                    target = tag
                elif target != tag:
                    reused[tag] = target
                if target not in use:
                    use.append(target)
            if doubts:
                raise NoteError(
                    "Nothing was tagged. " + "; ".join(doubts) + ". Retry with one of the existing tags; "
                    "only if none of them fit, pass create=true to make the new tag."
                )
            added = [t for t in use if t not in have]
            if len(have) + len(added) > MAX_TAGS_PER_NOTE:
                raise NoteError(f"Note {note['note_name']!r} has {len(have)} tags ({', '.join(sorted(have))}); adding {len(added)} would pass the limit of {MAX_TAGS_PER_NOTE}. Remove one first with agent_notes_remove_tag.")
            created = []
            for tag in added:
                row = conn.execute("SELECT tag_id FROM tag WHERE tag_name = ?", (tag,)).fetchone()
                if row is None:
                    row = {"tag_id": conn.execute("INSERT INTO tag (tag_name) VALUES (?)", (tag,)).lastrowid}
                    created.append(tag)
                conn.execute("INSERT INTO note_tag (note_pk, tag_id) VALUES (?, ?)", (note["note_pk"], row["tag_id"]))
            return {"note_name": note["note_name"], "added": added, "already_had": [t for t in use if t in have],
                    "new_tags": created, "matched_existing": reused, "tags": sorted(have | set(added))}
    finally:
        conn.close()


def remove_tag_db(db_path: str, name: str, tag: str) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            have = _note_tags(conn, note["note_pk"])
            target = tag if tag in have else next((h for h in have if _tag_key(h) == _tag_key(tag)), None)
            if target is None:
                raise NoteError(f"Note {note['note_name']!r} isn't tagged {tag!r}. " + (f"Its tags: {', '.join(have)}." if have else "It has no tags."))
            conn.execute("DELETE FROM note_tag WHERE note_pk = ? AND tag_id = (SELECT tag_id FROM tag WHERE tag_name = ?)", (note["note_pk"], target))
            _purge_unused_tags(conn)
            return {"note_name": note["note_name"], "removed": target, "tags": [h for h in have if h != target]}
    finally:
        conn.close()


def list_tags_db(db_path: str, query: Optional[str] = None) -> List[Any]:
    """Tags in use with their note counts, most used first; query keeps those containing it (or resembling it)."""
    conn = notes_db_connect(db_path)
    try:
        counts = _tag_counts(conn)
        if query:
            q = normalize_tag(query)
            keep = set(_similar_tags(q, counts)) | {n for n in counts if q in n}
            counts = {n: c for n, c in counts.items() if n in keep}
        return list(counts.items())
    finally:
        conn.close()


def rename_tag_db(db_path: str, tag: str, new_tag: str) -> Dict[str, Any]:
    """Rename a tag everywhere. If new_tag already exists the two are merged (notes with both keep one)."""
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            old = conn.execute("SELECT tag_id, tag_name FROM tag WHERE tag_name = ?", (tag,)).fetchone()
            if old is None:
                counts = _tag_counts(conn)
                similar = _similar_tags(tag, counts)
                raise NoteError(f"No tag named {tag!r}." + (f" Did you mean: {', '.join(repr(n) for n in similar)}?" if similar else " Use agent_notes_list_tags to see what exists."))
            target = conn.execute("SELECT tag_id, tag_name FROM tag WHERE tag_name = ?", (new_tag,)).fetchone()
            if target is None or target["tag_id"] == old["tag_id"]:
                conn.execute("UPDATE tag SET tag_name = ? WHERE tag_id = ?", (new_tag, old["tag_id"]))
                merged = False
            else:
                conn.execute("INSERT OR IGNORE INTO note_tag (note_pk, tag_id) SELECT note_pk, ? FROM note_tag WHERE tag_id = ?", (target["tag_id"], old["tag_id"]))
                conn.execute("DELETE FROM note_tag WHERE tag_id = ?", (old["tag_id"],))
                _purge_unused_tags(conn)
                merged = True
            notes_with = conn.execute("SELECT COUNT(*) FROM note_tag nt JOIN tag t ON t.tag_id = nt.tag_id WHERE t.tag_name = ?", (new_tag,)).fetchone()[0]
            return {"old_tag": old["tag_name"], "tag": new_tag, "merged": merged, "notes": notes_with}
    finally:
        conn.close()


def tagged_notes_db(db_path: str, tags: List[str], match_all: bool) -> "tuple[List[Any], List[str], List[str]]":
    """Notes carrying the given tags, best match first: (notes, tags found, tags that don't exist). Each note is a dict with its
    tags, summary and every entry (newest first), ready for pack_tagged."""
    conn = notes_db_connect(db_path)
    try:
        counts = _tag_counts(conn)
        by_key = {_tag_key(n): n for n in counts}
        found: List[str] = []
        missing: List[str] = []
        for tag in tags:
            target = tag if tag in counts else by_key.get(_tag_key(tag))
            if target is None:
                similar = _similar_tags(tag, counts)
                missing.append(tag + (f" (similar: {', '.join(similar)})" if similar else ""))
            elif target not in found:
                found.append(target)
        if not found or (match_all and missing):
            named = ", ".join(missing) if missing else ", ".join(tags)
            raise NoteError(f"No note is tagged {named}. Use agent_notes_list_tags to see the tags in use.")
        marks = ",".join("?" for _ in found)
        rows = conn.execute(
            f"SELECT n.note_pk, n.note_name, n.updated_at, COUNT(DISTINCT t.tag_id) AS matched FROM note_id n "
            f"JOIN note_tag nt ON nt.note_pk = n.note_pk JOIN tag t ON t.tag_id = nt.tag_id WHERE t.tag_name IN ({marks}) "
            f"GROUP BY n.note_pk HAVING matched >= ? ORDER BY matched DESC, n.updated_at DESC, n.note_pk DESC",
            found + [len(found) if match_all else 1],
        ).fetchall()
        notes = []
        for r in rows:
            note_tags = _note_tags(conn, r["note_pk"])
            summary = conn.execute("SELECT summary_text FROM note_summary WHERE note_pk = ?", (r["note_pk"],)).fetchone()
            entries = conn.execute(
                "SELECT d.entry_no, d.note_text, d.created_at, a.author_name FROM note_data d LEFT JOIN note_author a ON a.author_id = d.author_id "
                "WHERE d.note_pk = ? ORDER BY d.created_at DESC, d.entry_no DESC",
                (r["note_pk"],),
            ).fetchall()
            notes.append({
                "note_name": r["note_name"], "tags": note_tags, "matched_tags": [t for t in note_tags if t in found],
                "summary": summary["summary_text"] if summary else None,
                "entries": [{"entry_no": e["entry_no"], "author": e["author_name"] or "unknown", "created_at": e["created_at"], "text": e["note_text"]} for e in entries],
            })
        return notes, found, missing
    finally:
        conn.close()


_HEADING_EXTRAS = 70  # the "entries", "total_entries" and "entries_not_shown" keys pack_tagged adds to every note


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False)) + 2  # + the ", " that separates it from its neighbour


def _shape_tagged(note: Dict[str, Any], verbose: bool) -> Dict[str, Any]:
    """The note as the model will receive it. Plain mode keeps only what a reader needs: the name, the summary if
    there is one, and the entry texts as bare strings."""
    if verbose:
        return {**note, "_verbose": True}
    shaped: Dict[str, Any] = {"note_name": note["note_name"]}
    if note["summary"]:
        shaped["summary"] = note["summary"]
    shaped["entries"] = [e["text"] for e in note["entries"]]
    return shaped


def pack_tagged(notes: List[Dict[str, Any]], budget: int) -> "tuple[List[Dict[str, Any]], List[str]]":
    """Fit as much as possible of these notes into `budget` characters without ever cutting a string in half.
    Pass 1 admits each note's heading (name, tags, summary) in rank order, stopping at the first that doesn't fit.
    Pass 2 then adds entries newest-first, rotating across the admitted notes so one long note can't use the whole
    budget; a note stops growing the first time its next entry doesn't fit. Returns (notes with entries in time
    order, names of the notes left out)."""
    used = 0
    packed: List[Dict[str, Any]] = []
    for note in notes:
        cost = _HEADING_EXTRAS + _size({k: v for k, v in note.items() if k != "entries"})
        if used + cost > budget:
            break
        used += cost
        packed.append({**{k: v for k, v in note.items() if k != "entries"}, "total_entries": len(note["entries"]), "_pending": list(note["entries"]), "entries": []})
    open_notes = list(packed)
    while open_notes:
        for note in list(open_notes):
            if not note["_pending"]:
                open_notes.remove(note)
                continue
            cost = _size(note["_pending"][0])
            if used + cost > budget:
                open_notes.remove(note)  # soft stop: this note is done growing; others may still have shorter entries that fit
                continue
            used += cost
            note["entries"].append(note["_pending"].pop(0))
    for note in packed:
        note.pop("_pending")
        note["entries"].reverse()  # newest-first while packing; read in time order
        hidden = note.pop("total_entries") - len(note["entries"])
        verbose = note.pop("_verbose", False)
        if hidden or verbose:
            note["entries_not_shown"] = hidden
    return packed, [n["note_name"] for n in notes[len(packed):]]


def delete_note_db(db_path: str, name: str) -> Dict[str, Any]:
    conn = notes_db_connect(db_path)
    try:
        with _write_txn(conn):
            note = _get_note(conn, name)
            entries = conn.execute("SELECT COUNT(*) FROM note_data WHERE note_pk = ?", (note["note_pk"],)).fetchone()[0]
            conn.execute("DELETE FROM note_id WHERE note_pk = ?", (note["note_pk"],))  # cascades to note_data
            _purge_unused_tags(conn)
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
    # Number parameters are annotated str on purpose: Open WebUI runs int() on a string given for an
    # int/Optional[int] parameter *before* the method sees it, so the "" the docstrings tell models to
    # pass for "default" (or "#3" for an entry) would fail there. It converts a number to str for a
    # str parameter, and _to_int() does the parsing, so 5, "5", "" and "#3" all work.
    class Valves(BaseModel):
        NOTES_DB_PATH: str = Field(
            default="/app/backend/data/comfy_outputs/agent_notes.sqlite3",
            description="SQLite database file for agent notes. Deliberately separate from the ComfyUI job database; keep it in the same folder.",
        )
        MAX_READ_ENTRIES: int = Field(default=50, description="Caps how many of a note's most recent entries agent_notes_read returns at once.")
        MAX_LIST_RESULTS: int = Field(default=50, description="Caps agent_notes_list results.")
        MIN_SUMMARY_CHARS: int = Field(default=1000, description="A note whose entries total fewer characters than this isn't offered for summarizing (agent_notes_list needs_summary=true skips it, and shows 'short'): a summary of a note that small would just copy it. 0 summarizes everything.")
        MAX_TAGGED_CHARS: int = Field(default=6000, description="Character budget for agent_notes_read_tagged's reply (about a quarter as many tokens). Whole notes/entries only; anything that doesn't fit is left out and reported.")
        MAX_CONTINUATION_ENTRIES: int = Field(default=6, description="With continues=true, agent_notes_create/agent_notes_append split a long text into at most this many entries of up to 470 characters each (so 6 = about 2800 characters per call). A longer text is refused whole.")
        MAX_SEARCH_RESULTS: int = Field(default=30, description="Caps each of agent_notes_search' two result lists (matching notes, matching entries).")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def agent_notes_list(self, limit: Optional[str] = None, needs_summary: bool = False, tag: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
        """
        List the notes that exist, most recently changed first, as a Markdown table (name, number
        of entries, last updated, comment). Start here to see whether a note already exists
        before creating one. Call agent_notes_read to see a note's contents.

        The Summary column says whether the note has a summary: none, current, stale (the note
        changed since it was summarized), or short (too small to need one; just read it).

        :param limit: Maximum notes returned, default 20. Pass "" (or omit) for the default.
        :param needs_summary: true to list only notes whose summary is missing or stale (a summarizing agent's to-do list). Pass false (the normal choice) otherwise.
        :param tag: List only notes carrying this tag. Pass "" (or omit) for all notes. The result also lists the tags in use, so you can see which exist.
        :param verbose: true to include authors, timestamps and other bookkeeping. Pass false (the normal choice) to get just the text, which is shorter.
        """
        v = self.valves
        try:
            cap = min(_to_int(limit, 20, "limit"), v.MAX_LIST_RESULTS)
            tag_filter = None if is_unset(tag) else normalize_tag(tag)
            total, rows = await asyncio.to_thread(list_notes_db, v.NOTES_DB_PATH, cap, _to_bool(needs_summary), tag_filter, v.MIN_SUMMARY_CHARS)
            in_use = await asyncio.to_thread(list_tags_db, v.NOTES_DB_PATH)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        tag_line = ", ".join(f"{n} ({c})" for n, c in in_use[:TAGS_SHOWN]) if in_use else ""
        if not rows:
            empty = "No note needs a summary right now." if _to_bool(needs_summary) else "No notes yet - use agent_notes_create to start one."
            out_empty: Dict[str, Any] = {"success": True, "count": 0, "total_notes": 0, "table": empty}
            if tag_line:
                out_empty["tags_in_use"] = tag_line
            return out_empty
        if _to_bool(verbose):
            lines = ["| Note | Entries | Last updated (UTC) | Started by | Summary | Tags | Comment |", "|---|---|---|---|---|---|---|"]
            for r in rows:
                lines.append(
                    f"| {_cell(r['note_name'], NOTE_NAME_MAX)} | {r['entries']} | {r['updated_at'][:16].replace('T', ' ')} "
                    f"| {_cell(r['author_name'] or 'unknown', AUTHOR_NAME_MAX)} | {r['summary_state']} | {_cell(r['tags'] or '')} | {_cell(r['note_comment'])} |"
                )
        else:
            lines = ["| Note | Entries | Summary | Tags | Comment |", "|---|---|---|---|---|"]
            for r in rows:
                lines.append(
                    f"| {_cell(r['note_name'], NOTE_NAME_MAX)} | {r['entries']} | {r['summary_state']} | {_cell(r['tags'] or '')} | {_cell(r['note_comment'])} |"
                )
        out: Dict[str, Any] = {"success": True, "count": len(rows), "total_notes": total, "table": "\n".join(lines)}
        if tag_line:
            out["tags_in_use"] = tag_line
        if total > len(rows):
            out["note"] = f"Showing {len(rows)} of {total} notes; pass a larger limit (max {v.MAX_LIST_RESULTS}) for more."
        return out

    async def agent_notes_read(self, name: str, limit: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
        """
        Read a note: its comment and its entries in chronological order, each with its entry
        number. If the note has more entries than fit, you get the most recent ones and the
        result says so.

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        :param limit: How many of the most recent entries to return. Pass "" (or omit) for the maximum allowed.
        :param verbose: true to include authors, timestamps and other bookkeeping. Pass false (the normal choice) to get just the text, which is shorter.
        """
        v = self.valves
        try:
            cap = min(_to_int(limit, v.MAX_READ_ENTRIES, "limit"), v.MAX_READ_ENTRIES)
            result = await asyncio.to_thread(read_note_db, v.NOTES_DB_PATH, validate_name(name), cap)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        full = _to_bool(verbose)
        entries = []
        for r in result["entries"]:
            item = {"entry_no": r["entry_no"], "text": r["note_text"]}  # the number stays: edit_entry/delete_entry need it
            if full:
                item = {"entry_no": r["entry_no"], "author": r["author_name"] or "unknown", "created_at": r["created_at"], "text": r["note_text"]}
                if r["edited_at"]:
                    item["edited_at"] = r["edited_at"]
            entries.append(item)
        out: Dict[str, Any] = {
            "success": True,
            "note_name": result["note"]["note_name"],
            "note_comment": result["note"]["note_comment"],
            "tags": result["tags"],
            "total_entries": result["total"],
            "entries": entries,
        }
        if full:
            out["started_by"] = result["started_by"] or "unknown"
        if result["summary"]:
            sm = result["summary"]
            out["summary"] = {"text": sm["summary_text"], "stale": bool(sm["stale"])}
            if full:
                out["summary"].update({"last_summarized": sm["last_summarized"], "summarized_by": sm["author_name"] or "unknown"})
        if result["total"] > len(entries):
            out["note"] = (
                f"Showing the {len(entries)} most recent of {result['total']} entries. "
                f"Pass a larger limit (max {v.MAX_READ_ENTRIES}) to see more."
            )
        return out

    async def agent_notes_create(
        self, name: str, text: str, comment: Optional[str] = None, author_name: Optional[str] = None,
        created_at: Optional[str] = None, continues: bool = False, __model__: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Start a brand-new note with its first entry. Names are unique (ignoring case): if a note
        with this name already exists, nothing is created - use agent_notes_append to add to it instead.

        LIMITS (a call over any of them is refused, nothing is saved): note name 60 characters,
        comment 500, each entry's text 500. Keep entries short and self-contained. For longer
        text that must be kept word for word, pass continues=true (see below).

        :param name: A short, descriptive name (at most 60 characters), e.g. "character-bios" or "Chapter 3 plot points". This is how you and other agents refer to the note from now on.
        :param text: The first entry's text, at most 500 characters (or longer with continues=true).
        :param comment: Optional one-line description of what the note is for, at most 500 characters. Pass "" (or omit) for none.
        :param author_name: Who is writing this, at most 60 characters. Pass "" (or omit) to be recorded under your model's name.
        :param created_at: Only when copying in an older note: when it was originally written, as ISO 8601 (2026-03-01T14:30:00Z, or just 2026-03-01) or a Unix epoch number. Pass "" (or omit) for the current time, which is right for anything new.
        :param continues: Pass true when text is longer than 500 characters and must be kept verbatim: the tool saves it, unchanged, as several consecutive entries, each but the last ending \"[continues at entry #N]\" (the reply gives their numbers). Pass false (the normal choice) otherwise.
        """
        try:
            result = await asyncio.to_thread(
                create_note_db,
                self.valves.NOTES_DB_PATH,
                validate_name(name),
                entry_parts(text, continues, self.valves.MAX_CONTINUATION_ENTRIES),
                validate_comment(comment),
                resolve_author(author_name, __model__),
                validate_timestamp(created_at),
            )
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result, "note": "Created. Use agent_notes_append to add more entries."}

    async def agent_notes_append(
        self, name: str, text: str, author_name: Optional[str] = None, created_at: Optional[str] = None, continues: bool = False, __model__: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Add a new entry to the end of an existing note. This never changes earlier entries - use
        it for anything new you want to record. To fix an earlier entry use agent_notes_edit_entry; to
        start a new note use agent_notes_create.

        LIMIT: each entry's text is at most 500 characters (a longer one is refused, nothing is
        saved) - unless you pass continues=true, which saves a longer text word for word as
        several consecutive entries (the reply gives their numbers; the tool limits how many).

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        :param text: The new entry's text, at most 500 characters (or longer with continues=true).
        :param author_name: Who is writing this, at most 60 characters. Pass "" (or omit) to be recorded under your model's name.
        :param created_at: Only when copying in an older note: when this entry was originally written, as ISO 8601 (2026-03-01T14:30:00Z, or just 2026-03-01) or a Unix epoch number. Pass "" (or omit) for the current time, which is right for anything new. Entries read back in time order, so a backdated entry appears before newer ones even though its number is higher.
        :param continues: Pass true when text is longer than 500 characters and must be kept verbatim: the tool saves it, unchanged, as several consecutive entries, each but the last ending \"[continues at entry #N]\" (the reply gives their numbers). Pass false (the normal choice) otherwise.
        """
        try:
            result = await asyncio.to_thread(
                append_entry_db,
                self.valves.NOTES_DB_PATH,
                validate_name(name),
                entry_parts(text, continues, self.valves.MAX_CONTINUATION_ENTRIES),
                resolve_author(author_name, __model__),
                validate_timestamp(created_at),
            )
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_edit_entry(self, name: str, entry_no: str, text: str) -> Dict[str, Any]:
        """
        Replace the text of one existing entry, keeping its number and its place in the note.

        LIMIT: the new text is at most 500 characters (a longer one is refused, the entry is left
        as it was). If the rewrite won't fit, shorten it and put the rest in agent_notes_append (with continues=true if it must stay verbatim).

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        :param entry_no: The entry's number, as shown by agent_notes_read (e.g. 3 or "#3").
        :param text: The entry's new text, at most 500 characters.
        """
        try:
            number = _to_int(entry_no, None, "entry_no")
            if number is None:
                raise NoteError("entry_no is required - agent_notes_read shows each entry's number.")
            result = await asyncio.to_thread(edit_entry_db, self.valves.NOTES_DB_PATH, validate_name(name), number, validate_text(text))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_delete_entry(self, name: str, entry_no: str) -> Dict[str, Any]:
        """
        Permanently delete one entry from a note. The other entries keep their numbers (numbers
        are never reused).

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        :param entry_no: The entry's number, as shown by agent_notes_read (e.g. 3 or "#3").
        """
        try:
            number = _to_int(entry_no, None, "entry_no")
            if number is None:
                raise NoteError("entry_no is required - agent_notes_read shows each entry's number.")
            result = await asyncio.to_thread(delete_entry_db, self.valves.NOTES_DB_PATH, validate_name(name), number)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_update(
        self, name: str, new_name: Optional[str] = None, comment: Optional[str] = None, clear_comment: bool = False,
        author_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Rename a note, change its comment, and/or change who is recorded as its author. Entries are
        untouched (each keeps its own author). Give at least one of new_name, comment,
        clear_comment, or author_name.

        LIMITS: note name 60 characters, comment 500, author name 60 (a call over any of them is
        refused, nothing is changed).

        :param name: The note's current name, as shown by agent_notes_list (case doesn't matter).
        :param new_name: The new name (at most 60 characters; must not match another note's name). Pass "" (or omit) to keep the current name.
        :param comment: The new comment (at most 500 characters). Pass "" (or omit) to keep the current comment.
        :param clear_comment: true to erase the comment entirely. Pass false (the normal choice) otherwise.
        :param author_name: Record this as the note's author instead (at most 60 characters). Pass "" (or omit) to leave the author as it is.
        """
        try:
            wants_name = not is_unset(new_name)
            wants_comment = not is_unset(comment)
            wants_author = not is_unset(author_name)
            clearing = _to_bool(clear_comment)
            if not (wants_name or wants_comment or clearing or wants_author):
                raise NoteError("Nothing to update - give new_name, comment, clear_comment=true, or author_name.")
            if clearing and wants_comment:
                raise NoteError("Give either comment or clear_comment=true, not both.")
            result = await asyncio.to_thread(
                update_note_db,
                self.valves.NOTES_DB_PATH,
                validate_name(name),
                validate_name(new_name) if wants_name else None,
                "" if clearing else (validate_comment(comment) if wants_comment else None),
                validate_author(author_name) if wants_author else None,
            )
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_delete(self, name: str) -> Dict[str, Any]:
        """
        Permanently delete a note and every entry in it. This cannot be undone and asks for no
        confirmation, so be sure - agent_notes_read first if unsure.

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        """
        try:
            result = await asyncio.to_thread(delete_note_db, self.valves.NOTES_DB_PATH, validate_name(name))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_add_tags(self, name: str, tags: str, create: bool = False) -> Dict[str, Any]:
        """
        Tag a note so related notes can be found together (agent_notes_list with a tag lists them). Tags
        are short lowercase labels shared by every note, e.g. "comfy", "character-bios", "todo".
        REUSE existing tags: agent_notes_list shows the tags in use, and agent_notes_list_tags shows them with counts.

        A tag that matches an existing one (ignoring case, hyphens and plurals) is simply reused.
        If you give a NEW tag that resembles existing ones, nothing is saved and the reply lists the
        similar tags: retry with one of those. Only when none fit, pass create=true to make it.

        LIMITS: a tag is at most 40 characters, a note has at most 10 tags. Punctuation is cleaned up for you ("Habits & Interests" becomes habits-and-interests).

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        :param tags: One tag, or several separated by commas, e.g. "comfy, sdxl".
        :param create: true to create a tag even though similar ones exist (only after checking they don't fit). Pass false (the normal choice) otherwise.
        """
        try:
            result = await asyncio.to_thread(add_tags_db, self.valves.NOTES_DB_PATH, validate_name(name), split_tags(tags), _to_bool(create))
            adjusted = tag_adjustments(tags)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        if adjusted:
            result["adjusted"] = adjusted  # the model asked for one spelling and got another: say so
        return {"success": True, **result}

    async def agent_notes_remove_tag(self, name: str, tag: str) -> Dict[str, Any]:
        """
        Take one tag off a note. The tag disappears entirely once no note carries it.

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        :param tag: The tag to remove, as shown by agent_notes_read or agent_notes_list.
        """
        try:
            result = await asyncio.to_thread(remove_tag_db, self.valves.NOTES_DB_PATH, validate_name(name), normalize_tag(tag))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_list_tags(self, query: Optional[str] = None) -> Dict[str, Any]:
        """
        List the tags in use, with how many notes carry each, most used first. Check this before
        inventing a new tag for agent_notes_add_tags.

        :param query: Only tags containing or resembling this text. Pass "" (or omit) for all tags.
        """
        try:
            rows = await asyncio.to_thread(list_tags_db, self.valves.NOTES_DB_PATH, None if is_unset(query) else str(query))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, "count": len(rows), "tags": [{"tag": n, "notes": c} for n, c in rows]}

    async def agent_notes_rename_tag(self, tag: str, new_tag: str) -> Dict[str, Any]:
        """
        Rename a tag on every note that carries it. If new_tag already exists the two are merged:
        use this to fold a near-duplicate (say "sdxl-poses") into the tag it should have been ("poses").

        :param tag: The existing tag, as shown by agent_notes_list_tags.
        :param new_tag: Its new name, at most 40 characters.
        """
        try:
            result = await asyncio.to_thread(rename_tag_db, self.valves.NOTES_DB_PATH, normalize_tag(tag), normalize_tag(new_tag))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_read_tagged(self, tags: str, match: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
        """
        Pull in the notes carrying certain tags, with their contents, in one call: for each note its
        tags, its summary if it has one, and its most recent entries. Use this to gather everything
        about a subject (say tags "identity, speech" for a character) instead of reading notes one by one.

        The reply has a size limit, so it holds whole entries only, newest first, spread across the
        notes; it says which notes or entries were left out. For the rest, use agent_notes_read on
        that note.

        :param tags: One tag, or several separated by commas, e.g. "identity, speech". Variant spellings are matched (see agent_notes_list_tags).
        :param match: "any" (the default) for notes carrying at least one of the tags, best match first; "all" for only notes carrying every tag. Pass "" (or omit) for any.
        :param verbose: true to include each entry's number, author and timestamp and each note's tags. Pass false (the normal choice) to get just names, summaries and entry text, which is much shorter.
        """
        v = self.valves
        try:
            wanted = split_tags(tags)
            mode = "any" if is_unset(match) else str(match).strip().lower()
            if mode not in ("any", "all"):
                raise NoteError(f'match must be "any" or "all", got {match!r}.')
            notes, found, missing = await asyncio.to_thread(tagged_notes_db, v.NOTES_DB_PATH, wanted, mode == "all")
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        full = _to_bool(verbose)
        shaped = [_shape_tagged(n, full) for n in notes]
        packed, left_out = pack_tagged(shaped, max(500, v.MAX_TAGGED_CHARS) - 300)  # 300: the reply's own keys and remarks
        out: Dict[str, Any] = {"success": True, "notes": packed}
        if full:
            out.update({"tags": found, "match": mode, "notes_found": len(notes)})
        remarks = []
        if not notes:
            remarks.append(f"No note carries {'all of ' if mode == 'all' else ''}those tags.")
        if missing:
            remarks.append("Not in use as tags: " + "; ".join(missing) + ".")
        if left_out:
            out["notes_left_out"] = left_out
            remarks.append(f"{len(left_out)} more matching note(s) didn't fit the size limit and were left out; use agent_notes_read on them by name.")
        if any(n.get("entries_not_shown") for n in packed):
            remarks.append("Some notes show only their newest entries (see entries_not_shown); agent_notes_read has the rest.")
        if remarks:
            out["note"] = " ".join(remarks)
        return out

    async def agent_notes_search(self, query: str, limit: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
        """
        Search every note for some text (case-insensitive substring). Matches note names and
        comments, and the text of individual entries; returns both lists so you can find which
        note holds something without reading them all. Matching entries are listed newest first.

        :param query: The text to look for.
        :param limit: Maximum matches in each list, default 10. Pass "" (or omit) for the default.
        :param verbose: true to include authors, timestamps and other bookkeeping. Pass false (the normal choice) to get just the text, which is shorter.
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
                {"note_name": r["note_name"], "entry_no": r["entry_no"], "text": r["note_text"]}
                if not _to_bool(verbose)
                else {"note_name": r["note_name"], "entry_no": r["entry_no"], "author": r["author_name"] or "unknown", "text": r["note_text"]}
                for r in entries
            ],
        }

    async def agent_notes_update_summary(
        self, name: str, summary: str, author_name: Optional[str] = None, __model__: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Write (or replace) a note's summary: a brief, searchable description of what the note
        contains, so other agents can find it with agent_notes_search_summary without reading every note. Each
        note has at most one summary; calling this again replaces it. Read the whole note first
        (agent_notes_read) so the summary reflects all of it.

        Write for retrieval: name the key people, places, decisions and topics in plain words, since
        agent_notes_search_summary matches on the words used. LIMIT: 500 characters (a longer one is refused,
        nothing is saved).

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        :param summary: The summary, at most 500 characters, one paragraph.
        :param author_name: Who wrote the summary, at most 60 characters. Pass "" (or omit) to be recorded under your model's name.
        """
        try:
            result = await asyncio.to_thread(
                update_summary_db,
                self.valves.NOTES_DB_PATH,
                validate_name(name),
                validate_summary(summary),
                resolve_author(author_name, __model__),
            )
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}

    async def agent_notes_search_summary(self, query: str, limit: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
        """
        Search the notes' summaries (and note names) for the words in a query, best match first.
        Use this to find which note holds what you need, then agent_notes_read for the details. Any
        word of the query can match; notes matching more of the words rank higher, so a few
        distinctive keywords work better than a full sentence. A result marked stale means the
        note changed after it was summarized, so confirm with agent_notes_read. Short notes (under about 1000 characters) have no summary; find those with agent_notes_search or a tag.

        :param query: A few keywords, e.g. "dragon lair map".
        :param limit: Maximum results, default 10. Pass "" (or omit) for the default.
        :param verbose: true to include authors, timestamps and other bookkeeping. Pass false (the normal choice) to get just the text, which is shorter.
        """
        v = self.valves
        try:
            if not query or not str(query).strip():
                raise NoteError("query is required.")
            cap = min(_to_int(limit, 10, "limit"), v.MAX_SEARCH_RESULTS)
            rows, words = await asyncio.to_thread(search_summaries_db, v.NOTES_DB_PATH, str(query).strip(), cap)
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {
            "success": True,
            "count": len(rows),
            "results": [
                {"note_name": r["note_name"], "summary": r["summary_text"], "stale": bool(r["stale"])}
                if not _to_bool(verbose)
                else {
                    "note_name": r["note_name"],
                    "summary": r["summary_text"],
                    "matched_words": f"{r['score']} of {words}",
                    "last_summarized": r["last_summarized"],
                    "summarized_by": r["author_name"] or "unknown",
                    "stale": bool(r["stale"]),
                }
                for r in rows
            ],
        }

    async def agent_notes_delete_summary(self, name: str) -> Dict[str, Any]:
        """
        Permanently remove a note's summary. The note and its entries are untouched. Normally you
        don't need this - agent_notes_update_summary replaces a summary - but it clears one that is wrong.

        :param name: The note's name, as shown by agent_notes_list (case doesn't matter).
        """
        try:
            result = await asyncio.to_thread(delete_summary_db, self.valves.NOTES_DB_PATH, validate_name(name))
        except (ValueError, sqlite3.Error) as e:
            return _error(e)
        return {"success": True, **result}
