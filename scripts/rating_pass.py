#!/usr/bin/env python3
"""
Run the agent_notes rating pass (Weighting_Guidance.md, "The rating pass") for you, chunk after chunk.

Each chunk is one fresh throwaway chat on the agent's own Open WebUI server, run the same way agent_duo.py
runs a turn (the server-side tool loop, with the tools attached to the agent's model preset): the agent
reads ONE page of a note (10 entries) and rates up to 8 of them with agent_notes_set_weight. The script then
works out where the next page starts from what the agent's read actually returned (the lowest ordinary entry
number on the page), waits, and sends the next chunk with before=<that number>, until the note is done. Every
chunk starts a new chat, so a 9B / 8k model never carries earlier chunks in its context.

It is still the agent doing the rating, as herself, on her own model and persona; the script only does the
"Repeat your last instructions, with before=NNN" part. Ratings are her judgment, so this does not change who
decides them. Entries she has already rated are simply rated again (the last rating wins).

Safe to stop (Ctrl-C) and resume: use --before with the number the last chunk printed. A chunk that fails
(rate limit, timeout) is retried after a wait; the script gives up on a note after a few failures in a row.

Usage:
    python scripts/rating_pass.py --model medium-mara --note internal-life --dry-run
    python scripts/rating_pass.py --model medium-mara --note internal-life --chunks 5
    python scripts/rating_pass.py --model medium-mara --note internal-life --before 204 --delay 60
    python scripts/rating_pass.py --server hannah --model hannah-long --note some-note

--model is the agent's model preset id on that server (agent_duo's defaults are medium-mara and hannah-long).
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from push_tool import DEFAULT_SECRETS_FILE, configured_servers, parse_secrets, server_connection  # noqa: E402
import agent_duo  # noqa: E402

PAGE = 10
MAX_RATINGS = 8
TURN_TIMEOUT_SECONDS = 300
POLL_SECONDS = 3
MAX_FAILURES_IN_A_ROW = 4
RATE_LIMIT_WAIT_SECONDS = 150

# Kept in step with the prompt in Weighting_Guidance.md.
PROMPT = """Rating pass for the note "{note}". Use agent_notes_set_weight only; do not edit or add entries.
This is one small chunk: read ONE page of entries, rate up to {max_ratings} of them, then stop and report.

1. {read_step}
2. Of the entries on that one page, call agent_notes_set_weight for each one that is NOT 5-neutral to you, up to {max_ratings} calls in all. Leave the neutral ones alone: they already sit at 5. Do not read a second page.
3. Use the whole range. If a difficult moment is on the page (hurt, doubt, loss, frustration, fear, shame), rate it low; do not lower anything that was joyful. Do not give every entry the same number.
4. Finish with: the note name, how many entries you rated, and the lowest entry number among the page's ordinary entries (the "entries" list, NOT the "strong_memories" lists). Nothing else."""


def build_prompt(note: str, before: Optional[int], max_ratings: int = MAX_RATINGS) -> str:
    if before is None:
        step = f'Call agent_notes_read with name="{note}", verbose=false, count=false, limit={PAGE}.'
    else:
        step = (f'Call agent_notes_read with name="{note}", verbose=false, count=false, limit={PAGE} and before={before} '
                f"(the argument is named before). It shows the {PAGE} entries just older than {before}.")
    return PROMPT.format(note=note, read_step=step, max_ratings=max_ratings)


def _result_text(item: dict) -> str:
    parts = item.get("output")
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)) if isinstance(parts, list) else str(parts or "")


def inspect_output(output: Any) -> dict:
    """What a finished chunk really did, from the message's tool-call `output` (not from what the agent says she did):
    the successful agent_notes_set_weight calls, and the lowest ordinary entry number on the page her read returned."""
    calls: dict = {}
    rated: List[int] = []
    lowest: Optional[int] = None
    read_ok = False
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls[item.get("call_id") or item.get("id") or ""] = item
        elif item.get("type") == "function_call_output":
            call = calls.get(item.get("call_id") or "") or {}
            try:
                body = json.loads(_result_text(item))
            except ValueError:
                continue
            if not isinstance(body, dict) or not body.get("success"):
                continue
            if call.get("name") == "agent_notes_set_weight" and "entry_no" in body:
                rated.append(int(body["entry_no"]))
            elif call.get("name") == "agent_notes_read":
                read_ok = True
                numbers = [e["entry_no"] for e in body.get("entries") or [] if isinstance(e, dict) and "entry_no" in e]
                if numbers:
                    lowest = min(numbers)
    return {"rated": rated, "lowest": lowest, "read_ok": read_ok}


def reply_number(reply: str) -> Optional[int]:
    """The last whole number in the agent's final line, the fallback when her read's result isn't in the output."""
    lines = [ln for ln in reply.strip().splitlines() if ln.strip()]
    found = re.findall(r"\d+", lines[-1]) if lines else []
    return int(found[-1]) if found else None


def run_chunk(url: str, key: str, model: str, tool_ids: List[str], prompt: str, keep_chats: bool) -> "tuple[str, Any]":
    chat_id, asst_id = agent_duo.create_backing_chat(url, key, model, "rating pass", prompt)
    try:
        agent_duo.start_turn(url, key, model, chat_id, asst_id, tool_ids, [{"role": "user", "content": prompt}])
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        while True:
            time.sleep(POLL_SECONDS)
            msg = agent_duo.read_turn(url, key, chat_id, asst_id)
            if msg.get("error"):
                err = msg["error"]
                raise agent_duo.AgentError(str(err.get("content") if isinstance(err, dict) else err)[:300])
            if msg.get("done"):
                return agent_duo.strip_details(msg.get("content") or ""), msg.get("output") or []
            if time.monotonic() > deadline:
                raise agent_duo.AgentError(f"no reply within {TURN_TIMEOUT_SECONDS} seconds")
    finally:
        if not keep_chats:
            agent_duo.delete_chat(url, key, chat_id)


def is_rate_limit(text: str) -> bool:
    return bool(re.search(r"429|rate.?limit", text, re.I))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True, help="the agent's model preset id on the server")
    ap.add_argument("--note", required=True, help="the note to rate, e.g. internal-life")
    ap.add_argument("--server", default="local", help="local (default) or a name configured as OWUI_<NAME>_URL/_KEY in secrets.md")
    ap.add_argument("--before", type=int, help="resume: start with the page just older than this entry number (default: the newest page)")
    ap.add_argument("--chunks", type=int, default=30, help="most chunks to run this time (default 30)")
    ap.add_argument("--delay", type=float, default=45, help="seconds to wait between chunks (default 45)")
    ap.add_argument("--max-ratings", type=int, default=MAX_RATINGS, help=f"most ratings per chunk (default {MAX_RATINGS})")
    ap.add_argument("--keep-chats", action="store_true", help="leave each chunk's throwaway chat in place to inspect its tool calls")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and the server details, call nothing that changes anything")
    ap.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    args = ap.parse_args()

    values = parse_secrets(args.secrets)
    if args.server != "local" and args.server.lower() not in configured_servers(values):
        ap.error(f"Unknown server {args.server!r}; secrets.md configures: {['local'] + configured_servers(values)}.")
    url, key = server_connection(values, args.server.lower())
    tool_ids = agent_duo.fetch_tool_ids(url, key, args.model)
    print(f"server {args.server} ({url}), model {args.model}, tools attached: {', '.join(tool_ids) or 'NONE'}")
    if not any("agent_notes" in t for t in tool_ids):
        print("warning: no agent_notes tool is attached to that model preset; the agent could not rate anything.")
    if args.dry_run:
        print("\n" + build_prompt(args.note, args.before, args.max_ratings))
        return 0

    before = args.before
    failures = 0
    total_rated = 0
    for n in range(1, args.chunks + 1):
        prompt = build_prompt(args.note, before, args.max_ratings)
        label = "newest page" if before is None else f"before={before}"
        try:
            reply, output = run_chunk(url, key, args.model, tool_ids, prompt, args.keep_chats)
        except (agent_duo.AgentError, requests.RequestException) as ex:
            failures += 1
            wait = RATE_LIMIT_WAIT_SECONDS if is_rate_limit(str(ex)) else args.delay * 2
            print(f"chunk {n} ({label}) failed [{failures}/{MAX_FAILURES_IN_A_ROW}]: {ex}; waiting {wait:.0f}s and retrying")
            if failures >= MAX_FAILURES_IN_A_ROW:
                print(f"giving up. Resume with --before {before}" if before is not None else "giving up. Nothing was paged yet; run again.")
                return 1
            time.sleep(wait)
            continue
        failures = 0
        done = inspect_output(output)
        lowest = done["lowest"] if done["lowest"] is not None else (reply_number(reply) if done["read_ok"] else None)
        total_rated += len(done["rated"])
        print(f"chunk {n} ({label}): rated {len(done['rated'])} entries {sorted(done['rated'])}, page reached down to {lowest}")
        if not done["read_ok"]:
            print("  the agent's read did not succeed in this chunk (nothing was read); stopping so you can look at it. Try --keep-chats.")
            return 1
        if not done["rated"]:
            print("  no ratings were saved in this chunk" + (" - the agent may have described rating without calling the tool" if reply.strip() else ""))
        if lowest is None or lowest <= 1:
            print(f"reached the start of {args.note}. {total_rated} ratings saved this run.")
            return 0
        if before is not None and lowest >= before:
            print(f"the page did not move back (lowest {lowest}, before {before}); stopping.")
            return 1
        before = lowest
        if n < args.chunks:
            time.sleep(args.delay)
    print(f"stopped after {args.chunks} chunks; {total_rated} ratings saved this run. Resume with --before {before}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
