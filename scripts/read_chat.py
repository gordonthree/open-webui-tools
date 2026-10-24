#!/usr/bin/env python3
"""
Print an Open WebUI (OWUI) chat as a labelled, readable transcript, for troubleshooting an agent.

Every block says whose it is: the user's messages, the agent's thinking, the agent's tool calls (with a
shortened result) and what the agent said. The agent's thinking is printed as written; the raw
reasoning markup is replaced by a heading that names the agent and its model, so the transcript can't
be mistaken for anyone else's reasoning (for example when it is handed to Claude Code to read).
Read-only - it never changes anything.

Connection details come from secrets.md (OWUI_BASE_URL, OWUI_API_KEY), same as push_tool.py.

Usage:
    python scripts/read_chat.py 13bc4f7c-cbcd-4437-adc7-bf26ffc104db
    python scripts/read_chat.py <chat_id> --agent Mara --turns 3-7
    python scripts/read_chat.py <chat_id> --thinking-only        # only the agent's thinking, per turn
    python scripts/read_chat.py <chat_id> --result-chars 0       # tool calls without their results
    python scripts/read_chat.py <chat_id> --out tmp/chat.txt
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_noop_chats import get_chat  # noqa: E402
from push_tool import DEFAULT_SECRETS_FILE, configured_servers, parse_secrets, require, server_connection  # noqa: E402
from tick_report import message_chain  # noqa: E402

# Older chats keep everything in `content`, with thinking and tool calls as <details> blocks.
DETAILS = re.compile(r'<details type="(reasoning|tool_calls)"([^>]*)>(.*?)</details>', re.S)
SUMMARY = re.compile(r"<summary>.*?</summary>", re.S)
THINK = re.compile(r"<think>(.*?)</think>", re.S)


def text_of(value):
    """Plain text from a string, a list of {type, text} parts, or anything else (as JSON)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_of(p.get("text", p) if isinstance(p, dict) else p) for p in value)
    return json.dumps(value, ensure_ascii=False)


def shorten(text, limit):
    text = text.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()} ... [{len(text) - limit} more chars]"


def unquote(text):
    """OWUI renders thinking inside <details> as a '> ' quote; take the quoting off."""
    return "\n".join(re.sub(r"^> ?", "", line) for line in text.splitlines())


def parts_from_output(output):
    """(kind, text, extra) parts from a message's `output` list, in order."""
    parts, calls = [], {}
    for item in output:
        kind = item.get("type")
        if kind == "reasoning":
            parts.append(("thinking", text_of(item.get("content")), None))
        elif kind == "function_call":
            part = ["call", item.get("name") or "?", {"args": text_of(item.get("arguments")), "result": None}]
            calls[item.get("call_id")] = part
            parts.append(part)
        elif kind == "function_call_output":
            part = calls.get(item.get("call_id"))
            if part:
                part[2]["result"] = text_of(item.get("output"))
            else:
                parts.append(("result", text_of(item.get("output")), None))
        elif kind == "message":
            parts.append(("said", text_of(item.get("content")), None))
    return [tuple(p) for p in parts]


def parts_from_content(content):
    """(kind, text, extra) parts from an older message whose `content` holds <details>/<think> blocks."""
    parts, pos = [], 0
    content = THINK.sub(lambda m: f'<details type="reasoning">{m.group(1)}</details>', content)
    for m in DETAILS.finditer(content):
        before = content[pos:m.start()].strip()
        if before:
            parts.append(("said", before, None))
        kind, attrs, body = m.groups()
        if kind == "reasoning":
            parts.append(("thinking", unquote(SUMMARY.sub("", body)).strip(), None))
        else:
            name = re.search(r'name="([^"]*)"', attrs)
            args = re.search(r'arguments="([^"]*)"', attrs)
            result = re.search(r'result="([^"]*)"', attrs)
            parts.append(("call", name.group(1) if name else "?",
                          {"args": _unescape(args.group(1)) if args else "",
                           "result": _unescape(result.group(1)) if result else None}))
        pos = m.end()
    rest = content[pos:].strip()
    if rest:
        parts.append(("said", rest, None))
    return parts


def _unescape(text):
    return (text.replace("&quot;", '"').replace("&#x27;", "'").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&amp;", "&"))


def message_parts(msg):
    if msg.get("role") == "assistant" and msg.get("output"):
        return parts_from_output(msg["output"])
    content = text_of(msg.get("content"))
    if msg.get("role") == "assistant":
        return parts_from_content(content)
    return [("said", content, None)]


def stamp(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def parse_turns(spec, total):
    if not spec:
        return range(1, total + 1)
    lo, _, hi = spec.partition("-")
    lo = int(lo) if lo else 1
    hi = int(hi) if hi else (lo if "-" not in spec else total)
    return range(lo, min(hi, total) + 1)


def render(chat, agent=None, user="Gordon", turns=None, thinking_only=False, result_chars=300):
    chain = message_chain(chat)
    models = sorted({m.get("model") for m in chain if m.get("role") == "assistant" and m.get("model")})
    label = agent or (models[0] if models else "Agent")
    model_note = f" ({', '.join(models)})" if models else ""
    lines = [
        f'Chat {chat.get("id")} "{chat.get("title")}" - {stamp(chat.get("created_at"))}',
        f"Transcript of {label}'s chat, read from Open WebUI. Sections marked \"{label}'s thinking\" are "
        f"the output of {label}'s own model{model_note}, recorded in the chat - not Claude's reasoning.",
        "",
    ]
    for n in parse_turns(turns, len(chain)):
        msg = chain[n - 1]
        is_agent = msg.get("role") == "assistant"
        who = f"{label} ({msg.get('model')})" if is_agent and msg.get("model") else (label if is_agent else user)
        if thinking_only and not is_agent:
            continue
        lines.append(f"=== Turn {n} - {who} - {stamp(msg.get('timestamp'))} ===")
        for kind, text, extra in message_parts(msg):
            if thinking_only and kind != "thinking":
                continue
            if kind == "thinking":
                lines += [f"[{label}'s thinking]", text.strip() or "(empty)", f"[end of {label}'s thinking]"]
            elif kind == "call":
                lines.append(f"[{label} called {text}({shorten(extra['args'], 500)})]")
                if extra.get("result") is not None and result_chars > 0:
                    lines.append(f"  [result] {shorten(extra['result'], result_chars)}")
            elif kind == "result" and result_chars > 0:
                lines.append(f"  [tool result] {shorten(text, result_chars)}")
            elif kind == "said" and text.strip():
                lines += [f"[{label if is_agent else user} said]", text.strip()]
        lines.append("")
    return "\n".join(lines)


def main(argv=None, session=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chat_id")
    ap.add_argument("--agent", help="name for the agent in labels (default: the model id)")
    ap.add_argument("--user", default="Gordon", help="name for the human in labels")
    ap.add_argument("--turns", help="turn or range to print, e.g. 4, 3-7, 5-")
    ap.add_argument("--thinking-only", action="store_true", help="print only the agent's thinking")
    ap.add_argument("--result-chars", type=int, default=300, help="characters of each tool result (0 = none)")
    ap.add_argument("--out", type=Path, help="write the transcript to this file instead of stdout")
    ap.add_argument("--json-file", type=Path, help="read the chat from a JSON file shaped like the API's reply (for a chat the API key cannot open, dumped from the database)")
    ap.add_argument("--server", default="local", help="which OWUI server holds the chat: local (default) or one secrets.md configures, e.g. hannah")
    ap.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    args = ap.parse_args(argv)

    if args.json_file:
        text = render(json.loads(args.json_file.read_text(encoding="utf-8")), agent=args.agent, user=args.user,
                      turns=args.turns, thinking_only=args.thinking_only, result_chars=args.result_chars)
        if args.out:
            args.out.write_text(text + "\n", encoding="utf-8")
            print(f"Wrote {args.out}")
        else:
            print(text)
        return 0
    secrets = parse_secrets(args.secrets)
    if args.server != "local" and args.server.lower() not in configured_servers(secrets):
        ap.error(f"Unknown server {args.server!r}; secrets.md configures: {['local'] + configured_servers(secrets)}.")
    base_url, key = server_connection(secrets, args.server.lower())
    base_url = base_url.rstrip("/")
    if session is None:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {key}"
    text = render(get_chat(session, base_url, args.chat_id), agent=args.agent, user=args.user,
                  turns=args.turns, thinking_only=args.thinking_only, result_chars=args.result_chars)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
