#!/usr/bin/env python3
"""
Sweep a ComfyUI server's own /history for completed jobs and add whatever the job database
doesn't already know about - run this from the host, no OWUI involved at all.

For each completed entry in the server's history:
  - Already in the database (matched by comfy_prompt_id + server)? Skipped - nothing to do.
  - Not tracked, but matches an existing job row that's missing its comfy_prompt_id (the same
    "unlinked job" case retrieve_image's opportunistic reconciliation handles for one file at a
    time)? Backfilled - same graph-match + closest-in-time tie-break as that reconciliation logic.
  - Not tracked at all (e.g. rendered directly through ComfyUI's own UI, or from before job
    logging existed)? A brand-new job row is created from just what /history provides: tool is
    recorded as "external" since there's no way to know it was ever one of these OWUI tools (or
    even Open WebUI) at all.

ComfyUI's own history retention (cleared on restart, bounded size) caps how far back this can
reach - it can only import what the server still has, same limitation retrieve_image's
history=<count> mode has.

Usage:
    python scripts/import_server_history.py http://192.168.10.11:8188 /srv/owui/comfy_outputs/comfy_jobs.sqlite3
    python scripts/import_server_history.py <server> <db_path> --max-items 2000 --dry-run
"""
import argparse
import datetime
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from job_db_common import (
    execution_timing,
    extract_node_params,
    extract_prompts,
    classify_job_mode,
    history_prompt_graph,
    job_db_connect,
    now_iso,
)

_RECONCILE_AMBIGUITY_WINDOW_S = 5.0  # same window comfy_sdxl_retrieve.py's reconciliation uses


def fetch_history(server: str, max_items: int, timeout: int) -> Dict[str, Any]:
    r = requests.get(f"{server}/history", params={"max_items": max_items}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def is_completed(entry: Dict[str, Any]) -> bool:
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        return False
    images = [
        img
        for out in (entry.get("outputs") or {}).values()
        for img in (out.get("images") or [])
        if img.get("type", "output") == "output"
    ]
    return bool(images)


def already_tracked(conn: sqlite3.Connection, server: str, comfy_prompt_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE server = ? AND comfy_prompt_id = ? LIMIT 1", (server, comfy_prompt_id)
    ).fetchone()
    return row is not None


def find_unlinked_candidates(conn: sqlite3.Connection, server: str, graph: Dict[str, Any]) -> List[sqlite3.Row]:
    """Same premise as comfy_sdxl_retrieve.py's find_unlinked_job_candidates: jobs on `server`
    with comfy_prompt_id IS NULL - that column is only ever set once a job is known to have
    reached the server, so this is exactly the unlinked set - whose submitted graph matches
    exactly."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT j.job_uuid, j.created_at, o.raw_json FROM jobs j JOIN outputs o ON o.job_uuid = j.job_uuid "
        "WHERE j.server = ? AND j.comfy_prompt_id IS NULL",
        (server,),
    ).fetchall()
    matches = []
    for row in rows:
        try:
            if json.loads(row["raw_json"]) == graph:
                matches.append(row)
        except ValueError:
            continue
    return matches


def resolve_match(candidates: List[sqlite3.Row], execution_start_ts_ms: Optional[int]) -> Dict[str, Any]:
    """Same tie-break as comfy_sdxl_retrieve.py's resolve_reconciliation_match: prefer the
    candidate closest in time to the render; report (not hide) genuine ambiguity."""
    if len(candidates) == 1:
        return {"job_uuid": candidates[0]["job_uuid"], "ambiguous": False}
    if execution_start_ts_ms is None:
        ordered = sorted(candidates, key=lambda r: r["created_at"])
        return {"job_uuid": ordered[0]["job_uuid"], "ambiguous": True}
    exec_dt = datetime.datetime.fromtimestamp(execution_start_ts_ms / 1000, tz=datetime.timezone.utc)

    def _delta_s(row: sqlite3.Row) -> float:
        return abs((datetime.datetime.fromisoformat(row["created_at"]) - exec_dt).total_seconds())

    ordered = sorted(candidates, key=_delta_s)
    ambiguous = abs(_delta_s(ordered[0]) - _delta_s(ordered[1])) < _RECONCILE_AMBIGUITY_WINDOW_S
    return {"job_uuid": ordered[0]["job_uuid"], "ambiguous": ambiguous}


def backfill_job(conn: sqlite3.Connection, job_uuid: str, comfy_prompt_id: str, entry: Dict[str, Any]) -> None:
    """Same write shape as comfy_sdxl_retrieve.py's record_job_reconciled: backfill
    comfy_prompt_id, then status/timing/results - as one transaction here since this script owns
    its connection for the whole run rather than opening a short-lived one per call."""
    start_ts, end_ts, duration = execution_timing(entry)
    now = now_iso()
    conn.execute(
        "UPDATE jobs SET comfy_prompt_id = ?, status = 'completed', execution_start_ts = ?, "
        "execution_end_ts = ?, duration_s = ? WHERE job_uuid = ? AND comfy_prompt_id IS NULL",
        (comfy_prompt_id, start_ts, end_ts, duration, job_uuid),
    )
    conn.execute(
        "INSERT INTO results (job_uuid, stage, raw_json, recorded_at) VALUES (?, 'history', ?, ?)",
        (job_uuid, json.dumps(entry, default=str), now),
    )


def import_new_job(conn: sqlite3.Connection, server: str, comfy_prompt_id: str, entry: Dict[str, Any]) -> str:
    """A job /history knows about that no existing row matches at all - recorded from scratch.
    Returns the new job_uuid."""
    graph = history_prompt_graph(entry) or {}
    positive_prompt, negative_prompt = extract_prompts(graph)
    start_ts, end_ts, duration = execution_timing(entry)
    job_uuid = str(uuid.uuid4())
    now = now_iso()

    conn.execute(
        "INSERT INTO jobs (job_uuid, comfy_prompt_id, tool, server, status, created_at, "
        "execution_start_ts, execution_end_ts, duration_s) VALUES (?, ?, 'external', ?, 'completed', ?, ?, ?, ?)",
        (job_uuid, comfy_prompt_id, server, now, start_ts, end_ts, duration),
    )
    conn.execute(
        "INSERT INTO inputs (job_uuid, raw_json, recorded_at) VALUES (?, ?, ?)",
        (
            job_uuid,
            json.dumps(
                {
                    "source": "scripts/import_server_history.py",
                    "note": "Discovered directly in the ComfyUI server's /history; no original tool request is available.",
                }
            ),
            now,
        ),
    )
    conn.execute(
        "INSERT INTO outputs (job_uuid, raw_json, recorded_at) VALUES (?, ?, ?)",
        (job_uuid, json.dumps(graph, default=str), now),
    )
    conn.execute(
        "INSERT INTO prompts (job_uuid, positive_prompt, negative_prompt) VALUES (?, ?, ?)",
        (job_uuid, positive_prompt, negative_prompt),
    )
    try:
        conn.execute(
            "INSERT INTO prompts_fts (job_uuid, positive_prompt, negative_prompt) VALUES (?, ?, ?)",
            (job_uuid, positive_prompt or "", negative_prompt or ""),
        )
    except sqlite3.OperationalError:
        pass  # no FTS5 in this SQLite build
    conn.executemany(
        "INSERT INTO node_params (job_uuid, node_id, class_type, param_name, value_text, value_num) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(job_uuid, *row) for row in extract_node_params(graph)],
    )
    conn.execute(
        "INSERT INTO results (job_uuid, stage, raw_json, recorded_at) VALUES (?, 'history', ?, ?)",
        (job_uuid, json.dumps(entry, default=str), now),
    )
    return job_uuid


def import_entry(conn: sqlite3.Connection, server: str, comfy_prompt_id: str, entry: Any, dry_run: bool) -> str:
    """Returns one of: 'already_tracked', 'skipped_not_completed', 'skipped_no_graph',
    'reconciled', 'reconciled_ambiguous', 'imported'."""
    if not isinstance(entry, dict):
        return "skipped_no_graph"
    if not is_completed(entry):
        return "skipped_not_completed"
    if already_tracked(conn, server, comfy_prompt_id):
        return "already_tracked"

    graph = history_prompt_graph(entry)
    if not graph:
        return "skipped_no_graph"

    candidates = find_unlinked_candidates(conn, server, graph)
    if candidates:
        start_ts, _end_ts, _duration = execution_timing(entry)
        resolution = resolve_match(candidates, start_ts)
        if not dry_run:
            backfill_job(conn, resolution["job_uuid"], comfy_prompt_id, entry)
        return "reconciled_ambiguous" if resolution["ambiguous"] else "reconciled"

    if not dry_run:
        import_new_job(conn, server, comfy_prompt_id, entry)
    return "imported"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("server", help="ComfyUI server address, e.g. http://192.168.10.11:8188")
    parser.add_argument("db_path", help="Path to the shared job database.")
    parser.add_argument("--max-items", type=int, default=500, help="How many of the server's most recent history entries to fetch (default 500). Bounded by the server's own retention regardless.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP request timeout in seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything.")
    args = parser.parse_args()

    server = args.server.rstrip("/")

    try:
        raw = fetch_history(server, args.max_items, args.timeout)
    except requests.exceptions.RequestException as e:
        sys.exit(f"Could not reach {server}: {e}")
    except ValueError as e:
        sys.exit(f"Unexpected response from {server}: {e}")

    db_existed = Path(args.db_path).exists()
    conn = job_db_connect(args.db_path)
    if not db_existed:
        print(f"{args.db_path} didn't exist yet - created it (schema only; this still applies under --dry-run).")

    if not raw:
        conn.close()
        print(f"{server} returned no history entries.")
        return

    counts: Dict[str, int] = {}
    ambiguous_notes: List[str] = []
    try:
        for comfy_prompt_id, entry in raw.items():
            outcome = import_entry(conn, server, comfy_prompt_id, entry, args.dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "reconciled_ambiguous":
                ambiguous_notes.append(comfy_prompt_id)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    label = "Would import" if args.dry_run else "Imported"
    print(f"{server}: {len(raw)} history entries scanned.")
    print(f"  {counts.get('already_tracked', 0)} already tracked (skipped)")
    print(f"  {counts.get('skipped_not_completed', 0)} not completed (skipped)")
    print(f"  {counts.get('skipped_no_graph', 0)} had no usable graph (skipped)")
    print(f"  {counts.get('reconciled', 0) + counts.get('reconciled_ambiguous', 0)} backfilled onto an existing unlinked job row")
    print(f"  {counts.get('imported', 0)} {label.lower()} as new job rows (tool='external')")
    if ambiguous_notes:
        print(
            f"  NOTE: {len(ambiguous_notes)} backfill(s) matched more than one candidate row with similar "
            "timing - picked the closest, but can't be fully certain. comfy_prompt_id(s): "
            + ", ".join(ambiguous_notes[:10])
            + (" ..." if len(ambiguous_notes) > 10 else "")
        )
    if args.dry_run:
        print("(--dry-run: nothing was actually written)")


if __name__ == "__main__":
    main()
