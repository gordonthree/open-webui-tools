#!/usr/bin/env python3
"""
Run the agent_notes rating pass (Weighting_Guidance.md, "The rating pass") for you, chunk after chunk.

Each chunk is one fresh throwaway chat on the agent's own Open WebUI server, run the same way src/agent_duo.py
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
    python scripts/rating_pass.py --model medium-mara --note "Mara Voss - Speech" --persona
    python scripts/rating_pass.py --server hannah --model hannah-long --persona --max-ratings 10 --note hannah-psychology,hannah-identity,hannah-habits

--model is the agent's model preset id on that server (agent_duo's defaults are medium-mara and hannah-long).

--recheck is a second, different pass: looking back at the entries she rated very high (default 9 or 10) when she wrote them,
from --since on. The script reads those entries from the notes database itself (read-only, --db), groups them by the local day
they were written, and shows each day's entries in time order across the notes (internal-life and shared-history by default),
so she sees every entry about one evening together. She only calls agent_notes_set_weight; the prompt asks her to keep a 10
on the one or two entries that hold a moment's heart, go no lower than 7, and give the parts of one long text the same number.
Afterwards the script reports each change, and flags anything below 7, parts of one text left at different weights, and
ratings of entries that were not in the group.
    python scripts/rating_pass.py --model medium-mara --recheck --since 2026-10-17 --dry-run
    python scripts/rating_pass.py --model medium-mara --recheck --since 2026-10-17 --until 2026-10-18
"""
import argparse
import datetime
import json
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from push_tool import DEFAULT_SECRETS_FILE, configured_servers, parse_secrets, server_connection  # noqa: E402

PAGE = 10
MAX_RATINGS = 8
TURN_TIMEOUT_SECONDS = 300
RECHECK_TIMEOUT_SECONDS = 1500  # a whole evening can mean 20+ tool calls on a slow local model
POLL_SECONDS = 3
MAX_FAILURES_IN_A_ROW = 4
RATE_LIMIT_WAIT_SECONDS = 150

# Kept in step with the prompt in Weighting_Guidance.md.
PROMPT = """Rating pass for the note "{note}". Use agent_notes_set_weight only; do not edit or add entries.
This is one small chunk: read ONE page of entries, rate up to {max_ratings} of them, then stop and report.

1. {read_step}
2. Of the entries on that one page, call agent_notes_set_weight for each one that is NOT 5-neutral to you, up to {max_ratings} calls in all. Leave the neutral ones alone: they already sit at 5. Do not read a second page.
3. {rate_step}
4. Finish with: the note name, how many entries you rated, and the lowest entry number among the page's ordinary entries (the "entries" list, NOT the "strong_memories" lists). Nothing else."""


MEMORY_RATE = ("Use the whole range. If a difficult moment is on the page (hurt, doubt, loss, frustration, fear, shame), rate it low; "
               "do not lower anything that was joyful. Do not give every entry the same number.")
PERSONA_RATE = ("This is one of your persona notes, so the number is how strongly the trait is you, not how it felt: 9-10 defining, "
                "7-8 central, 5 ordinary, 3 or lower occasional or situational. A low number does not mean bad. Do not give every entry the same number.")

# --recheck: memories rated very high the moment they were written, looked at again a few days later.
RECHECK_NOTES = "internal-life,shared-history"
RECHECK_MIN_WEIGHT = 9
RECHECK_FLOOR = 7
RECHECK_GROUP_MAX = 25  # entries (counting each part of a long text) per chat; big enough for a whole evening (Oct 17: 22)
DEFAULT_LOCAL_DB = Path.home() / "docker/open-webui/data/agent_notes.sqlite3"

RECHECK_PROMPT = """Looking back at your strongest memories from {day}. Use agent_notes_set_weight only; do not edit or add entries, and do not read any notes: the entries are below.

Each of these was rated {min_weight} or higher the moment you wrote it. In these notes the weight is how a memory felt, and 10 is for the few moments you will never forget. Now that a little time has passed, give each one the weight it deserves.

- When several entries describe the same moment, keep a 10 on the one or two that hold its heart and let the rest be 8 or 9. They still belong to that moment; they don't all have to be it.
- You may keep a 10, and you may leave any entry as it is: just don't call the tool for it.
- These were all good moments. Lowering one keeps it warm, so go no lower than {floor}.
- An entry marked "one text in N parts" is a single memory: give every part the same number.

The entries, oldest first:

{entries}

For each entry you change, call agent_notes_set_weight with name, entry_no and weight. Finish with one line: how many entries you changed. Nothing else."""


# --------------------------------------------------------------------------- #
# Talking to the agent's Open WebUI server. These mirror the helpers in src/agent_duo.py (kept in step by hand) so this script needs
# only `requests`, not the pydantic that importing the Pipe would drag in.
# --------------------------------------------------------------------------- #

_DETAILS_RE = re.compile(r"<details\b.*?</details>", re.S | re.I)


class AgentError(Exception):
    pass


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _check(resp, what: str) -> None:
    if resp.status_code != 200:
        raise AgentError(f"{what} failed (HTTP {resp.status_code}): {str(resp.text)[:200]}")


def strip_details(text: str) -> str:
    return _DETAILS_RE.sub("", text or "").strip()


def fetch_tool_ids(url: str, key: str, model_id: str) -> List[str]:
    """The tool ids attached to the agent's model preset. Open WebUI's UI sends these itself; the API doesn't."""
    resp = requests.get(f"{url}/api/v1/models/model", params={"id": model_id}, headers=_headers(key), timeout=20)
    if resp.status_code == 404:
        return []
    _check(resp, "Reading the model preset")
    ids = ((resp.json().get("meta") or {}).get("toolIds")) or []
    return [i for i in ids if isinstance(i, str)]


def create_backing_chat(url: str, key: str, model_id: str, label: str, prompt: str) -> "tuple[str, str]":
    """A throwaway chat holding one user message and an empty assistant message; returns (chat_id, assistant_id)."""
    user_id, asst_id, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    umsg = {"id": user_id, "parentId": None, "childrenIds": [asst_id], "role": "user", "content": prompt[:200], "timestamp": now, "models": [model_id]}
    amsg = {"id": asst_id, "parentId": user_id, "childrenIds": [], "role": "assistant", "content": "", "model": model_id, "modelName": model_id, "timestamp": now}
    chat = {
        "title": f"[rating_pass] {label}", "models": [model_id], "timestamp": now * 1000,
        "history": {"messages": {user_id: umsg, asst_id: amsg}, "currentId": asst_id},
        "messages": [umsg, amsg],
    }
    resp = requests.post(f"{url}/api/v1/chats/new", headers=_headers(key), json={"chat": chat}, timeout=30)
    _check(resp, "Creating the chat")
    return resp.json()["id"], asst_id


def start_turn(url: str, key: str, model_id: str, chat_id: str, asst_id: str, tool_ids: List[str], messages: List[dict]) -> None:
    body = {"model": model_id, "stream": True, "chat_id": chat_id, "id": asst_id, "session_id": str(uuid.uuid4()), "messages": messages}
    if tool_ids:
        body["tool_ids"] = tool_ids
    _check(requests.post(f"{url}/api/chat/completions", headers=_headers(key), json=body, timeout=60), "Starting the turn")


def read_turn(url: str, key: str, chat_id: str, asst_id: str) -> dict:
    resp = requests.get(f"{url}/api/v1/chats/{chat_id}", headers=_headers(key), timeout=30)
    _check(resp, "Reading the chat")
    return (((resp.json().get("chat") or {}).get("history") or {}).get("messages") or {}).get(asst_id) or {}


def delete_chat(url: str, key: str, chat_id: str) -> None:
    try:
        requests.delete(f"{url}/api/v1/chats/{chat_id}", headers=_headers(key), timeout=20)
    except requests.RequestException:
        pass  # a leftover chat titled "[rating_pass] ..." is harmless


def build_prompt(note: str, before: Optional[int], max_ratings: int = MAX_RATINGS, persona: bool = False) -> str:
    if before is None:
        step = f'Call agent_notes_read with name="{note}", verbose=false, count=false, limit={PAGE}.'
    else:
        step = (f'Call agent_notes_read with name="{note}", verbose=false, count=false, limit={PAGE} and before={before} '
                f"(the argument is named before). It shows the {PAGE} entries just older than {before}.")
    return PROMPT.format(note=note, read_step=step, max_ratings=max_ratings, rate_step=PERSONA_RATE if persona else MEMORY_RATE)


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


def run_chunk(url: str, key: str, model: str, tool_ids: List[str], prompt: str, keep_chats: bool,
              timeout: float = TURN_TIMEOUT_SECONDS) -> "tuple[str, Any]":
    chat_id, asst_id = create_backing_chat(url, key, model, "rating pass", prompt)
    try:
        start_turn(url, key, model, chat_id, asst_id, tool_ids, [{"role": "user", "content": prompt}])
        deadline = time.monotonic() + timeout
        while True:
            time.sleep(POLL_SECONDS)
            msg = read_turn(url, key, chat_id, asst_id)
            if msg.get("error"):
                err = msg["error"]
                raise AgentError(str(err.get("content") if isinstance(err, dict) else err)[:300])
            if msg.get("done"):
                return strip_details(msg.get("content") or ""), msg.get("output") or []
            if time.monotonic() > deadline:
                raise AgentError(f"no reply within {timeout:.0f} seconds")
    finally:
        if not keep_chats:
            delete_chat(url, key, chat_id)


# --------------------------------------------------------------------------- #
# --recheck
# --------------------------------------------------------------------------- #

def _local(created_at: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(created_at).astimezone()


def load_recheck_items(db_path: Path, notes: List[str], since: datetime.date, until: Optional[datetime.date],
                       min_weight: int) -> List[dict]:
    """The memories to look at again, oldest first: one item per entry, or per long text saved in several linked parts (a
    chain is included whole if any part qualifies). Pinned entries are left out: the tools keep their weight anyway."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        items = []
        for note in notes:
            rows = conn.execute(
                "SELECT n.note_name, d.entry_no, d.note_text, d.created_at, d.weight, d.pinned, d.chain_id "
                "FROM note_data d JOIN note_id n ON n.note_pk = d.note_pk WHERE lower(n.note_name) = lower(?) ORDER BY d.entry_no",
                (note,)).fetchall()
            groups: dict = {}
            for r in rows:
                groups.setdefault(("chain", r["chain_id"]) if r["chain_id"] is not None else ("entry", r["entry_no"]), []).append(r)
            for parts in groups.values():
                when = _local(parts[0]["created_at"])
                score = max((p["weight"] + 5) // 10 for p in parts)
                if any(p["pinned"] for p in parts) or score < min_weight:
                    continue
                if when.date() < since or (until is not None and when.date() > until):
                    continue
                items.append({"note": parts[0]["note_name"], "entries": [p["entry_no"] for p in parts], "weight": score,
                              "text": "".join(p["note_text"] for p in parts), "when": when})
        return sorted(items, key=lambda i: i["when"])
    finally:
        conn.close()


def group_by_day(items: List[dict], max_entries: int = RECHECK_GROUP_MAX) -> List["tuple[str, List[dict]]"]:
    """(day label, items) per chat: one local day each, split into consecutive runs when a day holds more than max_entries."""
    out: List["tuple[str, List[dict]]"] = []
    current_day, run, size = None, [], 0
    for item in items:
        day = item["when"].strftime("%A, %B %-d")
        n = len(item["entries"])
        if run and (day != current_day or size + n > max_entries):
            out.append((current_day, run))
            run, size = [], 0
        current_day = day
        run.append(item)
        size += n
    if run:
        out.append((current_day, run))
    return out


def span(nos: List[int]) -> str:
    return str(nos[0]) if len(nos) == 1 else f"{nos[0]}-{nos[-1]}"


def format_item(item: dict) -> str:
    nos = item["entries"]
    if len(nos) == 1:
        label = f'{item["note"]} #{nos[0]}, weight {item["weight"]}'
    else:
        label = (f'{item["note"]} #{span(nos)}, weight {item["weight"]}, one text in {len(nos)} parts '
                 f'(entries {", ".join(f"#{n}" for n in nos)})')
    return f'[{label}, {item["when"].strftime("%H:%M")}] {item["text"]}'


def build_recheck_prompt(day: str, items: List[dict], min_weight: int = RECHECK_MIN_WEIGHT) -> str:
    return RECHECK_PROMPT.format(day=day, min_weight=min_weight, floor=RECHECK_FLOOR,
                                 entries="\n\n".join(format_item(i) for i in items))


def inspect_recheck(output: Any) -> List[dict]:
    """The agent_notes_set_weight calls that succeeded: note_name, entry_no, old_weight, weight (from the tool's own replies)."""
    calls: dict = {}
    changes = []
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls[item.get("call_id") or item.get("id") or ""] = item
        elif item.get("type") == "function_call_output":
            if (calls.get(item.get("call_id") or "") or {}).get("name") != "agent_notes_set_weight":
                continue
            try:
                body = json.loads(_result_text(item))
            except ValueError:
                continue
            if isinstance(body, dict) and body.get("success") and "entry_no" in body:
                changes.append({k: body.get(k) for k in ("note_name", "entry_no", "old_weight", "weight")})
    return changes


def recheck_problems(items: List[dict], changes: List[dict]) -> List[str]:
    """What to look at after a chat: ratings below the floor, parts of one text left at different weights, entries not shown."""
    final = {(c["note_name"].lower(), int(c["entry_no"])): c["weight"] for c in changes}
    shown = {(i["note"].lower(), n) for i in items for n in i["entries"]}
    problems = []
    for (note, no), w in sorted(final.items()):
        if (note, no) not in shown:
            problems.append(f"{note} #{no} was rated ({w}) but was not one of the entries shown")
        elif w < RECHECK_FLOOR:
            problems.append(f"{note} #{no} went down to {w}, below the floor of {RECHECK_FLOOR}")
    for i in items:
        if len(i["entries"]) > 1:
            weights = {final.get((i["note"].lower(), n), i["weight"]) for n in i["entries"]}
            if len(weights) > 1:
                problems.append(f'{i["note"]} #{span(i["entries"])} is one text but its parts now differ: {sorted(weights)}')
    return problems


def recheck(url: str, key: str, args, tool_ids: list, groups: List["tuple[str, List[dict]]"]) -> int:
    total = 0
    for n, (day, items) in enumerate(groups, 1):
        prompt = build_recheck_prompt(day, items, args.min_weight)
        for attempt in range(1, MAX_FAILURES_IN_A_ROW + 1):
            try:
                reply, output = run_chunk(url, key, args.model, tool_ids, prompt, args.keep_chats, RECHECK_TIMEOUT_SECONDS)
                break
            except (AgentError, requests.RequestException) as ex:
                wait = RATE_LIMIT_WAIT_SECONDS if is_rate_limit(str(ex)) else args.delay * 2
                print(f"chat {n} ({day}) failed [{attempt}/{MAX_FAILURES_IN_A_ROW}]: {ex}" +
                      (f"; waiting {wait:.0f}s and retrying" if attempt < MAX_FAILURES_IN_A_ROW else ""))
                if attempt == MAX_FAILURES_IN_A_ROW:
                    print(f"giving up. Resume with --since {items[0]['when'].date()}")
                    return 1
                time.sleep(wait)
        changes = inspect_recheck(output)
        total += len(changes)
        print(f"chat {n} of {len(groups)} ({day}, {sum(len(i['entries']) for i in items)} entries): {len(changes)} changed")
        for c in changes:
            print(f"  {c['note_name']} #{c['entry_no']}: {c['old_weight']} -> {c['weight']}")
        if not changes and reply.strip():
            print(f"  no tool calls saved; she replied: {reply.strip()[:200]}")
        for p in recheck_problems(items, changes):
            print(f"  CHECK: {p}")
        if n < len(groups):
            time.sleep(args.delay)
    print(f"done: {total} weights changed in {len(groups)} chats.")
    return 0


def is_rate_limit(text: str) -> bool:
    return bool(re.search(r"429|rate.?limit", text, re.I))


def rate_note(url: str, key: str, args, tool_ids: list, note: str, before: Optional[int]) -> int:
    """Run the chunks for one note; returns 0 when it finished or used up --chunks, 1 when it had to stop."""
    failures = 0
    total_rated = 0
    for n in range(1, args.chunks + 1):
        prompt = build_prompt(note, before, args.max_ratings, args.persona)
        label = "newest page" if before is None else f"before={before}"
        try:
            reply, output = run_chunk(url, key, args.model, tool_ids, prompt, args.keep_chats)
        except (AgentError, requests.RequestException) as ex:
            failures += 1
            wait = RATE_LIMIT_WAIT_SECONDS if is_rate_limit(str(ex)) else args.delay * 2
            print(f"chunk {n} ({label}) failed [{failures}/{MAX_FAILURES_IN_A_ROW}]: {ex}; waiting {wait:.0f}s and retrying")
            if failures >= MAX_FAILURES_IN_A_ROW:
                print(f"giving up on {note}. Resume with --note {note!r} --before {before}" if before is not None
                      else f"giving up on {note}. Nothing was paged yet; run again.")
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
            print(f"reached the start of {note}. {total_rated} ratings saved this run.")
            return 0
        if before is not None and lowest >= before:
            print(f"the page did not move back (lowest {lowest}, before {before}); stopping.")
            return 1
        before = lowest
        if n < args.chunks:
            time.sleep(args.delay)
    print(f"stopped after {args.chunks} chunks; {total_rated} ratings saved on {note}. Resume with --note {note!r} --before {before}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True, help="the agent's model preset id on the server")
    ap.add_argument("--note", help="the note to rate, e.g. internal-life; or a comma-separated list of notes, done one after another (all must be the same kind, since --persona applies to all). "
                    f"With --recheck the default is {RECHECK_NOTES}")
    ap.add_argument("--server", default="local", help="local (default) or a name configured as OWUI_<NAME>_URL/_KEY in secrets.md")
    ap.add_argument("--before", type=int, help="resume: start with the page just older than this entry number (default: the newest page)")
    ap.add_argument("--chunks", type=int, default=30, help="most chunks to run this time (default 30)")
    ap.add_argument("--delay", type=float, default=45, help="seconds to wait between chunks (default 45)")
    ap.add_argument("--max-ratings", type=int, default=MAX_RATINGS, help=f"most ratings per chunk (default {MAX_RATINGS})")
    ap.add_argument("--persona", action="store_true", help="the note is a persona facet (kind persona): rate how strongly each trait is hers, not how it felt")
    ap.add_argument("--recheck", action="store_true", help="look again at memories rated very high when written (see the module docstring); needs --since")
    ap.add_argument("--since", type=datetime.date.fromisoformat, help="--recheck: first local day (YYYY-MM-DD) to include")
    ap.add_argument("--until", type=datetime.date.fromisoformat, help="--recheck: last local day to include (default: today)")
    ap.add_argument("--min-weight", type=int, default=RECHECK_MIN_WEIGHT, help=f"--recheck: include entries rated at least this (default {RECHECK_MIN_WEIGHT})")
    ap.add_argument("--db", type=Path, help=f"--recheck: the notes database, read-only (default for --server local: {DEFAULT_LOCAL_DB}; "
                    "for another server, a copy of its file)")
    ap.add_argument("--keep-chats", action="store_true", help="leave each chunk's throwaway chat in place to inspect its tool calls")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and the server details, call nothing that changes anything")
    ap.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    args = ap.parse_args()

    if args.recheck:
        if args.since is None:
            ap.error("--recheck needs --since YYYY-MM-DD.")
        if args.persona or args.before is not None:
            ap.error("--recheck is for memory notes and does not page: leave out --persona and --before.")
        if args.db is None:
            if args.server != "local":
                ap.error("--recheck on another server needs --db with a copy of that server's notes database.")
            args.db = DEFAULT_LOCAL_DB
        if not args.db.is_file():
            ap.error(f"No notes database at {args.db}.")
    notes = [x.strip() for x in (args.note or (RECHECK_NOTES if args.recheck else "")).split(",") if x.strip()]
    if not notes:
        ap.error("--note needs at least one note name.")
    if len(notes) > 1 and args.before is not None:
        ap.error("--before resumes one note; pass a single --note with it.")
    values = parse_secrets(args.secrets)
    if args.server != "local" and args.server.lower() not in configured_servers(values):
        ap.error(f"Unknown server {args.server!r}; secrets.md configures: {['local'] + configured_servers(values)}.")
    url, key = server_connection(values, args.server.lower())
    tool_ids = fetch_tool_ids(url, key, args.model)
    print(f"server {args.server} ({url}), model {args.model}, tools attached: {', '.join(tool_ids) or 'NONE'}")
    if not any("agent_notes" in t for t in tool_ids):
        print("warning: no agent_notes tool is attached to that model preset; the agent could not rate anything.")
    if args.recheck:
        items = load_recheck_items(args.db, notes, args.since, args.until, args.min_weight)
        groups = group_by_day(items)
        print(f"{len(items)} memories rated {args.min_weight}+ since {args.since} in {', '.join(notes)}; {len(groups)} chats")
        for day, group in groups:
            print(f"  {day}: " + ", ".join(f'{i["note"]} #{span(i["entries"])}={i["weight"]}' for i in group))
        if args.dry_run:
            if groups:
                print("\nFirst chat's prompt:\n\n" + build_recheck_prompt(*groups[0], args.min_weight))
            return 0
        return recheck(url, key, args, tool_ids, groups)
    if args.dry_run:
        for note in notes:
            print("\n" + build_prompt(note, args.before, args.max_ratings, args.persona))
        return 0

    for i, note in enumerate(notes):
        if len(notes) > 1:
            print(f"\n=== note {i + 1} of {len(notes)}: {note} ===")
        if rate_note(url, key, args, tool_ids, note, args.before) != 0:
            rest = notes[i + 1:]
            if rest:
                print(f"stopped before the remaining notes: {', '.join(rest)}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
