#!/usr/bin/env python3
"""
Delete leftover "[agent_duo]" backing chats from Mara's and Hannah's Open WebUI (OWUI) servers.

Every turn of the agent_duo Pipe (src/agent_duo.py) creates a short-lived chat titled
"[agent_duo] <agent name>" on that agent's own server, to run her tool loop, and deletes it again
once the turn finishes - unless the pipe's KEEP_BACKING_CHATS valve is on (handy for inspecting
tool calls while testing a prompt change), or the delete itself failed (delete_chat() swallows
errors rather than fail a turn, so a dropped connection leaves one behind). Either way they pile up.

This is a human-run cleanup script, not a tool either agent can call - deleting a model's own chats
stays off the model-callable surface, the same line already drawn for autonomy-tick cleanup.

A chat is deleted only if BOTH hold:
  - its title starts with --prefix (default "[agent_duo]");
  - it is at least --min-age-minutes old (default 60), so a chat mid-turn is never touched.

Runs across every server secrets.md configures (local, plus any OWUI_<NAME>_URL/_KEY) by default -
a duo round touches both agents' servers, so leftovers can appear on either. --server (repeatable,
or "all") narrows it, same flag as push_tool.py.

Usage:
    python scripts/cleanup_duo_chats.py --dry-run
    python scripts/cleanup_duo_chats.py --yes                      # for cron
    python scripts/cleanup_duo_chats.py --server hannah --dry-run
    python scripts/cleanup_duo_chats.py --yes --log ~/logs/cleanup_duo_chats.log

Suggested crontab entry (hourly; adjust the repo path and log directory):
    0 * * * * /usr/bin/python3 /home/gordon/dev/open-webui-tools/scripts/cleanup_duo_chats.py --yes --log /home/gordon/logs/cleanup_duo_chats.log

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
from push_tool import DEFAULT_SECRETS_FILE, configured_servers, parse_secrets, server_connection  # noqa: E402

DEFAULT_PREFIX = "[agent_duo]"


def list_chats(session, base_url, headers):
    resp = session.get(f"{base_url}/api/v1/chats/", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def find_doomed(chats, prefix, min_age_seconds, now):
    """Chat summaries whose title starts with prefix and are at least min_age_seconds old. A chat
    with no updated_at is kept (err on the side of not deleting) rather than treated as ancient."""
    found = []
    for c in chats:
        if not (c.get("title") or "").startswith(prefix):
            continue
        updated_at = c.get("updated_at")
        if updated_at is None or now - updated_at < min_age_seconds:
            continue
        found.append(c)
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


def clean_server(session, server, base_url, api_key, prefix, min_age_seconds, now, dry_run, assume_yes, say):
    """Returns (deleted_count, failed_count) for one server."""
    headers = {"Authorization": f"Bearer {api_key}"}
    doomed = find_doomed(list_chats(session, base_url, headers), prefix, min_age_seconds, now)
    say(f"[{server}] {len(doomed)} chat(s) matching {prefix!r}" + (" (dry run)." if dry_run else "."))
    for c in doomed:
        say(f"  - {c['id'][:8]}  {(c.get('title') or '')[:60]}")
    if dry_run or not doomed:
        return 0, 0
    if not assume_yes:
        if not sys.stdin.isatty() or input(f"[{server}] Delete {len(doomed)} chat(s)? [y/N] ").strip().lower() != "y":
            say(f"[{server}] Aborted.")
            return 0, 0
    deleted = failed = 0
    for c in doomed:
        resp = session.delete(f"{base_url}/api/v1/chats/{c['id']}", headers=headers, timeout=30)
        if resp.status_code == 200:
            deleted += 1
        else:
            failed += 1
            say(f"  failed to delete {c['id'][:8]}: {resp.status_code} {str(resp.text)[:200]}")
    say(f"[{server}] Deleted {deleted}/{len(doomed)}.")
    return deleted, failed


def main(argv=None, session=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", action="append", metavar="NAME",
                     help="Server to clean: local, a name configured as OWUI_<NAME>_URL/_KEY, or 'all'. "
                          "May be repeated. Default: local plus every server secrets.md configures.")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help=f'title prefix to match (default "{DEFAULT_PREFIX}")')
    ap.add_argument("--min-age-minutes", type=float, default=60)
    ap.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation (for cron)")
    ap.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    ap.add_argument("--log", type=Path, help="append this run's output to FILE, each line timestamped (UTC)")
    args = ap.parse_args(argv)
    say = make_reporter(args.log)

    values = parse_secrets(args.secrets)
    extras = configured_servers(values)
    wanted = args.server or (["local"] + extras)
    servers = ["local"] + extras if "all" in wanted else list(dict.fromkeys(wanted))
    unknown = [x for x in servers if x != "local" and x not in extras]
    if unknown:
        ap.error(f"Unknown server(s) {unknown}; secrets.md configures: {['local'] + extras}.")

    if session is None:
        session = requests.Session()

    now = time.time()
    total_deleted = total_failed = 0
    for server in servers:
        base_url, api_key = server_connection(values, server)
        deleted, failed = clean_server(
            session, server, base_url.rstrip("/"), api_key, args.prefix,
            args.min_age_minutes * 60, now, args.dry_run, args.yes, say,
        )
        total_deleted += deleted
        total_failed += failed
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
