#!/usr/bin/env python3
"""
Push local tool source (src/<name>.py) into a running Open WebUI (OWUI) instance via its Tools
API, so a revision can be deployed without manually re-importing through the OWUI web UI.

Reads connection details and per-tool ids from secrets.md at the repo root (gitignored - see
secrets.example.md for the expected format and how to generate an API key). Never commit
secrets.md.

Preserves each tool's existing name/meta/access_grants: this script fetches the tool's current
record first and only replaces its 'content' field, rather than overwriting everything blind.

Usage:
    python scripts/push_tool.py comfy_sdxl_direct
    python scripts/push_tool.py comfy_sdxl_graph comfy_sdxl_retrieve
    python scripts/push_tool.py all
    python scripts/push_tool.py all --dry-run
"""
import argparse
import re
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
DEFAULT_SECRETS_FILE = REPO_ROOT / "secrets.md"

TOOL_NAMES = ["comfy_sdxl_direct", "comfy_sdxl_graph", "comfy_sdxl_retrieve"]

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


def push_one(tool_name: str, base_url: str, api_key: str, tool_id: str, dry_run: bool) -> None:
    local_path = SRC_DIR / f"{tool_name}.py"
    if not local_path.exists():
        sys.exit(f"No such file: {local_path}")
    new_content = local_path.read_text(encoding="utf-8")

    headers = {"Authorization": f"Bearer {api_key}"}
    get_resp = requests.get(f"{base_url}/api/v1/tools/id/{tool_id}", headers=headers, timeout=15)
    if get_resp.status_code != 200:
        sys.exit(
            f"[{tool_name}] Could not fetch tool {tool_id!r} ({get_resp.status_code}): {get_resp.text[:300]}\n"
            "Check the id in secrets.md and that your API key's account owns (or is admin over) this tool."
        )
    current = get_resp.json()

    if dry_run:
        old_content = current.get("content", "")
        changed = "yes" if old_content != new_content else "no (identical)"
        print(
            f"[{tool_name}] id={tool_id} name={current.get('name')!r} would push: changed={changed}, "
            f"{len(new_content)} bytes (currently {len(old_content)} bytes)"
        )
        return

    # Only 'content' actually changes here - name/meta/access_grants are echoed back as-is so
    # this never clobbers settings made through the OWUI UI. The server itself recomputes
    # meta.manifest and meta.has_user_valves from the new content's frontmatter/UserValves class
    # regardless of what's sent, so nothing more needs doing with those fields.
    body = {
        "id": tool_id,
        "name": current.get("name", tool_name),
        "content": new_content,
        "meta": current.get("meta") or {},
        "access_grants": current.get("access_grants"),
    }
    post_resp = requests.post(
        f"{base_url}/api/v1/tools/id/{tool_id}/update", headers=headers, json=body, timeout=30
    )
    if post_resp.status_code != 200:
        sys.exit(f"[{tool_name}] Update failed ({post_resp.status_code}): {post_resp.text[:500]}")
    updated = post_resp.json()
    print(
        f"[{tool_name}] pushed OK - id={tool_id} name={updated.get('name')!r} "
        f"updated_at={updated.get('updated_at')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tools", nargs="+", help=f"One or more of {TOOL_NAMES}, or 'all'.")
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without pushing.")
    args = parser.parse_args()

    if "all" in args.tools:
        if len(args.tools) > 1:
            parser.error("'all' can't be combined with specific tool names.")
        names = TOOL_NAMES
    else:
        names = args.tools
        unknown = [n for n in names if n not in TOOL_NAMES]
        if unknown:
            parser.error(f"Unknown tool name(s) {unknown}; choose from {TOOL_NAMES} or 'all'.")

    values = parse_secrets(args.secrets_file)
    base_url = require(values, "OWUI_BASE_URL").rstrip("/")
    api_key = require(values, "OWUI_API_KEY")

    for name in names:
        tool_id = require(values, name)
        push_one(name, base_url, api_key, tool_id, args.dry_run)


if __name__ == "__main__":
    main()
