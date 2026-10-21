"""
title: Append File
author: Gordon
version: 0.2.0
description: Adds text to the end of a file in Mara's drafts folder through Open Terminal, which has no append of its own (it reads the file, adds the text, writes it back). Only files under ALLOWED_ROOTS, never a PROTECTED_FILES file; never changes the text already in the file.
"""

import asyncio
import json
import posixpath
from typing import Any, Dict, Optional

import requests
from pydantic import BaseModel, Field


def parse_list(value: str) -> "list[str]":
    """A comma-separated valve as a list, without blanks."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def clean_path(path: str, roots: "list[str]") -> "tuple[Optional[str], Optional[str]]":
    """(normalised path, None) when the path is a file path under one of the roots, else (None, the reason)."""
    roots = [posixpath.normpath(r) for r in roots if r.startswith("/")]
    if not roots:
        return None, "No folder is allowed yet. Tell Gordon."
    p = (path or "").strip()
    if not p:
        return None, "Give the full path of the file, such as " + roots[0].rstrip("/") + "/story/notes.md."
    if not p.startswith("/"):
        return None, f"Use the full path, starting with {roots[0].rstrip('/')}/."
    if p.endswith("/"):
        return None, "That is a folder. Give a file path."
    norm = posixpath.normpath(p)
    for root in roots:
        if norm != root and norm.startswith(root.rstrip("/") + "/"):
            return norm, None
    return None, "I can only add to files under " + ", ".join(r.rstrip("/") + "/" for r in roots) + "."


def is_protected(norm: str, protected: "list[str]") -> bool:
    """True when the file is on the protected list: an entry with a slash is a full path, any other entry is a file name in any folder (case ignored)."""
    base = posixpath.basename(norm).lower()
    for entry in protected:
        if "/" in entry:
            if posixpath.normpath(entry).lower() == norm.lower():
                return True
        elif entry.lower() == base:
            return True
    return False


def join_text(existing: str, text: str) -> str:
    """The file's new content: what was there, unchanged, a blank line, then the new text ending in a single newline."""
    new = text.strip("\n") + "\n"
    if not existing.strip("\n"):
        return new if not existing else existing + new
    if existing.endswith("\n\n"):
        return existing + new
    return existing + ("\n" if existing.endswith("\n") else "\n\n") + new


class Tools:
    class Valves(BaseModel):
        TERMINAL_URL: str = Field(default="http://open-terminal:8000", description="Open Terminal's address as seen from the Open WebUI container.")
        API_KEY: str = Field(default="", description="Open Terminal's API key (OPEN_TERMINAL_API_KEY).")
        ALLOWED_ROOTS: str = Field(default="/companion/drafts", description="Comma-separated folders; only files under them can be added to.")
        PROTECTED_FILES: str = Field(default="charter.md", description="Comma-separated files that are never changed: a file name (any folder) or a full path.")
        SEND_USER_ID: bool = Field(default=True, description="Send the calling user's id so Open Terminal runs the write as that user.")
        MAX_APPEND_CHARS: int = Field(default=20000, description="The most text one call may add.")
        MAX_FILE_CHARS: int = Field(default=200000, description="Refuse to rewrite a file longer than this.")
        CREATE_IF_MISSING: bool = Field(default=True, description="Create the file when it does not exist yet.")
        TIMEOUT_SECONDS: int = Field(default=30, description="Per request to Open Terminal.")

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self, user_id: Optional[str] = None) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.valves.API_KEY}"}
        if user_id and self.valves.SEND_USER_ID:
            h["X-User-Id"] = str(user_id)
        return h

    def _append(self, path: str, text: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        v = self.valves
        if not v.API_KEY:
            return {"success": False, "error": "The tool has no Open Terminal API key yet. Tell Gordon."}
        norm, why = clean_path(path, parse_list(v.ALLOWED_ROOTS))
        if why:
            return {"success": False, "error": why}
        if is_protected(norm, parse_list(v.PROTECTED_FILES)):
            return {"success": False, "error": "That file is protected. Gordon changes it himself."}
        if not (text or "").strip():
            return {"success": False, "error": "There is no text to add."}
        if len(text) > v.MAX_APPEND_CHARS:
            return {"success": False, "error": f"That is {len(text)} characters; add at most {v.MAX_APPEND_CHARS} at a time."}
        base = v.TERMINAL_URL.rstrip("/")
        try:
            r = requests.get(f"{base}/files/read", params={"path": norm}, headers=self._headers(user_id), timeout=v.TIMEOUT_SECONDS)
            created = False
            if r.status_code == 404:
                if not v.CREATE_IF_MISSING:
                    return {"success": False, "error": f"{norm} does not exist."}
                existing, created = "", True
            elif r.status_code == 401:
                return {"success": False, "error": "Open Terminal refused the API key. Tell Gordon."}
            elif r.status_code != 200:
                return {"success": False, "error": f"Could not read the file (HTTP {r.status_code})."}
            else:
                data = r.json()
                existing = data.get("content")
                if not isinstance(existing, str):
                    return {"success": False, "error": "That file is not plain text, so I left it alone."}
                total = data.get("total_lines")
                if isinstance(total, int) and len(existing.splitlines()) != total:
                    return {"success": False, "error": "I could not read the whole file, so I did not change it."}
                if len(existing) > v.MAX_FILE_CHARS:
                    return {"success": False, "error": "That file is too long to rewrite safely."}
            new = join_text(existing, text)
            w = requests.post(f"{base}/files/write", json={"path": norm, "content": new}, headers=self._headers(user_id), timeout=v.TIMEOUT_SECONDS)
            if w.status_code != 200:
                return {"success": False, "error": f"Could not write the file (HTTP {w.status_code}); it is unchanged."}
            out: Dict[str, Any] = {"success": True, "path": norm, "created": created, "added_chars": len(new) - len(existing), "size_after": w.json().get("size")}
            if out["size_after"] != len(new.encode("utf-8")):
                out["warning"] = "The saved size is not what I expected. Read the file to check it."
            return out
        except requests.RequestException as e:
            return {"success": False, "error": f"Could not reach Open Terminal ({type(e).__name__}). Tell Gordon."}

    async def append_file(self, path: str, text: str, __user__: Optional[dict] = None) -> str:
        """
        Add text to the end of one of your drafts files (under /companion/drafts/). It never changes what is already in the file, and it
        creates the file if it does not exist. Use it to save a new section to your notes file.

        :param path: Full path of the file, such as /companion/drafts/lighthouse/lighthouse-notes.md
        :param text: The text to add. Start it with its own heading if it is a new section.
        :return: JSON with success and the new size, or an error saying what to fix.
        """
        user_id = (__user__ or {}).get("id")
        result = await asyncio.to_thread(self._append, path, text, user_id)
        return json.dumps(result, ensure_ascii=False)
