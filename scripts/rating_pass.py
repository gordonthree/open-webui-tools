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
"""
import argparse
import json
import re
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


def run_chunk(url: str, key: str, model: str, tool_ids: List[str], prompt: str, keep_chats: bool) -> "tuple[str, Any]":
    chat_id, asst_id = create_backing_chat(url, key, model, "rating pass", prompt)
    try:
        start_turn(url, key, model, chat_id, asst_id, tool_ids, [{"role": "user", "content": prompt}])
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        while True:
            time.sleep(POLL_SECONDS)
            msg = read_turn(url, key, chat_id, asst_id)
            if msg.get("error"):
                err = msg["error"]
                raise AgentError(str(err.get("content") if isinstance(err, dict) else err)[:300])
            if msg.get("done"):
                return strip_details(msg.get("content") or ""), msg.get("output") or []
            if time.monotonic() > deadline:
                raise AgentError(f"no reply within {TURN_TIMEOUT_SECONDS} seconds")
    finally:
        if not keep_chats:
            delete_chat(url, key, chat_id)


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
    ap.add_argument("--note", required=True, help="the note to rate, e.g. internal-life; or a comma-separated list of notes, done one after another (all must be the same kind, since --persona applies to all)")
    ap.add_argument("--server", default="local", help="local (default) or a name configured as OWUI_<NAME>_URL/_KEY in secrets.md")
    ap.add_argument("--before", type=int, help="resume: start with the page just older than this entry number (default: the newest page)")
    ap.add_argument("--chunks", type=int, default=30, help="most chunks to run this time (default 30)")
    ap.add_argument("--delay", type=float, default=45, help="seconds to wait between chunks (default 45)")
    ap.add_argument("--max-ratings", type=int, default=MAX_RATINGS, help=f"most ratings per chunk (default {MAX_RATINGS})")
    ap.add_argument("--persona", action="store_true", help="the note is a persona facet (kind persona): rate how strongly each trait is hers, not how it felt")
    ap.add_argument("--keep-chats", action="store_true", help="leave each chunk's throwaway chat in place to inspect its tool calls")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and the server details, call nothing that changes anything")
    ap.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    args = ap.parse_args()

    notes = [x.strip() for x in args.note.split(",") if x.strip()]
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
