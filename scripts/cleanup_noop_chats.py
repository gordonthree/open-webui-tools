#!/usr/bin/env python3
"""
Delete finished "NOOP" autonomy-tick chats from Open WebUI (OWUI).

An autonomy tick (see autonomy/charter.md) that decides to do nothing ends with the final message
`NOOP`. Those chats are clutter, so this script finds them in the folder the automation files its
runs into and deletes them. A chat is deleted only if ALL of these hold:
  - it is in the named folder (default "Companion ticks"; not its subfolders);
  - it is at least --min-age-minutes old (default 60), so a tick still running is never touched;
  - its current (last) message is a finished assistant message whose text is exactly NOOP.
If Gordon replied in the chat, or the tick did anything else, the last message differs and it is kept.

With --also-empty, finished ticks whose final assistant message is empty are deleted too: ticks that
were stopped by hand (or died mid-run) leave a finished chat with no text. Opt-in, because an empty
ending can also be a real failure worth reading first - run tick_report.py to see which is which.

Connection details come from secrets.md (OWUI_BASE_URL, OWUI_API_KEY), the same file push_tool.py uses.

Usage:
    python scripts/cleanup_noop_chats.py --dry-run
    python scripts/cleanup_noop_chats.py --yes                    # for cron
    python scripts/cleanup_noop_chats.py --dry-run --also-empty   # preview stopped ticks as well
    python scripts/cleanup_noop_chats.py --folder "Ticks" --min-age-minutes 120 --yes
    python scripts/cleanup_noop_chats.py --yes --log ~/logs/cleanup_noop_chats.log

Suggested crontab entry (every 6 hours, on the hour; adjust the repo path and log directory):
    0 */6 * * * /usr/bin/python3 /home/gordon/dev/open-webui-tools/scripts/cleanup_noop_chats.py --yes --log /home/gordon/logs/cleanup_noop_chats.log

--log appends each run's output to the file, one line per message, each prefixed with a UTC
timestamp. Cron mails or drops stdout, so the log is the record of what was deleted.
"""
import argparse
import datetime
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from push_tool import DEFAULT_SECRETS_FILE, parse_secrets, require  # noqa: E402

DEFAULT_FOLDER = "Companion ticks"


def find_folder_id(session, base_url, name):
    resp = session.get(f"{base_url}/api/v1/folders/", timeout=30)
    resp.raise_for_status()
    matches = [f for f in resp.json() if (f.get("name") or "").casefold() == name.casefold()]
    if not matches:
        sys.exit(f'No folder named "{name}" in OWUI (create it, or pass --folder).')
    if len(matches) > 1:
        sys.exit(f'More than one folder named "{name}"; rename one so the match is unambiguous.')
    return matches[0]["id"]


def list_folder_chats(session, base_url, folder_id):
    resp = session.get(f"{base_url}/api/v1/chats/folder/{folder_id}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_chat(session, base_url, chat_id):
    resp = session.get(f"{base_url}/api/v1/chats/{chat_id}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def is_noop(chat):
    """True if the chat's current message is a finished assistant message that is exactly NOOP."""
    history = (chat.get("chat") or {}).get("history") or {}
    messages = history.get("messages") or {}
    current = messages.get(history.get("currentId"))
    if not current or current.get("role") != "assistant" or not current.get("done"):
        return False
    return (current.get("content") or "").strip().casefold() == "noop"


def is_empty_ending(chat):
    """True if the chat's current message is a finished assistant message with no text at all."""
    history = (chat.get("chat") or {}).get("history") or {}
    messages = history.get("messages") or {}
    current = messages.get(history.get("currentId"))
    if not current or current.get("role") != "assistant" or not current.get("done"):
        return False
    return not (current.get("content") or "").strip()


def find_noop_chats(session, base_url, folder_id, min_age_seconds, now, include_empty=False):
    """Return the full chat records of the deletable chats in the folder: NOOP ones, plus (with
    include_empty) finished ones that ended with no text."""
    found = []
    for summary in list_folder_chats(session, base_url, folder_id):
        if now - (summary.get("updated_at") or now) < min_age_seconds:
            continue  # too new (or no timestamp: err on the side of keeping it)
        chat = get_chat(session, base_url, summary["id"])
        if chat.get("folder_id") != folder_id:
            continue  # belt and braces: never delete something that isn't verifiably in the folder
        if is_noop(chat) or (include_empty and is_empty_ending(chat)):
            found.append(chat)
    return found


def make_reporter(log_path):
    """Return say(msg): prints to stdout and, if log_path is given, appends a UTC-timestamped line."""
    log_file = None
    if log_path:
        log_path = Path(log_path).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path

    def say(msg):
        print(msg)
        if log_file:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with log_file.open("a", encoding="utf-8") as fh:
                for line in str(msg).splitlines() or [""]:
                    fh.write(f"{stamp} {line}\n")
    return say


def main(argv=None, session=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--folder", default=DEFAULT_FOLDER, help=f'folder name (default "{DEFAULT_FOLDER}")')
    ap.add_argument("--min-age-minutes", type=float, default=60)
    ap.add_argument("--also-empty", action="store_true",
                    help="also delete finished ticks that ended with an empty message (stopped by hand)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation (for cron)")
    ap.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    ap.add_argument("--log", type=Path, help="append this run's output to FILE, each line timestamped (UTC)")
    args = ap.parse_args(argv)
    say = make_reporter(args.log)

    secrets = parse_secrets(args.secrets)
    base_url = require(secrets, "OWUI_BASE_URL").rstrip("/")
    if session is None:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {require(secrets, 'OWUI_API_KEY')}"

    folder_id = find_folder_id(session, base_url, args.folder)
    doomed = find_noop_chats(session, base_url, folder_id, args.min_age_minutes * 60, time.time(),
                             include_empty=args.also_empty)
    what = "NOOP/empty" if args.also_empty else "NOOP"
    say(f'{len(doomed)} {what} chat(s) in "{args.folder}"' + (" (dry run)." if args.dry_run else "."))
    for chat in doomed:
        kind = "NOOP" if is_noop(chat) else "empty"
        say(f"  - {chat['id'][:8]}  {kind:5}  {(chat.get('title') or '')[:60]}")
    if args.dry_run or not doomed:
        return 0
    if not args.yes:
        if not sys.stdin.isatty() or input(f"Delete {len(doomed)} chat(s)? [y/N] ").strip().lower() != "y":
            say("Aborted.")
            return 1

    failed = 0
    for chat in doomed:
        resp = session.delete(f"{base_url}/api/v1/chats/{chat['id']}", timeout=30)
        if resp.status_code != 200:
            failed += 1
            say(f"  failed to delete {chat['id'][:8]}: {resp.status_code} {resp.text[:200]}")
    say(f"Deleted {len(doomed) - failed}/{len(doomed)}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
