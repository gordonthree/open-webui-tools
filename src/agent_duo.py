"""
title: Agent Duo
author: Gordon
version: 1.0.0
description: A Pipe that puts two agents, each living on its own Open WebUI server, into one chat with
    the human. It shows up as a model ("Mara + Hannah"). Each message you send starts a bounded round of
    alternating turns: the pipe asks one agent, then the other, through that agent's own server, so each
    keeps her own persona, tools (agent_notes etc.) and knowledge. Both replies stream into this chat under
    a bold speaker label. An agent can hand the floor back by replying with just the END_MARKER.
"""

import asyncio
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

import requests
from pydantic import BaseModel, Field

# Same spellings every tool in this project treats as "not given".
_UNSET_STRINGS = {"", "none", "null", "default", "n/a", "undefined"}


def is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


_DETAILS_RE = re.compile(r"<details\b.*?</details>", re.DOTALL | re.IGNORECASE)


def strip_details(text: str) -> str:
    """Drop Open WebUI's collapsible tool-call / reasoning blocks, leaving only what the agent said."""
    return _DETAILS_RE.sub("", text or "").strip()


def message_text(message: dict) -> str:
    """A chat message's content as plain text (it can be a list of typed parts)."""
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content if isinstance(content, str) else ""


def parse_history(messages: List[dict], agent_names: List[str], user_name: str) -> List[dict]:
    """Turn the front chat's messages into [{"speaker": name, "text": ...}].

    The pipe writes each agent turn into an assistant message as a `**Name:** text` section, so one
    assistant message can hold several turns; they are split back apart here. System messages are skipped.
    """
    header = re.compile(r"^\*\*(" + "|".join(re.escape(n) for n in agent_names) + r"):\*\*[ \t]*", re.MULTILINE)
    notes = re.compile(
        r"^\*(?:" + "|".join(re.escape(n) for n in agent_names) + r") (?:couldn't answer|has nothing to add|returned an empty reply)[^\n]*\*[ \t]*$",
        re.MULTILINE,
    )
    turns: List[dict] = []
    for msg in messages:
        role, text = msg.get("role"), message_text(msg)
        if role == "assistant":
            text = notes.sub("", text)  # the pipe's own status notes ("X has nothing to add") aren't turns
        if role == "user":
            if text.strip():
                turns.append({"speaker": user_name, "text": text.strip()})
        elif role == "assistant":
            matches = list(header.finditer(text))
            if not matches:
                if text.strip():  # an assistant message the pipe didn't write: credit it to nobody in particular
                    turns.append({"speaker": agent_names[0], "text": text.strip()})
                continue
            for i, m in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                body = text[m.end():end].strip()
                if body:
                    turns.append({"speaker": m.group(1), "text": body})
    return turns


def build_messages(me: str, other: str, user_name: str, turns: List[dict], end_marker: str) -> List[dict]:
    """The message list one agent is shown: its own turns as `assistant`, everyone else's as `user`
    (labelled `[Name]: ...`), consecutive same-role messages merged, led by a short turn-taking note."""
    system = (
        f"You are {me}, in a group chat with {user_name} (the human) and {other}, another AI agent who runs on a "
        f"separate server and has her own memory. Messages from others are prefixed with their name in square "
        f"brackets, like [{other}]: ...; never put a name prefix on your own reply. Speak as yourself, keep it "
        f"conversational, and use your tools and memory as you normally would. If you have nothing to add and "
        f"want to hand the floor back to {user_name}, reply with exactly {end_marker} and nothing else."
    )
    out: List[dict] = [{"role": "system", "content": system}]
    for turn in turns:
        if turn["speaker"] == me:
            role, text = "assistant", turn["text"]
        else:
            role, text = "user", f"[{turn['speaker']}]: {turn['text']}"
        if len(out) > 1 and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    return out


def choose_first(turns: List[dict], agent_names: List[str], default_first: str, user_name: str) -> str:
    """Who speaks first: whichever agent the human's latest message names (alone), else the default."""
    last = next((t["text"] for t in reversed(turns) if t["speaker"] == user_name), "")
    named = [n for n in agent_names if re.search(r"\b" + re.escape(n) + r"\b", last, re.IGNORECASE)]
    if len(named) == 1:
        return named[0]
    return default_first if default_first in agent_names else agent_names[0]


def split_end_marker(reply: str, end_marker: str) -> "tuple[str, bool]":
    """(text without the marker, whether the agent yielded)."""
    if end_marker and end_marker in reply:
        return reply.replace(end_marker, "").strip(), True
    return reply.strip(), False


class AgentError(Exception):
    pass


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _check(resp, what: str) -> None:
    if resp.status_code != 200:
        raise AgentError(f"{what} failed (HTTP {resp.status_code}): {str(resp.text)[:200]}")


def fetch_tool_ids(url: str, key: str, model_id: str) -> List[str]:
    """The tool ids attached to the agent's model preset. Open WebUI's UI sends these itself; the API doesn't."""
    resp = requests.get(f"{url}/api/v1/models/model", params={"id": model_id}, headers=_headers(key), timeout=20)
    if resp.status_code == 404:
        return []
    _check(resp, "Reading the model preset")
    ids = ((resp.json().get("meta") or {}).get("toolIds")) or []
    return [i for i in ids if isinstance(i, str)]


def create_backing_chat(url: str, key: str, model_id: str, label: str, prompt: str) -> "tuple[str, str]":
    """A throwaway chat holding one user message and an empty assistant message; returns (chat_id, assistant_id).
    Open WebUI only runs the full server-side tool loop for a request that names a chat and message."""
    user_id, asst_id, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    umsg = {"id": user_id, "parentId": None, "childrenIds": [asst_id], "role": "user", "content": prompt[:200], "timestamp": now, "models": [model_id]}
    amsg = {"id": asst_id, "parentId": user_id, "childrenIds": [], "role": "assistant", "content": "", "model": model_id, "modelName": model_id, "timestamp": now}
    chat = {
        "title": f"[agent_duo] {label}", "models": [model_id], "timestamp": now * 1000,
        "history": {"messages": {user_id: umsg, asst_id: amsg}, "currentId": asst_id},
        "messages": [umsg, amsg],
    }
    resp = requests.post(f"{url}/api/v1/chats/new", headers=_headers(key), json={"chat": chat}, timeout=30)
    _check(resp, "Creating the backing chat")
    return resp.json()["id"], asst_id


def start_turn(url: str, key: str, model_id: str, chat_id: str, asst_id: str, tool_ids: List[str], messages: List[dict]) -> None:
    body = {
        "model": model_id, "stream": True, "chat_id": chat_id, "id": asst_id, "session_id": str(uuid.uuid4()),
        "messages": messages,
    }
    if tool_ids:
        body["tool_ids"] = tool_ids
    resp = requests.post(f"{url}/api/chat/completions", headers=_headers(key), json=body, timeout=60)
    _check(resp, "Starting the turn")


def read_turn(url: str, key: str, chat_id: str, asst_id: str) -> dict:
    resp = requests.get(f"{url}/api/v1/chats/{chat_id}", headers=_headers(key), timeout=30)
    _check(resp, "Reading the backing chat")
    return (((resp.json().get("chat") or {}).get("history") or {}).get("messages") or {}).get(asst_id) or {}


def delete_chat(url: str, key: str, chat_id: str) -> None:
    try:
        requests.delete(f"{url}/api/v1/chats/{chat_id}", headers=_headers(key), timeout=20)
    except requests.RequestException:
        pass  # a leftover "[agent_duo]" chat is harmless; never let cleanup fail a turn


class Pipe:
    class Valves(BaseModel):
        MARA_NAME: str = Field(default="Mara", description="Speaker label for the first agent.")
        MARA_URL: str = Field(default="http://localhost:8080", description="Base URL of the first agent's Open WebUI, as this server reaches it.")
        MARA_KEY: str = Field(default="", description="Open WebUI API key (Settings -> Account -> API Keys) with access to the first agent's model.")
        MARA_MODEL_ID: str = Field(default="medium-mara", description="Model id of the first agent's preset on her server.")
        MARA_TOOL_IDS: str = Field(default="", description="Comma-separated tool ids to give her; blank = read them from her model preset.")
        HANNAH_NAME: str = Field(default="Hannah", description="Speaker label for the second agent.")
        HANNAH_URL: str = Field(default="https://hannah.dimension-x.net", description="Base URL of the second agent's Open WebUI.")
        HANNAH_KEY: str = Field(default="", description="Open WebUI API key for the second agent's server.")
        HANNAH_MODEL_ID: str = Field(default="hannah-long", description="Model id of the second agent's preset on her server.")
        HANNAH_TOOL_IDS: str = Field(default="", description="Comma-separated tool ids to give her; blank = read them from her model preset.")
        USER_NAME: str = Field(default="Gordon", description="How the agents refer to the human.")
        MAX_TURNS: int = Field(default=4, description="Most agent turns per human message (each turn is one agent speaking).")
        FIRST_SPEAKER: str = Field(default="Mara", description="Who speaks first unless the human's message names just one of the agents.")
        END_MARKER: str = Field(default="[PASS]", description="Reply an agent uses to hand the floor back to the human.")
        TURN_TIMEOUT_SECONDS: int = Field(default=300, description="Give up on one agent turn after this long.")
        POLL_SECONDS: float = Field(default=2.0, description="How often to check whether a turn has finished.")
        KEEP_BACKING_CHATS: bool = Field(default=False, description="Keep each turn's throwaway chat on the agent's server (titled '[agent_duo] ...') instead of deleting it; handy for seeing tool calls.")

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self) -> List[dict]:
        return [{"id": "agent_duo", "name": f"{self.valves.MARA_NAME} + {self.valves.HANNAH_NAME}"}]

    def _agents(self) -> List[dict]:
        v = self.valves
        return [
            {"name": v.MARA_NAME, "url": v.MARA_URL.rstrip("/"), "key": v.MARA_KEY, "model": v.MARA_MODEL_ID, "tools": v.MARA_TOOL_IDS},
            {"name": v.HANNAH_NAME, "url": v.HANNAH_URL.rstrip("/"), "key": v.HANNAH_KEY, "model": v.HANNAH_MODEL_ID, "tools": v.HANNAH_TOOL_IDS},
        ]

    async def _run_turn(self, agent: dict, messages: List[dict]) -> str:
        """One agent turn on her own server; returns her reply text. Raises AgentError."""
        v = self.valves
        url, key, model = agent["url"], agent["key"], agent["model"]
        if is_unset(key) or is_unset(url) or is_unset(model):
            raise AgentError(f"{agent['name']}'s URL, key and model id valves must all be set.")
        try:
            if is_unset(agent["tools"]):
                tool_ids = await asyncio.to_thread(fetch_tool_ids, url, key, model)
            else:
                tool_ids = [t.strip() for t in agent["tools"].split(",") if t.strip()]
            chat_id, asst_id = await asyncio.to_thread(create_backing_chat, url, key, model, agent["name"], messages[-1]["content"])
        except requests.RequestException as ex:
            raise AgentError(f"couldn't reach {url}: {ex}")
        try:
            await asyncio.to_thread(start_turn, url, key, model, chat_id, asst_id, tool_ids, messages)
            deadline = time.monotonic() + v.TURN_TIMEOUT_SECONDS
            while True:
                await asyncio.sleep(max(0.2, v.POLL_SECONDS))
                msg = await asyncio.to_thread(read_turn, url, key, chat_id, asst_id)
                if msg.get("error"):
                    err = msg["error"]
                    raise AgentError(str(err.get("content") if isinstance(err, dict) else err)[:300])
                if msg.get("done"):
                    return strip_details(msg.get("content") or "")
                if time.monotonic() > deadline:
                    raise AgentError(f"no reply within {v.TURN_TIMEOUT_SECONDS} seconds")
        except requests.RequestException as ex:
            raise AgentError(f"lost contact with {url}: {ex}")
        finally:
            if not v.KEEP_BACKING_CHATS:
                await asyncio.to_thread(delete_chat, url, key, chat_id)

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __task__: Optional[str] = None,
    ):
        # Open WebUI also calls the selected model for titles, tags and follow-ups; answer those
        # cheaply instead of waking both agents.
        if __task__:
            canned = {
                "title_generation": json.dumps({"title": "Two-agent chat"}),
                "tags_generation": json.dumps({"tags": []}),
                "follow_up_generation": json.dumps({"follow_ups": []}),
            }
            return canned.get(str(__task__), "")
        return self._converse(body, __event_emitter__)

    async def _converse(self, body: dict, __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None):
        v = self.valves
        agents = self._agents()
        names = [a["name"] for a in agents]
        by_name = {a["name"]: a for a in agents}

        async def status(text: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done}})

        turns = parse_history(body.get("messages") or [], names, v.USER_NAME)
        if not turns or turns[-1]["speaker"] != v.USER_NAME:
            yield "Send a message first - the agents answer you."
            return

        speaker = choose_first(turns, names, v.FIRST_SPEAKER, v.USER_NAME)
        for n in range(max(1, v.MAX_TURNS)):
            agent = by_name[speaker]
            other = names[1] if speaker == names[0] else names[0]
            await status(f"{speaker} is thinking... (turn {n + 1} of {v.MAX_TURNS})")
            try:
                reply = await self._run_turn(agent, build_messages(speaker, other, v.USER_NAME, turns, v.END_MARKER))
            except AgentError as ex:
                yield f"\n\n*{speaker} couldn't answer: {ex}*\n\n"
                break
            text, yielded = split_end_marker(reply, v.END_MARKER)
            if text:
                yield f"\n\n**{speaker}:** {text}\n\n"
                turns.append({"speaker": speaker, "text": text})
            elif not yielded:
                yield f"\n\n*{speaker} returned an empty reply.*\n\n"
                break
            if yielded:
                if not text:
                    yield f"\n\n*{speaker} has nothing to add.*\n\n"
                break
            speaker = other
        await status("Done", True)
