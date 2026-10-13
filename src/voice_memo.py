"""
title: Voice Memo
author: Gordon
version: 0.1.0
description: Lets a model record a spoken voice memo. It sends the text to a text-to-speech model on
    OpenRouter (default fish-audio/s2.1-pro-free:free), keeps the MP3 on disk, uploads it to Open WebUI's
    file storage and returns a link the user clicks to listen. Unrelated to ComfyUI.
requirements: requests
"""

import asyncio
import difflib
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


_VOICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


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


# Matches an expression tag such as [happy] or [soft tone]. Used to spot text that is already tagged
# and to take tags out before checking that the formatter left the words alone.
_TAG_RE = re.compile(r"\[[^\]\n]{2,40}\]")

DEFAULT_FORMATTER_PROMPT = """You prepare text to be spoken aloud by a Fish Audio S2 voice. Add expression tags and return the text.

RULES
- Keep every word, in the same order. Do not add, remove, reword or reorder words. Only add tags.
- Tags go in [square brackets]. The voice performs a tag; it never says it.
- A mood tag goes at the START of a sentence. Use at most one main mood per sentence; change it only when the feeling changes.
- A tone or sound tag can go anywhere, next to what it affects. After a sound tag, keep any written sound already there (for example "ha ha").
- Use few tags. At most one tag per two sentences, and none at all for plain or neutral sentences such as reminders or facts. Many tags sound unnatural.
- Pick a mood only when the words clearly show it. Never invent a feeling the text does not express (a reminder is not angry).
- If the text contains *asterisk actions* or (stage directions), REPLACE them with a bracket tag in the same place. The asterisks, parentheses and the words inside them must not appear in your reply.
- Remove emoji and Markdown marks. Do not add anything else.
- Reply with the tagged text only. No quotes, no explanation, no code block.

TAGS
Moods (start of a sentence): happy, sad, angry, excited, calm, nervous, confident, surprised, satisfied, delighted, scared, worried, upset, frustrated, depressed, empathetic, embarrassed, disgusted, moved, proud, relaxed, grateful, curious, sarcastic, disdainful, unhappy, anxious, hysterical, indifferent, uncertain, doubtful, confused, disappointed, regretful, guilty, ashamed, jealous, envious, hopeful, optimistic, pessimistic, nostalgic, lonely, bored, contemptuous, sympathetic, compassionate, determined, resigned
Tone (anywhere): in a hurry tone, shouting, screaming, whispering, soft tone, emphasis (put [emphasis] just before the word to stress)
Sounds (anywhere): laughing, chuckling, sobbing, crying loudly, sighing, groaning, panting, gasping, yawning, snoring, clear throat
Pauses and ambience (anywhere): break, long-break, audience laughing, background laughter, crowd laughing
Any short plain description also works, such as [slightly sad] or [very excited].

EXAMPLES
Input: Hey, it's me. The weekend plans came together after all. We got the table by the window! Ha ha, I told you it would work.
Output: [happy] Hey, it's me. The weekend plans came together after all. [excited] We got the table by the window! [laughing] Ha ha, I told you it would work.

Input: I miss the old place. I really do. I have some news though. I think it might be good.
Output: [sad][whispering] I miss the old place. [sighing] I really do. [nervous] I have some news though. [break] [hopeful] I think it might be good.
"""


_STAGE_RE = re.compile(r"\*[^*\n]{1,60}\*|\([^)\n]{1,60}\)")


def _words(text: str) -> List[str]:
    """Spoken words only: tags, *asterisk actions* and (stage directions) are not counted, because the formatter turns them into tags."""
    return re.findall(r"[a-z0-9']+", _STAGE_RE.sub(" ", _TAG_RE.sub(" ", text)).lower())


def _clean_formatter_output(out: str) -> str:
    out = (out or "").strip()
    out = re.sub(r"^```[a-z]*\n?|\n?```$", "", out).strip()
    # The voice would read these aloud, so drop any the formatter left behind.
    out = re.sub(r" {2,}", " ", _STAGE_RE.sub(" ", out)).replace("*", "").strip()
    if len(out) > 1 and out[0] == out[-1] and out[0] in "\"'":
        out = out[1:-1].strip()
    return out


def add_expression_tags(valves: "Tools.Valves", text: str) -> Tuple[str, str]:
    """Ask the formatter model to add expression tags. Returns (text to speak, reason it was left alone or "").

    Never raises: any problem hands back the original text, so a memo is not lost because of the formatter.
    """
    model = (valves.FORMATTER_MODEL or "").strip()
    if not model or not valves.OPENROUTER_API_KEY:
        return text, "formatter off"
    if _TAG_RE.search(text):
        return text, "already tagged"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": (valves.FORMATTER_PROMPT or "").strip() or DEFAULT_FORMATTER_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.3,
        "max_tokens": len(text) // 2 + 300,
    }
    try:
        r = requests.post(
            f"{valves.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {valves.OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=valves.FORMATTER_TIMEOUT_SECONDS,
        )
        if r.status_code >= 400:
            return text, f"formatter HTTP {r.status_code}"
        out = _clean_formatter_output(r.json()["choices"][0]["message"]["content"])
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
        return text, f"formatter failed: {e}"
    if not out:
        return text, "formatter returned nothing"
    if len(out) > len(text) * 1.6 + 80:
        return text, "formatter output too long"
    before, after = _words(text), _words(out)
    if before and difflib.SequenceMatcher(None, before, after, autojunk=False).ratio() < valves.FORMATTER_MIN_WORD_MATCH:
        return text, "formatter changed the words"
    return out, ""


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
        FORMATTER_MODEL: str = Field(
            default="mistralai/mistral-small-24b-instruct-2501",
            description="Small OpenRouter chat model that adds expression tags like [happy] or [whispering] to the memo text before it is spoken. Empty = off (the text is spoken as given). Skipped when the text already has tags.",
        )
        FORMATTER_PROMPT: str = Field(
            default="",
            description="System prompt for the formatter model. Empty = the built-in prompt with the Fish Audio tag guide.",
        )
        FORMATTER_MIN_WORD_MATCH: float = Field(
            default=0.9,
            description="0 to 1. How closely the formatted text's words must match the original (tags ignored). If the formatter rewrites more than this allows, the original text is spoken instead. 1 = no word changes at all.",
        )
        FORMATTER_TIMEOUT_SECONDS: int = Field(default=30, description="Timeout for the formatter call. On timeout the original text is spoken.")
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
        ATTACH_TO_CHAT: bool = Field(
            default=True,
            description="Also attach each uploaded memo to the assistant's message, so it is listed in the chat's Files tab. Needs the upload to have worked.",
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
        elif status == 429 and re.search(r"per-day|daily|free-models", str(e), re.I):
            hint = (
                "OpenRouter's daily limit for free models has been reached. It resets by itself in a few hours. "
                "Do not retry; tell the user the memo could not be recorded until the limit resets."
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

    async def voice_memo_help(self) -> str:
        """
        Explain how to use the voice memo tools. Call it when you are unsure how to record a memo or what to do with the result.

        :return: A short guide.
        """
        v = self.valves
        return (
            "Voice memo tools\n"
            f"- record_voice_memo(text, title, voice): speaks `text` aloud and saves it as an MP3. `text` is the exact words, "
            f"plain sentences with no Markdown, at most {v.MAX_CHARS} characters (split longer memos into several calls). "
            "Expression goes in square brackets and is performed, not spoken. Mood tags work best at the start of a sentence: [happy] [sad] [angry] [excited] [calm] [nervous]. Tone and sound tags can go anywhere: [whispering] [shouting] [soft tone] [laughing] [sighing] [break]. Any short description also works, e.g. [slightly sad]. Never use *asterisks* or (stage directions): they are read aloud. `title` is optional; pass \"\" for an automatic one. `voice` is optional; pass \"\" (the normal choice). Never invent a voice name; only a 32-character hex id from the user is valid.\n"
            "- Expression tags are optional. If you write none, the tool adds suitable ones itself; do not add them just to be safe.\n"
            "- The result contains a Markdown link: copy it into your reply, exactly as given, so the user can play the memo.\n"
            "- If the user wants it by email, use the 'Full link' line from the result in the email body. It opens only "
            "while the user is logged in to Open WebUI.\n"
            "- list_voice_memos(limit): shows recent memos with their links, newest first.\n"
            "- If recording fails, the error says what to do. Do not retry more than once; tell the user instead."
        )

    async def record_voice_memo(
        self,
        text: str,
        title: str = "",
        voice: str = "",
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> str:
        """
        Record a spoken voice memo from text and give the user a link to listen to it.

        :param text: The exact words to speak, written the way they should be said aloud (plain sentences, no Markdown). For expression use square-bracket tags: a mood at the start of a sentence ([happy], [sad], [angry], [excited], [calm], [nervous]) or a sound anywhere ([whispering], [laughing], [sighing]); never *asterisks* or (stage directions), which are read aloud.
        :param title: Optional short title, used in the file name and the link. Omit for an automatic one.
        :param voice: Almost always omit this (pass ""). Only a 32-character hex Fish Audio voice id the user gave you is valid; never invent a voice name, it is ignored.
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
        voice_note = ""
        voice = "" if is_unset(voice) else voice.strip()
        if voice and not _VOICE_ID_RE.match(voice.lower()):
            voice_note = f"\nNote: voice {voice!r} is not a valid voice id (32 hex characters), so the default voice was used."
            voice = ""
        voice = (voice.lower() or v.DEFAULT_VOICE)

        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Recording voice memo...", "done": False}})
        spoken, _why = await asyncio.to_thread(add_expression_tags, v, text)
        try:
            audio, _ctype = await asyncio.to_thread(synthesize, v, spoken, voice)
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
            # Must run off the event loop: this posts to Open WebUI itself, which would deadlock a blocked loop.
            url, upload_err = await asyncio.to_thread(
                upload_to_open_webui, v.OPEN_WEBUI_BASE_URL, v.OPEN_WEBUI_API_KEY, audio, name, v.REQUEST_TIMEOUT_SECONDS
            )

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
                            **({"spoken_text": spoken} if spoken != text else {}),
                            "url": url,
                        },
                        indent=1,
                    )
                )
            except OSError:
                pass

        if url and v.ATTACH_TO_CHAT:
            file_id = url.split("/api/v1/files/")[1].split("/")[0]
            await _emit(
                __event_emitter__,
                {
                    "type": "files",
                    "data": {
                        "files": [
                            {"type": "file", "id": file_id, "url": url, "name": name, "size": len(audio), "status": "uploaded"}
                        ]
                    },
                },
            )
        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Voice memo recorded", "done": True}})
        label = title or "voice memo"
        if url:
            full = f"{v.PUBLIC_BASE_URL.rstrip('/')}{url}" if v.PUBLIC_BASE_URL.strip() else ""
            email = (
                f"\nFull link for an email (opens only when the user is logged in to Open WebUI): {full}" if full else ""
            )
            return (
                f"Recorded {name} ({len(audio)} bytes). Put this link in your reply so the user can play it:\n"
                f"[Play {label}]({url}){email}\nSaved on the server as: {saved}{voice_note}"
            )
        why = upload_err or "uploading to Open WebUI is turned off"
        return (
            f"Recorded {name} ({len(audio)} bytes) and saved on the server as: {saved}{voice_note}\n"
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
