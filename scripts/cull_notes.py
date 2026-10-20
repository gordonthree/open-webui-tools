#!/usr/bin/env python3
"""
Purge everything but the last N days of entries from one or more agent notes (e.g. autonomy-log).

Host-side housekeeping, not an agent tool: the agents can't call it. It opens the notes database
directly, so run it on the host that has the file (see "Host access to the databases (ACLs)" in the README).

What it does to each named note:
  - deletes entries whose created_at is older than --days days ago (an entry's edit time doesn't count);
  - keeps a whole linked chain (continues=true entries) if any piece of it is inside the window;
  - never deletes the note itself, even if every entry is old (the note stays, empty);
  - leaves entry numbers alone (they are never reused, so a cull leaves gaps) and the note's updated_at;
  - with --clear-summary, also deletes the note's summary (the Nightly Summarizer will rewrite it).
Before changing anything it copies the database to tmp/ (--no-backup skips that). --dry-run only reports.

Usage:
    python3 scripts/cull_notes.py --note autonomy-log --days 7 --dry-run
    python3 scripts/cull_notes.py --note autonomy-log --days 7 --clear-summary
    python3 scripts/cull_notes.py --note autonomy-log,scratch --days 3 --db /path/to/agent_notes.sqlite3 --yes
"""
import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "/home/gordon/docker/open-webui/data/agent_notes.sqlite3"
BACKUP_DIR = Path(__file__).resolve().parent.parent / "tmp"


def cutoff_iso(days: float, now: "datetime.datetime | None" = None) -> str:
    """The ISO text (UTC, as the tool stores created_at) of `days` days ago."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(days=days)).isoformat()


# Entries to cull: older than the cutoff, not pinned, and not part of a chain that has a piece inside the window.
_OLD_SQL = (
    "FROM note_data d WHERE d.note_pk = :pk AND d.created_at < :cutoff AND NOT EXISTS ("
    " SELECT 1 FROM note_data c WHERE c.note_pk = d.note_pk AND c.chain_id IS NOT NULL AND c.chain_id = d.chain_id AND c.created_at >= :cutoff)"
)


def old_sql(conn: sqlite3.Connection) -> str:
    """_OLD_SQL, leaving out entries pinned in the web interface (a database from before pinning has no such column)."""
    has_pinned = "pinned" in [r[1] for r in conn.execute("PRAGMA table_info(note_data)")]
    return _OLD_SQL + (" AND d.pinned IS NULL" if has_pinned else "")


def cull_note(conn: sqlite3.Connection, name: str, cutoff: str, clear_summary: bool = False, dry_run: bool = False) -> dict:
    """Cull one note inside the caller's transaction. Returns counts; raises LookupError if the note doesn't exist."""
    row = conn.execute("SELECT note_pk, note_name FROM note_id WHERE lower(note_name) = lower(?)", (name.strip(),)).fetchone()
    if row is None:
        raise LookupError(f"No note named {name!r}.")
    pk = row[0]
    total = conn.execute("SELECT COUNT(*) FROM note_data WHERE note_pk = ?", (pk,)).fetchone()[0]
    old = conn.execute(f"SELECT COUNT(*) {old_sql(conn)}", {"pk": pk, "cutoff": cutoff}).fetchone()[0]
    cleared = False
    if not dry_run:
        conn.execute(f"DELETE FROM note_data WHERE rowid IN (SELECT d.rowid {old_sql(conn)})", {"pk": pk, "cutoff": cutoff})
        if clear_summary and old:
            cleared = conn.execute("DELETE FROM note_summary WHERE note_pk = ?", (pk,)).rowcount > 0
    return {"note": row[1], "total": total, "removed": old, "kept": total - old, "summary_cleared": cleared}


def backup(db: str) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / f"agent_notes_before_cull_{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}.sqlite3"
    src = sqlite3.connect(db)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Purge all but the last N days of entries from agent notes.")
    ap.add_argument("--note", required=True, help="note name, or several separated by commas")
    ap.add_argument("--days", type=float, required=True, help="keep entries created within this many days")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"path to agent_notes.sqlite3 (default: {DEFAULT_DB})")
    ap.add_argument("--clear-summary", action="store_true", help="also delete the summary of each note that lost entries")
    ap.add_argument("--dry-run", action="store_true", help="report what would be removed; change nothing")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--no-backup", action="store_true", help="don't copy the database to tmp/ first")
    args = ap.parse_args(argv)
    if args.days <= 0:
        ap.error("--days must be greater than 0")
    if not Path(args.db).exists():
        print(f"No database at {args.db}", file=sys.stderr)
        return 2
    names = [n for n in (x.strip() for x in args.note.split(",")) if n]
    cutoff = cutoff_iso(args.days)
    print(f"Keeping entries created at or after {cutoff} ({args.days:g} days ago).")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        preview = [cull_note(conn, n, cutoff, dry_run=True) for n in names]
    except LookupError as e:
        print(e, file=sys.stderr)
        return 2
    for r in preview:
        print(f"  {r['note']}: {r['total']} entries, {r['removed']} to remove, {r['kept']} kept")
    if args.dry_run or not any(r["removed"] for r in preview):
        print("Dry run: nothing changed." if args.dry_run else "Nothing to remove.")
        return 0
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1
    if not args.no_backup:
        print(f"Backed up to {backup(args.db)}")
    try:
        with conn:  # one transaction for all the notes
            results = [cull_note(conn, n, cutoff, args.clear_summary) for n in names]
    finally:
        conn.close()
    for r in results:
        print(f"  {r['note']}: removed {r['removed']}, kept {r['kept']}" + (", summary cleared" if r["summary_cleared"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
