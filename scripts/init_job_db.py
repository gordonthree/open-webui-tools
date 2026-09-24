#!/usr/bin/env python3
"""
Create (or verify) the shared job-database schema at a given path - no ComfyUI, OWUI, or Docker
required to run this.

The three OWUI tools (comfy_sdxl_direct.py, comfy_sdxl_graph.py, comfy_sdxl_retrieve.py) all
create this schema lazily and automatically the first time they actually write a job record, so
running this script is never REQUIRED for the tools to work. It exists for two reasons instead:

  1. JOB_DB_PATH is normally reached through a Docker bind mount. This lets you verify the
     mount/host-directory permissions are correct BEFORE the first real render - run it directly
     against the host-side path of that mount. No docker exec needed: a bind-mounted sqlite file
     is just a regular file on the host.
  2. It creates the full schema, including the FTS5 prompts_fts table, immediately - useful if
     you want to poke around the (empty) database with a host-side tool like the sqlite3 CLI or
     DB Browser for SQLite before any job has ever actually been logged.

Uses only the Python standard library - no OWUI/ComfyUI/pydantic/requests dependencies, so it
runs on a bare host Python install with nothing else set up (job_db_common.py, imported below, is
a local sibling file, not a third-party package).

Usage:
    python scripts/init_job_db.py /srv/owui/comfy_outputs/comfy_jobs.sqlite3
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from job_db_common import job_db_connect


def init_db(db_path: Path) -> None:
    already_existed = db_path.exists()
    conn = job_db_connect(str(db_path))
    try:
        fts_ok = True
        try:
            conn.execute("SELECT 1 FROM prompts_fts LIMIT 0;")
        except sqlite3.OperationalError:
            fts_ok = False
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
            )
        ]
    finally:
        conn.close()

    print(f"{'Verified schema on existing' if already_existed else 'Created'} database at {db_path}")
    print(f"Tables: {', '.join(tables)}")
    if not fts_ok:
        print(
            "NOTE: this SQLite build lacks the FTS5 extension - prompts_fts wasn't created; "
            "search_jobs still works, just falling back to a plain LIKE search on prompt text."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db_path", type=Path, help="Path to create/verify the job database at.")
    args = parser.parse_args()

    if args.db_path.exists() and args.db_path.stat().st_size > 0:
        # job_db_connect's CREATE TABLE IF NOT EXISTS is safe to re-run, but say so explicitly
        # rather than silently no-op'ing on a file the caller might not expect to already exist.
        print(
            f"{args.db_path} already exists ({args.db_path.stat().st_size} bytes) - "
            "will verify/extend its schema, not touch any existing data."
        )

    try:
        init_db(args.db_path)
    except OSError as e:
        sys.exit(f"Could not create database at {args.db_path}: {e}")


if __name__ == "__main__":
    main()
