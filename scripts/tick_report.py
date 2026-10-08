#!/usr/bin/env python3
"""
Summarize the autonomy-tick chats in Open WebUI (OWUI): what each tick ended with, how many tool calls
it took, and which ones look wrong. Read-only - it never changes or deletes anything.

Reads every chat in the tick folder (default "Companion ticks") through the API and reports:
  - outcome counts: NOOP / message (an ACT or CONTACT write-up) / SILENT (finished with no text, e.g.
    stopped by hand or died mid-run) / running / conversation (Gordon replied in the chat);
  - tool-call totals, and notify/send_email calls per day (the charter caps them: 6 and 10);
  - a flagged list: SILENT ticks, ticks with NO-LOG (no write to the autonomy-log note), and REPEAT
    (the same tool called many times in one tick - a retry loop, e.g. a tool refusing the same call).

Connection details come from secrets.md (OWUI_BASE_URL, OWUI_API_KEY), same as push_tool.py.

Usage:
    python scripts/tick_report.py
    python scripts/tick_report.py --since 2026-10-07 --all     # every tick, not just flagged ones
    python scripts/tick_report.py --repeat-threshold 6
"""
import argparse
import collections
import datetime
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_noop_chats import (  # noqa: E402
    DEFAULT_FOLDER, find_folder_id, get_chat, is_empty_ending, is_noop, list_folder_chats,
)
from push_tool import DEFAULT_SECRETS_FILE, parse_secrets, require  # noqa: E402

LOG_NOTE = "autonomy-log"
LOG_WRITE_TOOLS = ("agent_notes_append", "agent_notes_create")
CAPPED_TOOLS = {"notify": 6, "send_email": 10}  # per day, from the charter


def message_chain(chat):
    """Messages from the first to the current one, following parent links."""
    history = (chat.get("chat") or {}).get("history") or {}
    messages = history.get("messages") or {}
    chain, seen, cur = [], set(), history.get("currentId")
    while cur and cur in messages and cur not in seen:
        seen.add(cur)
        chain.append(messages[cur])
        cur = messages[cur].get("parentId")
    chain.reverse()
    return chain


def tool_calls(chat):
    """(name, arguments-string) for every tool call the tick made, in order."""
    calls = []
    for msg in message_chain(chat):
        if msg.get("role") != "assistant":
            continue
        for item in msg.get("output") or []:
            if item.get("type") == "function_call":
                calls.append((item.get("name") or "?", str(item.get("arguments") or "")))
    return calls


def analyze(chat, repeat_threshold):
    chain = message_chain(chat)
    last = chain[-1] if chain else {}
    user_turns = sum(1 for m in chain if m.get("role") == "user")
    calls = tool_calls(chat)
    running = last.get("role") == "assistant" and not last.get("done")

    if running:
        outcome = "running"
    elif user_turns > 1:
        outcome = "conversation"
    elif is_noop(chat):
        outcome = "NOOP"
    elif is_empty_ending(chat):
        outcome = "SILENT"
    else:
        outcome = "message"

    flags = []
    if outcome == "SILENT":
        flags.append("SILENT")
    logged = any(n in LOG_WRITE_TOOLS and LOG_NOTE in a.casefold() for n, a in calls)
    if not logged and outcome not in ("running", "conversation"):
        flags.append("NO-LOG")
    for name, count in collections.Counter(n for n, _ in calls).items():
        if count >= repeat_threshold:
            flags.append(f"REPEAT:{name}x{count}")
    return {
        "id": chat["id"], "created_at": chat.get("created_at") or 0, "outcome": outcome,
        "calls": calls, "flags": flags,
    }


def when(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def render(ticks, show_all, say=print):
    say(f"{len(ticks)} tick chat(s)")
    counts = collections.Counter(t["outcome"] for t in ticks)
    say("Outcomes: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    total = sum(len(t["calls"]) for t in ticks)
    if ticks:
        say(f"Tool calls: {total} total, {total / len(ticks):.1f} per tick, "
            f"max {max(len(t['calls']) for t in ticks)}")

    per_day = collections.defaultdict(collections.Counter)
    for t in ticks:
        day = datetime.datetime.fromtimestamp(t["created_at"]).strftime("%Y-%m-%d")
        for name, _ in t["calls"]:
            if name in CAPPED_TOOLS:
                per_day[day][name] += 1
    if per_day:
        say("Capped tools per day (charter limits: " + ", ".join(f"{k} {v}" for k, v in CAPPED_TOOLS.items()) + "):")
        for day in sorted(per_day):
            parts = [f"{k} {per_day[day][k]}" + (" OVER" if per_day[day][k] > CAPPED_TOOLS[k] else "")
                     for k in CAPPED_TOOLS]
            say(f"  {day}  " + ", ".join(parts))

    rows = [t for t in ticks if show_all or t["flags"]]
    say("")
    say("All ticks:" if show_all else f"Flagged ticks ({len(rows)}):")
    for t in rows:
        say(f"  {when(t['created_at'])}  {t['id'][:8]}  {t['outcome']:12} {len(t['calls']):3} calls  "
            + " ".join(t["flags"]))


def main(argv=None, session=None, say=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--folder", default=DEFAULT_FOLDER, help=f'folder name (default "{DEFAULT_FOLDER}")')
    ap.add_argument("--since", help="only ticks created on/after this local date, YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="list every tick, not just flagged ones")
    ap.add_argument("--repeat-threshold", type=int, default=8,
                    help="flag a tick that calls one tool this many times (default 8)")
    ap.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    args = ap.parse_args(argv)

    secrets = parse_secrets(args.secrets)
    base_url = require(secrets, "OWUI_BASE_URL").rstrip("/")
    if session is None:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {require(secrets, 'OWUI_API_KEY')}"

    since = None
    if args.since:
        since = datetime.datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    folder_id = find_folder_id(session, base_url, args.folder)
    ticks = []
    for summary in list_folder_chats(session, base_url, folder_id):
        chat = get_chat(session, base_url, summary["id"])
        if since is not None and (chat.get("created_at") or 0) < since:
            continue
        ticks.append(analyze(chat, args.repeat_threshold))
    ticks.sort(key=lambda t: t["created_at"])
    render(ticks, args.all, say)
    return 0


if __name__ == "__main__":
    sys.exit(main())
