"""
title: Folder Chat Search
author: Gordon
version: 0.2.0
description: Searches and reads the user's earlier chats like Open WebUI's built-in search_chats and view_chat, but only inside the chat folders the owner has named in a valve. The model cannot choose or see the folders, and the owner caps how many results a search returns and how much of a chat one read returns.
"""

import asyncio
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

_STOP = {"or", "and"}


class SearchError(RuntimeError):
    """A problem the model can be told about in a sentence."""


def search_words(query: str) -> List[str]:
    """The query as lower-case keywords: punctuation and the words OR / AND dropped, repeats removed."""
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if w not in _STOP]
    return list(dict.fromkeys(words))


def to_int(value: Any, name: str) -> Optional[int]:
    """A whole number from the model's text ('' or None = not given)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        raise SearchError(f"{name} must be a number.") from None


def configured_folders(text: str) -> List[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def resolve_folder_ids(conn: sqlite3.Connection, user_id: str, wanted: List[str], subfolders: bool) -> Dict[str, str]:
    """{folder id: folder name} for this user's folders matching the configured names (any case) or ids, plus everything inside
    them when `subfolders`."""
    rows = conn.execute("SELECT id, name, parent_id FROM folder WHERE user_id = ?", (user_id,)).fetchall()
    names = {w.lower() for w in wanted}
    found = {r[0]: r[1] for r in rows if r[0] in wanted or (r[1] or "").lower() in names}
    if subfolders:
        grew = True
        while grew:
            grew = False
            for fid, name, parent in rows:
                if parent in found and fid not in found:
                    found[fid] = name
                    grew = True
    return found


def message_texts(chat: Dict[str, Any]) -> List[str]:
    """Every message's text, in no particular order (stored chats keep them in history.messages, older ones in messages)."""
    msgs = ((chat.get("history") or {}).get("messages")) or chat.get("messages") or {}
    if isinstance(msgs, dict):
        msgs = list(msgs.values())
    out: List[str] = []
    for m in msgs if isinstance(msgs, list) else []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            content = " ".join(str(p["text"]) for p in content if isinstance(p, dict) and p.get("text"))
        if isinstance(content, str) and content:
            out.append(content)
    return out


def make_snippet(texts: List[str], words: List[str], chars: int) -> str:
    """About `chars` characters around the first place any keyword appears (the first keyword that appears anywhere wins)."""
    for w in words:
        for t in texts:
            i = t.lower().find(w)
            if i >= 0:
                start = max(0, i - chars // 3)
                end = min(len(t), start + chars)
                return ("..." if start else "") + t[start:end].replace("\n", " ") + ("..." if end < len(t) else "")
    return ""


def search_folder_chats_db(
    db_path: str, user_id: str, folders: List[str], subfolders: bool, query: str, limit: int,
    start: Optional[int], end: Optional[int], include_archived: bool, current_chat: Optional[str], snippet_chars: int,
) -> Tuple[int, List[Dict[str, Any]], str]:
    """(how many chats match, the best `limit` of them, 'all' or 'any'). A chat matches when its title or messages hold every
    keyword; if none does, any of them (most keywords first, then newest)."""
    words = search_words(query)
    if not words:
        raise SearchError("query is required.")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        raise SearchError("the chat database could not be opened.") from None
    try:
        ids = resolve_folder_ids(conn, user_id, folders, subfolders)
        if not ids:
            raise SearchError("none of the configured folders were found.")
        marks = ",".join("?" * len(ids))
        sql = f"SELECT id, title, updated_at, folder_id, chat, meta FROM chat WHERE user_id = ? AND folder_id IN ({marks})"
        args: List[Any] = [user_id, *ids]
        if not include_archived:
            sql += " AND archived = 0"
        if start is not None:
            sql += " AND updated_at >= ?"
            args.append(start)
        if end is not None:
            sql += " AND updated_at < ?"
            args.append(end)
        hits: List[Tuple[int, int, Dict[str, Any]]] = []
        for cid, title, updated, fid, raw, meta in conn.execute(sql, args):
            if cid == current_chat:
                continue
            try:
                if (json.loads(meta or "{}") or {}).get("internal"):
                    continue
                chat = json.loads(raw or "{}") or {}
            except ValueError:
                continue
            texts = message_texts(chat)
            haystack = ((title or "") + "\n" + "\n".join(texts)).lower()
            matched = sum(1 for w in words if w in haystack)
            if matched:
                snip = make_snippet(texts, words, snippet_chars) or (f"Title match: {title}" if title else "")
                hits.append((matched, updated or 0, {"id": cid, "title": title, "snippet": snip, "updated_at": updated, "folder": ids[fid]}))
    finally:
        conn.close()
    full = [h for h in hits if h[0] == len(words)]
    mode, pool = ("all", full) if full else ("any", hits)
    pool.sort(key=lambda h: (-h[0], -h[1]))
    return len(pool), [h[2] for h in pool[: max(0, limit)]], mode


def view_folder_chat_db(db_path: str, user_id: str, folders: List[str], subfolders: bool, chat_id: str, first: int, budget: int) -> Dict[str, Any]:
    """One chat from the allowed folders as its message chain in order, from message `first` (1-based), whole messages until
    `budget` characters are used (a single longer message is cut). A chat outside the folders looks exactly like a missing one."""
    missing = SearchError("chat not found.")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        raise SearchError("the chat database could not be opened.") from None
    try:
        ids = resolve_folder_ids(conn, user_id, folders, subfolders)
        if not ids:
            raise SearchError("none of the configured folders were found.")
        row = conn.execute("SELECT id, title, created_at, updated_at, folder_id, chat, meta FROM chat WHERE id = ? AND user_id = ?", (chat_id, user_id)).fetchone()
    finally:
        conn.close()
    if not row or row[4] not in ids:
        raise missing
    try:
        chat = json.loads(row[5] or "{}") or {}
    except ValueError:
        raise missing from None
    history = chat.get("history") or {}
    by_id = history.get("messages") or {}
    chain: List[Dict[str, str]] = []
    if isinstance(by_id, dict) and history.get("currentId"):
        seen, cur = set(), history.get("currentId")
        while cur and cur not in seen and isinstance(by_id.get(cur), dict):
            seen.add(cur)
            m = by_id[cur]
            chain.append({"role": m.get("role", ""), "content": m.get("content") if isinstance(m.get("content"), str) else " ".join(message_texts({"messages": [m]}))})
            cur = m.get("parentId")
        chain.reverse()
    else:  # no current-message pointer (or the older flat layout): every message as stored
        flat = list(by_id.values()) if isinstance(by_id, dict) and by_id else (chat.get("messages") or [])
        chain = [{"role": m.get("role", ""), "content": " ".join(message_texts({"messages": [m]}))} for m in flat if isinstance(m, dict)]
    total = len(chain)
    start = min(max(first, 1), max(total, 1))
    out: List[Dict[str, str]] = []
    used = 0
    for m in chain[start - 1:]:
        room = budget - used
        if room <= 0:
            break
        if len(m["content"]) > room:
            if out:
                break
            m = {"role": m["role"], "content": m["content"][:room] + "... [message cut]"}
        out.append(m)
        used += len(m["content"])
    result: Dict[str, Any] = {"id": row[0], "title": row[1], "folder": ids[row[4]], "created_at": row[2], "updated_at": row[3], "total_messages": total, "first_message": start, "messages": out}
    if start - 1 + len(out) < total:
        result["more"] = f"{total - (start - 1 + len(out))} more messages; call again with first_message={start + len(out)}."
    return result


class Tools:
    # Numbers are typed str on purpose: Open WebUI runs int() on a string given for an int parameter before the method sees it,
    # so a model passing "" for "not set" would fail there. to_int() does the parsing.
    class Valves(BaseModel):
        FOLDERS: str = Field(default="", description="Comma-separated chat folders the tool may search, by name (any case) or folder id. Empty = the tool refuses to search at all. The model can neither pick nor see these.")
        INCLUDE_SUBFOLDERS: bool = Field(default=True, description="Also search folders inside the listed ones.")
        MAX_RESULTS: int = Field(default=10, description="The most chats one search can return, whatever count the model asks for.")
        DEFAULT_COUNT: int = Field(default=5, description="How many chats to return when the model gives no count (never more than MAX_RESULTS).")
        SNIPPET_CHARS: int = Field(default=200, description="Length of the excerpt shown for each result.")
        INCLUDE_ARCHIVED: bool = Field(default=False, description="Also search archived chats in those folders.")
        MAX_VIEW_CHARS: int = Field(default=8000, description="Character budget for one view_folder_chat reply (about a quarter as many tokens). Whole messages only; the reply says where to continue.")
        CHATS_DB_PATH: str = Field(default="/app/backend/data/webui.db", description="Open WebUI's SQLite database (opened read-only).")

    def __init__(self):
        self.valves = self.Valves()

    async def search_folder_chats(
        self,
        query: str,
        count: str = "",
        start_timestamp: str = "",
        end_timestamp: str = "",
        __user__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
    ) -> str:
        """
        Search your earlier chats (the current one excluded) by title and message text. Only a fixed set of chat folders is searched.
        :param query: Keywords to look for. A chat with all of them comes first; if none has all, chats with some of them. Do not use OR/AND.
        :param count: How many results to return, or "" for the default (the tool has its own upper limit).
        :param start_timestamp: Only chats updated at or after this Unix time in seconds, or "".
        :param end_timestamp: Only chats updated before this Unix time in seconds, or "".
        :return: JSON with the matching chats (id, title, snippet, updated_at, folder).
        """
        v = self.valves
        user_id = (__user__ or {}).get("id")
        try:
            if not user_id:
                raise SearchError("user context not available.")
            folders = configured_folders(v.FOLDERS)
            if not folders:
                raise SearchError("chat search is not set up (no folders configured).")
            cap = max(1, v.MAX_RESULTS)
            asked = to_int(count, "count")
            limit = min(cap, asked if asked and asked > 0 else max(1, v.DEFAULT_COUNT))
            total, rows, mode = await asyncio.to_thread(
                search_folder_chats_db, v.CHATS_DB_PATH, user_id, folders, v.INCLUDE_SUBFOLDERS, query, limit,
                to_int(start_timestamp, "start_timestamp"), to_int(end_timestamp, "end_timestamp"),
                v.INCLUDE_ARCHIVED, __chat_id__, max(40, v.SNIPPET_CHARS),
            )
        except SearchError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:  # noqa: BLE001 - report, never raise into the chat
            return json.dumps({"error": f"search failed ({type(e).__name__})."})
        out: Dict[str, Any] = {"matched": mode, "total_matches": total, "returned": len(rows), "results": rows}
        if mode == "any" and rows:
            out["note"] = "No chat had every keyword; these have some of them."
        return json.dumps(out, ensure_ascii=False)

    async def view_folder_chat(
        self,
        chat_id: str,
        first_message: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Read an earlier chat found with search_folder_chats: its messages in order. A long chat comes back in parts.
        :param chat_id: The id of the chat, as search_folder_chats returned it.
        :param first_message: Message number to start from (1 = the beginning), or "" to start at the beginning. Use the number a previous reply gave under "more".
        :return: JSON with the chat's id, title, and messages.
        """
        v = self.valves
        user_id = (__user__ or {}).get("id")
        try:
            if not user_id:
                raise SearchError("user context not available.")
            folders = configured_folders(v.FOLDERS)
            if not folders:
                raise SearchError("chat search is not set up (no folders configured).")
            result = await asyncio.to_thread(
                view_folder_chat_db, v.CHATS_DB_PATH, user_id, folders, v.INCLUDE_SUBFOLDERS, str(chat_id or "").strip(),
                to_int(first_message, "first_message") or 1, max(500, v.MAX_VIEW_CHARS),
            )
        except SearchError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"could not read the chat ({type(e).__name__})."})
        return json.dumps(result, ensure_ascii=False)
