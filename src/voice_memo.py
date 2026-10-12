"""
title: Voice Memo
author: Gordon
version: 0.1.0
description: Lets a model record a spoken voice memo. It sends the text to a text-to-speech model on
    OpenRouter (default fish-audio/s2.1-pro-free:free), keeps the MP3 on disk, uploads it to Open WebUI's
    file storage and returns a link the user clicks to listen. Unrelated to ComfyUI.
requirements: requests
"""

import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel, Field

# Same spellings every tool in this project treats as "not given".
_UNSET_STRINGS = {"", "none", "null", "default", "n/a", "undefined"}


def is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


class MemoError(RuntimeError):
    """A failed OpenRouter call, keeping the HTTP status so the error can say what to do."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-") or "memo"


def synthesize(valves: "Tools.Valves", text: str, voice: str) -> Tuple[bytes, str]:
    """Call OpenRouter's /audio/speech. Returns (mp3 bytes, content type)."""
    if not valves.OPENROUTER_API_KEY:
        raise MemoError("OPENROUTER_API_KEY is not set in the tool's valves.", 401)
    body = {
        "model": valves.TTS_MODEL,
        "input": text,
        "response_format": "mp3",
    }
    if voice:
        body["voice"] = voice
    try:
        r = requests.post(
            f"{valves.OPENROUTER_BASE_URL.rstrip('/')}/audio/speech",
            headers={"Authorization": f"Bearer {valves.OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=valves.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise MemoError(f"Could not reach OpenRouter: {e}", -1)
    if r.status_code >= 400:
        detail = ""
        try:
            detail = (r.json().get("error") or {}).get("message", "")
        except Exception:
            detail = (r.text or "")[:200]
        raise MemoError(f"OpenRouter HTTP {r.status_code}: {detail}", r.status_code)
    if not r.content:
        raise MemoError("OpenRouter returned an empty audio body.", 502)
    return r.content, r.headers.get("Content-Type", "audio/mpeg")


def upload_to_open_webui(base_url: str, api_key: str, data: bytes, filename: str, timeout: int) -> Tuple[Optional[str], Optional[str]]:
    """Store the MP3 in Open WebUI's file storage. Returns (relative_url, None) or (None, error_text)."""
    if not api_key:
        return None, "OPEN_WEBUI_API_KEY is empty in the tool's valves"
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/v1/files/",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, data, mimetypes.guess_type(filename)[0] or "audio/mpeg")},
            params={"process": "false"},  # nothing to extract or embed
            timeout=timeout,
        )
        resp.raise_for_status()
        file_id = resp.json().get("id")
        if not file_id:
            return None, f"upload response had no 'id' field: {str(resp.text)[:200]}"
        return f"/api/v1/files/{file_id}/content", None
    except requests.RequestException as e:
        response = getattr(e, "response", None)
        detail = f" (HTTP {response.status_code}: {str(response.text)[:200]})" if response is not None else ""
        return None, f"{e}{detail}"
    except ValueError as e:
        return None, f"unexpected response from Open WebUI: {e}"


async def _emit(emitter: Optional[Callable], payload: Dict[str, Any]) -> None:
    if emitter:
        await emitter(payload)


class Tools:
    class Valves(BaseModel):
        OPENROUTER_API_KEY: str = Field(default="", description="OpenRouter API key (https://openrouter.ai/settings/keys).")
        OPENROUTER_BASE_URL: str = Field(default="https://openrouter.ai/api/v1", description="OpenRouter API base URL.")
        TTS_MODEL: str = Field(
            default="fish-audio/s2.1-pro-free:free",
            description="OpenRouter's own model id, with no Open WebUI connection prefix such as 'or.'.",
        )
        DEFAULT_VOICE: str = Field(
            default="b347db033a6549378b48d00acb0d06cd",
            description="Fish Audio voice id (reference_id from fish.audio's voice library) used when the model passes none.",
        )
        MEMO_DIR: str = Field(
            default="/app/backend/data/voice_memos",
            description="Folder (inside the Open WebUI container) where each memo's MP3 and a small .json sidecar are kept.",
        )
        MAX_CHARS: int = Field(default=3000, description="Longest memo text accepted, in characters.")
        UPLOAD_TO_OPEN_WEBUI: bool = Field(
            default=True,
            description="Upload each memo to Open WebUI's file storage so the user can play it from the chat. Needs OPEN_WEBUI_API_KEY.",
        )
        OPEN_WEBUI_BASE_URL: str = Field(
            default="http://localhost:8080",
            description="Address the tool uses to call Open WebUI's own API, from the backend's point of view.",
        )
        OPEN_WEBUI_API_KEY: str = Field(
            default="",
            description="Personal API key from Open WebUI (Settings -> Account -> API Keys). Set it here in the valve, not in the code.",
        )
        PUBLIC_BASE_URL: str = Field(
            default="https://open-webui.dimension-x.net",
            description="Address the user's browser uses for Open WebUI. Used to build a full link that also works in an email (the user must be logged in to Open WebUI to open it). Empty = only the in-chat relative link.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(default=90, description="Per-HTTP-request timeout.")

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _fail(e: Exception) -> str:
        """Turn an exception into an error message that tells the model what to do next."""
        text = f"Error: {e}"
        status = getattr(e, "status", 0)
        if status in (401, 403):
            hint = "OpenRouter rejected the API key or the spending limit is reached. This is a configuration problem. Tell the user; retrying will not help."
        elif status == 402:
            hint = "The OpenRouter account is out of credits. Tell the user; retrying will not help."
        elif status in (400, 404):
            hint = (
                "OpenRouter did not accept the request. If you passed a voice, retry once without it "
                "(pass an empty string for voice). If it fails again, tell the user the model or voice setting needs checking."
            )
        elif status == 429 or status >= 500 or status == -1:
            hint = "A temporary problem on the OpenRouter side. Retry once; if it fails again, tell the user."
        else:
            return text
        return f"{text}\nWhat to do: {hint}"

    def _memo_dir(self) -> Path:
        d = Path(self.valves.MEMO_DIR)
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def record_voice_memo(
        self,
        text: str,
        title: str = "",
        voice: str = "",
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> str:
        """
        Record a spoken voice memo from text and give the user a link to listen to it.

        :param text: The exact words to speak, written the way they should be said aloud (plain sentences, no Markdown).
        :param title: Optional short title, used in the file name and the link. Omit for an automatic one.
        :param voice: Optional Fish Audio voice id. Omit to use the default voice.
        :return: A message containing a Markdown link; put that link in your reply so the user can play the memo.
        """
        v = self.valves
        text = (text or "").strip()
        if not text:
            return "Error: text is empty.\nWhat to do: call again with the words the memo should say."
        if len(text) > v.MAX_CHARS:
            return (
                f"Error: the memo is {len(text)} characters; the limit is {v.MAX_CHARS}.\n"
                "What to do: shorten the text, or split it into several memos with separate calls."
            )
        title = "" if is_unset(title) else title.strip()
        voice = v.DEFAULT_VOICE if is_unset(voice) else voice.strip()

        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Recording voice memo...", "done": False}})
        try:
            audio, _ctype = synthesize(v, text, voice)
        except MemoError as e:
            await _emit(__event_emitter__, {"type": "status", "data": {"description": "Voice memo failed", "done": True}})
            return self._fail(e)

        stamp = datetime.now().astimezone()
        name = f"{stamp:%Y%m%d-%H%M%S}-{slugify(title or text)}.mp3"
        saved = ""
        try:
            path = self._memo_dir() / name
            path.write_bytes(audio)
            saved = str(path)
        except OSError as e:
            saved = f"(not saved to disk: {e})"

        url, upload_err = (None, None)
        if v.UPLOAD_TO_OPEN_WEBUI:
            url, upload_err = upload_to_open_webui(v.OPEN_WEBUI_BASE_URL, v.OPEN_WEBUI_API_KEY, audio, name, v.REQUEST_TIMEOUT_SECONDS)

        if saved and not saved.startswith("("):
            try:
                (self._memo_dir() / (name[:-4] + ".json")).write_text(
                    json.dumps(
                        {
                            "title": title or text[:60],
                            "created": stamp.isoformat(timespec="seconds"),
                            "voice": voice,
                            "model": v.TTS_MODEL,
                            "chars": len(text),
                            "text": text,
                            "url": url,
                        },
                        indent=1,
                    )
                )
            except OSError:
                pass

        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Voice memo recorded", "done": True}})
        label = title or "voice memo"
        if url:
            full = f"{v.PUBLIC_BASE_URL.rstrip('/')}{url}" if v.PUBLIC_BASE_URL.strip() else ""
            email = (
                f"\nFull link for an email (opens only when the user is logged in to Open WebUI): {full}" if full else ""
            )
            return (
                f"Recorded {name} ({len(audio)} bytes). Put this link in your reply so the user can play it:\n"
                f"[Play {label}]({url}){email}\nSaved on the server as: {saved}"
            )
        why = upload_err or "uploading to Open WebUI is turned off"
        return (
            f"Recorded {name} ({len(audio)} bytes) and saved on the server as: {saved}\n"
            f"No playable link could be made ({why}). Tell the user the memo exists at that path and that the tool's "
            "OPEN_WEBUI_API_KEY valve needs checking."
        )

    async def list_voice_memos(self, limit: str = "10") -> str:
        """
        List the most recent voice memos, newest first, with their titles, dates and play links.

        :param limit: How many memos to show (a number, default 10).
        :return: A Markdown list.
        """
        try:
            n = max(1, min(int(str(limit).strip() or 10), 50)) if not is_unset(limit) else 10
        except ValueError:
            n = 10
        d = Path(self.valves.MEMO_DIR)
        sidecars: List[Path] = sorted(d.glob("*.json"), reverse=True)[:n] if d.is_dir() else []
        if not sidecars:
            return "No voice memos recorded yet."
        lines = []
        for p in sidecars:
            try:
                m = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            base = self.valves.PUBLIC_BASE_URL.strip().rstrip("/")
            link = f"[play]({m['url']}) full link: {base}{m['url']}" if m.get("url") and base else f"[play]({m['url']})" if m.get("url") else "(no link)"
            lines.append(f"- {m.get('created', '?')} - {m.get('title', p.stem)} ({m.get('chars', '?')} chars) {link}")
        return "\n".join(lines) or "No voice memos recorded yet."
