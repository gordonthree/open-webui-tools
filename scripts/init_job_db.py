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
runs on a bare host Python install with nothing else set up.

Usage:
    python scripts/init_job_db.py /srv/owui/comfy_outputs/comfy_jobs.sqlite3
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# Kept byte-identical to JOB_DB_SCHEMA/PROMPTS_FTS_SCHEMA in src/comfy_sdxl_direct.py (the
# canonical copy), src/comfy_sdxl_graph.py, and src/comfy_sdxl_retrieve.py - see
# src/SQLITE_JOB_DB_HANDOFF.md. Update all four together if the schema ever changes.
JOB_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_uuid TEXT PRIMARY KEY,
    comfy_prompt_id TEXT,
    tool TEXT NOT NULL,
    server TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    execution_start_ts INTEGER,
    execution_end_ts INTEGER,
    duration_s REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_comfy_prompt_id ON jobs(comfy_prompt_id);
CREATE INDEX IF NOT EXISTS idx_jobs_server_status ON jobs(server, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);

CREATE TABLE IF NOT EXISTS inputs (
    job_uuid TEXT PRIMARY KEY REFERENCES jobs(job_uuid),
    raw_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outputs (
    job_uuid TEXT PRIMARY KEY REFERENCES jobs(job_uuid),
    raw_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL REFERENCES jobs(job_uuid),
    stage TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_job_uuid ON results(job_uuid);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL REFERENCES jobs(job_uuid),
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    raw_json TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_job_uuid ON errors(job_uuid);

CREATE TABLE IF NOT EXISTS prompts (
    job_uuid TEXT PRIMARY KEY REFERENCES jobs(job_uuid),
    positive_prompt TEXT,
    negative_prompt TEXT
);

CREATE TABLE IF NOT EXISTS node_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL REFERENCES jobs(job_uuid),
    node_id TEXT NOT NULL,
    class_type TEXT NOT NULL,
    param_name TEXT NOT NULL,
    value_text TEXT,
    value_num REAL
);
CREATE INDEX IF NOT EXISTS idx_node_params_job_uuid ON node_params(job_uuid);
CREATE INDEX IF NOT EXISTS idx_node_params_lookup ON node_params(class_type, param_name, value_text);
"""
PROMPTS_FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(job_uuid UNINDEXED, positive_prompt, negative_prompt);"


def init_db(db_path: Path) -> None:
    already_existed = db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(JOB_DB_SCHEMA)
        fts_ok = True
        try:
            conn.execute(PROMPTS_FTS_SCHEMA)
        except sqlite3.OperationalError:
            fts_ok = False  # this SQLite build lacks FTS5; the tools fall back to LIKE search
        conn.commit()
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
        # executescript's CREATE TABLE IF NOT EXISTS is safe to re-run, but say so explicitly
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
