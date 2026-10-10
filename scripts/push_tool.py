#!/usr/bin/env python3
"""
Push local tool source (src/<name>.py) into a running Open WebUI (OWUI) instance via its Tools
API, so a revision can be deployed without manually re-importing through the OWUI web UI.

Reads connection details and per-tool ids from secrets.md at the repo root (gitignored - see
secrets.example.md for the expected format and how to generate an API key). Never commit
secrets.md.

Preserves each tool's existing name/meta/access_grants: this script fetches the tool's current
record first and only replaces its 'content' field, rather than overwriting everything blind.
That same fetch is also used to back up the tool's current (about-to-be-overwritten) source to
tmp/ before every real push, timestamped, so a bad push is one file-copy away from undone.

Servers: the default is the one in OWUI_BASE_URL / OWUI_API_KEY ("local", tool ids are the bare
`<tool>: <id>` lines). Any other server X is configured by OWUI_X_URL and OWUI_X_KEY lines (e.g.
OWUI_HANNAH_URL / OWUI_HANNAH_KEY -> --server hannah). Its tool ids are looked up on the server
(a tool whose id equals the tool's file name) unless secrets.md has an `X_<tool>: <id>` line (e.g.
HANNAH_agent_notes) to override that. --server may be repeated, and --server all means local plus
every configured server. One server failing doesn't stop the others; the exit status says so.

Functions: `agent_duo` is an Open WebUI *Function* (a Pipe), not a Tool, so it goes through
/api/v1/functions/ instead of /api/v1/tools/. Its id is the bare `agent_duo: <id>` line like a tool's
(on the default server only - it isn't part of `all`, and other servers only get it when asked for by name).

Usage:
    python scripts/push_tool.py comfy_sdxl_direct
    python scripts/push_tool.py comfy_sdxl_graph comfy_sdxl_retrieve
    python scripts/push_tool.py all
    python scripts/push_tool.py all --dry-run
    python scripts/push_tool.py agent_notes --server all
    python scripts/push_tool.py agent_notes --server hannah --dry-run
    python scripts/push_tool.py agent_duo
"""
import argparse
import datetime
import re
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
DEFAULT_SECRETS_FILE = REPO_ROOT / "secrets.md"
BACKUP_DIR = REPO_ROOT / "tmp"

TOOL_NAMES = ["comfy_sdxl_direct", "comfy_sdxl_graph", "comfy_sdxl_retrieve", "comfy_sdxl_poses", "agent_notes", "nextcloud_mail"]
FUNCTION_NAMES = ["agent_duo"]  # Open WebUI Functions (Pipes), pushed via /api/v1/functions/ - not part of 'all'

_KV_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.+?)\s*$")


def parse_secrets(path: Path) -> dict:
    """A minimal 'KEY: value' parser for secrets.md - one entry per non-blank, non-heading line."""
    if not path.exists():
        sys.exit(
            f"No secrets file at {path}.\n"
            f"Copy secrets.example.md to {path.name} and fill in OWUI_BASE_URL, OWUI_API_KEY, "
            "and each tool's id."
        )
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_RE.match(line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def require(values: dict, key: str) -> str:
    val = values.get(key)
    if not val or val.startswith("<"):
        sys.exit(f"{key} is missing or still a placeholder in secrets.md.")
    return val


def backup_current_content(tool_name: str, content: str, server: str = "local") -> Path:
    """Save the tool's current (pre-overwrite) source to tmp/, timestamped. tmp/ is gitignored -
    these are local safety copies, not part of the repo's history."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = "" if server == "local" else f"_{server}"
    backup_path = BACKUP_DIR / f"{tool_name}{label}_{timestamp}.py"
    backup_path.write_text(content, encoding="utf-8")
    return backup_path


def api_kind(name: str) -> str:
    """The Open WebUI API collection a source file is pushed to."""
    return "functions" if name in FUNCTION_NAMES else "tools"


def push_one(tool_name: str, base_url: str, api_key: str, tool_id: str, dry_run: bool, server: str = "local") -> None:
    kind = api_kind(tool_name)
    tag = tool_name if server == "local" else f"{server}/{tool_name}"
    local_path = SRC_DIR / f"{tool_name}.py"
    if not local_path.exists():
        sys.exit(f"No such file: {local_path}")
    new_content = local_path.read_text(encoding="utf-8")

    headers = {"Authorization": f"Bearer {api_key}"}
    get_resp = requests.get(f"{base_url}/api/v1/{kind}/id/{tool_id}", headers=headers, timeout=15)
    if get_resp.status_code != 200:
        sys.exit(
            f"[{tag}] Could not fetch {kind[:-1]} {tool_id!r} ({get_resp.status_code}): {get_resp.text[:300]}\n"
            "Check the id in secrets.md and that your API key's account owns (or is admin over) this tool."
        )
    current = get_resp.json()

    if dry_run:
        old_content = current.get("content", "")
        changed = "yes" if old_content != new_content else "no (identical)"
        print(
            f"[{tag}] id={tool_id} name={current.get('name')!r} would push: changed={changed}, "
            f"{len(new_content)} bytes (currently {len(old_content)} bytes)"
        )
        return

    old_content = current.get("content")
    if old_content:
        backup_path = backup_current_content(tool_name, old_content, server)
        print(f"[{tag}] backed up current content to {backup_path}")
    else:
        print(f"[{tag}] WARNING: current content wasn't readable (no write access?) - no backup written")

    # Only 'content' actually changes here - name/meta/access_grants are echoed back as-is so
    # this never clobbers settings made through the OWUI UI. The server itself recomputes
    # meta.manifest and meta.has_user_valves from the new content's frontmatter/UserValves class
    # regardless of what's sent, so nothing more needs doing with those fields.
    body = {
        "id": tool_id,
        "name": current.get("name", tool_name),
        "content": new_content,
        "meta": current.get("meta") or {},
    }
    if kind == "tools":  # a Function has no access_grants; sending one would be rejected or ignored
        body["access_grants"] = current.get("access_grants")
    post_resp = requests.post(
        f"{base_url}/api/v1/{kind}/id/{tool_id}/update", headers=headers, json=body, timeout=30
    )
    if post_resp.status_code != 200:
        sys.exit(f"[{tag}] Update failed ({post_resp.status_code}): {post_resp.text[:500]}")
    updated = post_resp.json()
    print(
        f"[{tag}] pushed OK - id={tool_id} name={updated.get('name')!r} "
        f"updated_at={updated.get('updated_at')}"
    )


_SERVER_URL_RE = re.compile(r"^OWUI_([A-Za-z0-9]+)_URL$")


def configured_servers(values: dict) -> list:
    """Names (lowercase) of the extra servers secrets.md configures: those with both OWUI_X_URL and OWUI_X_KEY."""
    names = []
    for key in values:
        m = _SERVER_URL_RE.match(key)
        if m and f"OWUI_{m.group(1)}_KEY" in values:
            names.append(m.group(1).lower())
    return names


def server_connection(values: dict, server: str) -> "tuple[str, str]":
    if server == "local":
        return require(values, "OWUI_BASE_URL").rstrip("/"), require(values, "OWUI_API_KEY")
    upper = server.upper()
    return require(values, f"OWUI_{upper}_URL").rstrip("/"), require(values, f"OWUI_{upper}_KEY")


def resolve_tool_id(values: dict, server: str, tool_name: str, base_url: str, api_key: str) -> str:
    """local: the bare `<tool>` line. Other servers: an `X_<tool>` line if there is one, else the id the server itself lists."""
    if server == "local":
        return require(values, tool_name)
    override = values.get(f"{server.upper()}_{tool_name}")
    if override and not override.startswith("<"):
        return override
    kind = api_kind(tool_name)
    resp = requests.get(f"{base_url}/api/v1/{kind}/", headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
    if resp.status_code != 200:
        sys.exit(f"[{server}/{tool_name}] Could not list {kind} on {server} ({resp.status_code}): {resp.text[:300]}")
    ids = [t.get("id") for t in resp.json() if isinstance(t, dict)]
    if tool_name in ids:
        return tool_name
    sys.exit(
        f"[{server}/{tool_name}] No tool with id {tool_name!r} on {server} (it has: {ids}). push_tool.py only updates an existing tool: "
        f"import it once through OWUI's web UI, or add a `{server.upper()}_{tool_name}: <id>` line to secrets.md."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tools", nargs="+", help=f"One or more of {TOOL_NAMES + FUNCTION_NAMES}, or 'all' (the tools).")
    parser.add_argument("--server", action="append", metavar="NAME", help="Server to push to: local (the default), a name configured as OWUI_<NAME>_URL/_KEY, or 'all'. May be repeated.")
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without pushing.")
    args = parser.parse_args()

    if "all" in args.tools:
        if len(args.tools) > 1:
            parser.error("'all' can't be combined with specific tool names.")
        names = TOOL_NAMES
    else:
        names = args.tools
        unknown = [n for n in names if n not in TOOL_NAMES + FUNCTION_NAMES]
        if unknown:
            parser.error(f"Unknown tool name(s) {unknown}; choose from {TOOL_NAMES + FUNCTION_NAMES} or 'all'.")

    values = parse_secrets(args.secrets_file)
    extras = configured_servers(values)
    wanted = args.server or ["local"]
    servers = ["local"] + extras if "all" in wanted else list(dict.fromkeys(wanted))
    unknown = [x for x in servers if x != "local" and x not in extras]
    if unknown:
        parser.error(f"Unknown server(s) {unknown}; secrets.md configures: {['local'] + extras}.")

    failed = []
    for server in servers:
        try:
            base_url, api_key = server_connection(values, server)
            for name in names:
                push_one(name, base_url, api_key, resolve_tool_id(values, server, name, base_url, api_key), args.dry_run, server)
        except SystemExit as ex:  # push_one and friends exit with a message; keep going to the next server
            print(ex.code if isinstance(ex.code, str) else f"[{server}] failed", file=sys.stderr)
            failed.append(server)
    if failed:
        sys.exit(f"Failed on: {', '.join(failed)}")


if __name__ == "__main__":
    main()
