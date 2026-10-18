"""
title: Vision Describe
author: Gordon
version: 0.1.0
description: A filter that lets a text-only model "see". Before a request reaches the model, each image in the conversation is sent to
    a small vision model on an Ollama server and replaced by a text description, so the main model (a roleplay or writing model, say)
    keeps its context for words and never needs vision support. Descriptions are cached so an image is only looked at once.
requirements: requests
"""

import asyncio
import base64
import hashlib
import inspect
import re
from collections import OrderedDict
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel, Field

DEFAULT_PROMPT = (
    "Describe this image for someone who cannot see it, in one or two short paragraphs. Say what is shown: the people (apparent age, "
    "hair, clothing, expression, pose), objects, setting, lighting, colours and style. Quote any visible text exactly. "
    "Be factual and specific; do not guess names and do not invent a story."
)

_cache: "OrderedDict[str, str]" = OrderedDict()  # description by hash of the image, model and prompt
_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.S)


class VisionError(RuntimeError):
    """The vision model could not describe an image; the message says why in a few words."""


def decode_data_url(url: str) -> Optional[bytes]:
    m = _DATA_URL_RE.match(url or "")
    if not m:
        return None
    try:
        return base64.b64decode(m.group(2), validate=False)
    except ValueError:
        return None


def to_jpeg_b64(data: bytes, max_side: int, quality: int) -> str:
    """The image shrunk so its long side is at most max_side pixels (this caps what it costs in tokens), as base64 JPEG. Without
    Pillow, or for something Pillow cannot read, the original bytes are sent as they are."""
    try:
        from PIL import Image  # shipped with Open WebUI

        with Image.open(BytesIO(data)) as im:
            im.load()
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, (255, 255, 255))
                flat.paste(im, mask=im.split()[-1])
                im = flat
            else:
                im = im.convert("RGB")
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.LANCZOS)
            out = BytesIO()
            im.save(out, format="JPEG", quality=quality)
            return base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 - any decoding trouble: let the vision model try the raw bytes
        return base64.b64encode(data).decode("ascii")


def tidy(text: str, limit: int) -> str:
    """The description without a thinking block or markdown fuss, cut at a sentence end if it runs over `limit` characters."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"[*#`]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit // 2 else cut.rstrip() + "...").strip()


def ask_ollama(base_url: str, model: str, jpeg_b64: str, prompt: str, num_ctx: int, keep_alive: str, timeout: int) -> str:
    """One vision call to Ollama's /api/chat. Blocking: run it in a thread."""
    body: Dict[str, Any] = {
        "model": model,
        "stream": False,
        "think": False,  # a thinking model would spend its answer on reasoning
        "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx, "temperature": 0.2},
        "messages": [{"role": "user", "content": prompt, "images": [jpeg_b64]}],
    }
    url = f"{base_url.rstrip('/')}/api/chat"
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        if resp.status_code == 400 and "think" in resp.text.lower():  # a model that does not know the option
            body.pop("think")
            resp = requests.post(url, json=body, timeout=timeout)
        if resp.status_code != 200:
            raise VisionError(f"the vision server answered HTTP {resp.status_code}: {resp.text[:150]}")
        content = ((resp.json() or {}).get("message") or {}).get("content") or ""
    except requests.Timeout:
        raise VisionError(f"the vision server did not answer within {timeout} seconds") from None
    except requests.RequestException as e:
        raise VisionError(f"the vision server could not be reached ({type(e).__name__})") from None
    except ValueError:
        raise VisionError("the vision server's reply could not be read") from None
    if not content.strip():
        raise VisionError("the vision model returned nothing")
    return content


async def fetch_open_webui_image(url: str, user: Optional[dict]) -> Optional[bytes]:
    """An image held by Open WebUI itself (a /api/v1/files/<id>/content path, a file id, or an http(s) URL it is allowed to fetch),
    read the way Open WebUI reads one for a vision model, so its ownership and address checks apply. None if it can't be read."""
    try:
        from open_webui.models.users import Users
        from open_webui.utils.files import get_image_base64_from_url

        owner = Users.get_user_by_id((user or {}).get("id"))
        if inspect.isawaitable(owner):
            owner = await owner
        data_url = await get_image_base64_from_url(url, user=owner)
    except Exception:  # noqa: BLE001 - outside Open WebUI (or an API change): nothing to fetch with
        return None
    return decode_data_url(data_url) if data_url else None


async def image_bytes(url: str, user: Optional[dict]) -> Optional[bytes]:
    return decode_data_url(url) or await fetch_open_webui_image(url, user)


async def describe(valves: Any, data: bytes, prompt: str) -> str:
    """The description of one image: from the cache, or from the vision model (shrunk first)."""
    key = hashlib.sha256(data + b"|" + valves.VISION_MODEL.encode() + b"|" + prompt.encode() + b"|" + str(valves.MAX_IMAGE_SIDE).encode()).hexdigest()
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    jpeg = await asyncio.to_thread(to_jpeg_b64, data, max(64, valves.MAX_IMAGE_SIDE), 85)
    raw = await asyncio.to_thread(ask_ollama, valves.OLLAMA_URL, valves.VISION_MODEL, jpeg, prompt, valves.NUM_CTX, valves.KEEP_ALIVE, valves.TIMEOUT_SECONDS)
    text = tidy(raw, valves.MAX_DESCRIPTION_CHARS)
    if not text:
        raise VisionError("the vision model returned nothing")
    _cache[key] = text
    while len(_cache) > max(1, valves.CACHE_SIZE):
        _cache.popitem(last=False)
    return text


class Filter:
    class Valves(BaseModel):
        OLLAMA_URL: str = Field(default="http://100.92.139.77:11434", description="Ollama server that runs the vision model, as Open WebUI's backend reaches it.")
        VISION_MODEL: str = Field(default="hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", description="Name of the vision model in Ollama, exactly as `ollama list` shows it.")
        PROMPT: str = Field(default=DEFAULT_PROMPT, description="What the vision model is asked about every image.")
        MAX_IMAGE_SIDE: int = Field(default=1024, description="Images are shrunk so their long side is at most this many pixels before they are sent. An image costs roughly one token per 28x28 pixels, so 1024 is about 1,000 tokens; larger costs more VRAM and time.")
        NUM_CTX: int = Field(default=4096, description="Context window requested from the vision model (kept small so it fits a small GPU). Must cover the image plus the description.")
        KEEP_ALIVE: str = Field(default="10m", description="How long Ollama keeps the vision model loaded after a call (e.g. 10m, 1h, 0 to unload at once).")
        TIMEOUT_SECONDS: int = Field(default=120, description="How long to wait for a description. The first call after the model was unloaded includes loading it.")
        MAX_DESCRIPTION_CHARS: int = Field(default=1200, description="A longer description is cut at a sentence end (about 4 characters per token).")
        MAX_IMAGES: int = Field(default=4, description="The most images described per request, newest first; older ones become a one-line note. Descriptions are cached, so this only matters the first time.")
        CACHE_SIZE: int = Field(default=64, description="How many descriptions to remember (an image shows up again in every later request of its chat).")
        IMAGE_TEMPLATE: str = Field(default="[Image: {description}]", description="What replaces an image in the message. {description} is the vision model's text.")
        ERROR_TEMPLATE: str = Field(default="[An image was attached, but it could not be viewed ({reason}). Say so rather than guessing what it shows.]", description="What replaces an image that could not be described. {reason} is a few words on why.")
        SKIPPED_TEMPLATE: str = Field(default="[An earlier image was shared in this conversation.]", description="What replaces images beyond MAX_IMAGES.")
        SHOW_STATUS: bool = Field(default=True, description="Show 'Looking at the image...' in Open WebUI's status line while it works.")

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None, __event_emitter__: Optional[Callable] = None) -> dict:
        v = self.valves
        messages = body.get("messages") or []
        refs: List[Tuple[int, int]] = [
            (mi, pi)
            for mi, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), list)
            for pi, part in enumerate(m["content"])
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        if not refs:
            return body

        async def status(text: str, done: bool) -> None:
            if v.SHOW_STATUS and __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done}})

        newest = set(refs[-max(0, v.MAX_IMAGES):]) if v.MAX_IMAGES > 0 else set()
        replacements: Dict[Tuple[int, int], str] = {}
        for ref in refs:
            if ref not in newest:
                replacements[ref] = v.SKIPPED_TEMPLATE
                continue
            part = messages[ref[0]]["content"][ref[1]]
            image = part.get("image_url")
            url = image.get("url", "") if isinstance(image, dict) else str(image or "")
            try:
                data = await image_bytes(url, __user__)
                if not data:
                    raise VisionError("the image could not be read")
                await status("Looking at the image...", False)
                replacements[ref] = v.IMAGE_TEMPLATE.replace("{description}", await describe(v, data, v.PROMPT))
            except VisionError as e:
                replacements[ref] = v.ERROR_TEMPLATE.replace("{reason}", str(e))
            except Exception as e:  # noqa: BLE001 - never let the filter stop the chat
                replacements[ref] = v.ERROR_TEMPLATE.replace("{reason}", f"{type(e).__name__}")
        await status("Image described", True)

        for mi in sorted({mi for mi, _ in refs}):
            parts = messages[mi]["content"]
            out: List[Any] = []
            for pi, part in enumerate(parts):
                text = replacements.get((mi, pi))
                out.append({"type": "text", "text": text} if text is not None else part)
            if all(isinstance(p, dict) and p.get("type") == "text" for p in out):
                messages[mi]["content"] = "\n\n".join(str(p.get("text", "")) for p in out if p.get("text"))
            else:
                messages[mi]["content"] = out
        return body
