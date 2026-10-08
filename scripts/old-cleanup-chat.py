#!/usr/bin/env python3
"""
Open WebUI chat cleanup — retains:
  • chats updated in the last 15 days
  • chats that belong to a project folder (folder_id != null)
Deletes the rest.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://open-webui.dimension-x.net"
API_KEY = os.getenv("OPEN_WEBUI_KEY")

if not API_KEY:
    try:
        API_KEY = input("Enter Open WebUI API key (Bearer token): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted: no API key provided.")
        sys.exit(1)

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def fetch_all_chats():
    """Paginate through /api/v1/chats/ and return a flat list."""
    all_chats = []
    page = 1
    while True:
        resp = SESSION.get(f"{BASE_URL}/api/v1/chats/", params={"page": page, "per_page": 100})
        resp.raise_for_status()
        data = resp.json()
        # Open WebUI returns {"chats": [...], "total": ..., "page": ..., "per_page": ...}
        chats = data.get("chats", data) if isinstance(data, dict) else data
        if not chats:
            break
        all_chats.extend(chats)
        page += 1
    return all_chats

def should_keep(chat, cutoff_ts):
    updated_at = chat.get("updated_at")
    folder_id = chat.get("folder_id")
    if updated_at is None:
        return True  # malformed, err on side of caution
    # updated_at is Unix seconds (float/int)
    if updated_at >= cutoff_ts:
        return True
    if folder_id is not None:
        return True
    return False

def delete_chat(chat_id):
    resp = SESSION.delete(f"{BASE_URL}/api/v1/chats/{chat_id}")
    if resp.status_code == 200:
        return True
    else:
        print(f"  ✗ Failed to delete {chat_id}: {resp.status_code} {resp.text}")
        return False

def main(dry_run=False):
    cutoff = datetime.now(timezone.utc) - timedelta(days=15)
    cutoff_ts = cutoff.timestamp()

    print(f"Fetching chats from {BASE_URL} …")
    chats = fetch_all_chats()
    print(f"Total chats: {len(chats)}")

    to_delete = []
    for chat in chats:
        if not should_keep(chat, cutoff_ts):
            to_delete.append(chat)

    print(f"Chats eligible for deletion: {len(to_delete)}")
    if dry_run:
        print("\nDRY RUN — would delete:")
        for c in to_delete:
            ts = datetime.fromtimestamp(c["updated_at"], tz=timezone.utc).isoformat()
            folder = c.get("folder_id") or "root"
            print(f"  - {c['id'][:8]}…  {c['title'][:50]:50}  updated={ts}  folder={folder}")
        return

    confirm = input(f"\nDelete {len(to_delete)} chats? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    deleted = 0
    for chat in to_delete:
        print(f"Deleting {chat['id'][:8]}… {chat['title'][:50]}", end=" ")
        if delete_chat(chat["id"]):
            print("✓")
            deleted += 1
        else:
            print("✗")
        time.sleep(0.1)  # be nice to the API

    print(f"\nDone. Deleted {deleted}/{len(to_delete)} chats.")

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
