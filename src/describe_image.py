"""
title: Describe Image
author: Gordon
version: 0.1.0
description: Lets a model look at an image it was given a link to (a picture it generated, a file in Open WebUI, a ComfyUI result, a web
    link) by sending it to a small vision model on an Ollama server and returning the description, optionally answering a specific
    question about it. For pictures a user attaches to a message, use the Vision Describe filter instead.
requirements: requests
"""

import asyncio
import base64
import hashlib
import inspect
import re
import urllib.parse
from collections import OrderedDict
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel, Field

DEFAULT_PROMPT = (
    "Describe this image for someone who cannot see it, in one or two short paragraphs. Say what is shown: the people (apparent age, "
    "hair, clothing, expression, pose), objects, setting, lighting, colours and style. Quote any visible text exactly. "
    "Be factual and specific; do not guess names and do not invent a story."
)
QUESTION_PROMPT = (
    "Look carefully at this image and answer this question about it: {question}\n"
    "Answer in a few sentences, only from what is visible; if the image does not show it, say so."
)

_cache: "OrderedDict[str, str]" = OrderedDict()
_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.S)
_URL_RE = re.compile(r"(https?://[^\s)\]>\"']+|/api/v1/files/[^\s)\]>\"']+)")
_COMFY_NAME_RE = re.compile(r"^(?:[\w .-]+/)?[\w .-]+\.(?:png|jpg|jpeg|webp)$", re.I)


class VisionError(RuntimeError):
    """The image could not be fetched or described; the message says why in a few words."""


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
    except Exception:  # noqa: BLE001
        return base64.b64encode(data).decode("ascii")


def tidy(text: str, limit: int) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"[*#`]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit // 2 else cut.rstrip() + "...").strip()


def ask_ollama(base_url: str, model: str, jpeg_b64: str, prompt: str, num_ctx: int, keep_alive: str, timeout: int) -> str:
    body: Dict[str, Any] = {
        "model": model,
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx, "temperature": 0.2},
        "messages": [{"role": "user", "content": prompt, "images": [jpeg_b64]}],
    }
    url = f"{base_url.rstrip('/')}/api/chat"
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        if resp.status_code == 400 and "think" in resp.text.lower():
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
    """An image Open WebUI can read for this user (a /api/v1/files/<id>/content path, a file id, or an http(s) URL it is allowed
    to fetch), read the way Open WebUI reads one for a vision model. None if it can't be read."""
    try:
        from open_webui.models.users import Users
        from open_webui.utils.files import get_image_base64_from_url

        owner = Users.get_user_by_id((user or {}).get("id"))
        if inspect.isawaitable(owner):
            owner = await owner
        data_url = await get_image_base64_from_url(url, user=owner)
    except Exception:  # noqa: BLE001
        return None
    return decode_data_url(data_url) if data_url else None


def fetch_comfy_image(servers: List[str], reference: str, timeout: int) -> Optional[bytes]:
    """A ComfyUI result by file name (as a result's use_as_source_image gives it), from the first listed server that has it."""
    subfolder, _, filename = reference.strip().replace("\\", "/").rpartition("/")
    query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "output"})
    for server in servers:
        try:
            r = requests.get(f"{server.rstrip('/')}/view?{query}", timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content
        except requests.RequestException:
            continue
    return None


def pick_reference(image: Any) -> str:
    """The link or file name inside whatever the model passed: a bare link, a markdown image, a sentence containing one."""
    text = str(image or "").strip()
    m = _URL_RE.search(text)
    return m.group(1) if m else text.strip("`'\" ")


def latest_message_image(messages: Any) -> Optional[str]:
    """The newest image link in the conversation's messages, if a message still carries one."""
    for message in reversed(messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image = part.get("image_url")
                    return image.get("url") if isinstance(image, dict) else str(image or "")
    return None


class Tools:
    class Valves(BaseModel):
        OLLAMA_URL: str = Field(default="http://100.92.139.77:11434", description="Ollama server that runs the vision model, as Open WebUI's backend reaches it.")
        VISION_MODEL: str = Field(default="hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", description="Name of the vision model in Ollama, exactly as `ollama list` shows it.")
        PROMPT: str = Field(default=DEFAULT_PROMPT, description="What the vision model is asked when the model gives no question.")
        COMFY_SERVERS: List[str] = Field(default=["http://192.168.10.11:8188", "http://192.168.10.12:8188"], description="ComfyUI servers to look on when the model gives a result's file name (like sdxl_simple_00012_.png) instead of a link.")
        MAX_IMAGE_SIDE: int = Field(default=1024, description="Images are shrunk so their long side is at most this many pixels before they are sent (about one token per 28x28 pixels).")
        NUM_CTX: int = Field(default=4096, description="Context window requested from the vision model; must cover the image plus the answer.")
        KEEP_ALIVE: str = Field(default="10m", description="How long Ollama keeps the vision model loaded after a call (e.g. 10m, 1h).")
        TIMEOUT_SECONDS: int = Field(default=120, description="How long to wait for the vision model, including loading it.")
        MAX_DESCRIPTION_CHARS: int = Field(default=1200, description="A longer answer is cut at a sentence end.")
        CACHE_SIZE: int = Field(default=64, description="How many answers to remember, so asking again about the same image is instant.")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def describe_image(
        self, image: Optional[str] = None, question: Optional[str] = None, __user__: Optional[dict] = None, __messages__: Optional[list] = None,
    ) -> str:
        """
        Look at an image and say what is in it. Use it for a picture you only have a link or file name for: one you or a tool generated,
        or one in the conversation as a link. Optionally ask something specific about it (what is she wearing, what does the sign say).

        :param image: The picture's link (for example the chat_url or image link a tool returned, a /api/v1/files/... path, or a web link) or a ComfyUI result's file name such as sdxl_simple_00012_.png. Pass "" (or omit) to look at the newest image in the conversation.
        :param question: Optional: something specific to find out about the image. Pass "" (or omit) for a general description.
        """
        v = self.valves
        try:
            reference = pick_reference(image) if str(image or "").strip() and str(image).strip().lower() not in ("none", "null", "latest") else (latest_message_image(__messages__) or "")
            if not reference:
                raise VisionError("no image to look at: pass the picture's link or file name")
            data = decode_data_url(reference)
            source = "a data link" if data else reference[:80]
            if data is None and _COMFY_NAME_RE.match(reference) and "/api/" not in reference and "://" not in reference:
                data = await asyncio.to_thread(fetch_comfy_image, v.COMFY_SERVERS, reference, 30)
                if data is None:
                    raise VisionError(f"{reference!r} was not found on the ComfyUI servers; pass the full link instead")
            if data is None:
                data = await fetch_open_webui_image(reference, __user__)
                if data is None:
                    raise VisionError(f"the image at {reference[:80]!r} could not be fetched")
            wanted = str(question or "").strip()
            prompt = QUESTION_PROMPT.replace("{question}", wanted) if wanted and wanted.lower() not in ("none", "null") else v.PROMPT
            key = hashlib.sha256(data + b"|" + v.VISION_MODEL.encode() + b"|" + prompt.encode() + b"|" + str(v.MAX_IMAGE_SIDE).encode()).hexdigest()
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key]
            jpeg = await asyncio.to_thread(to_jpeg_b64, data, max(64, v.MAX_IMAGE_SIDE), 85)
            raw = await asyncio.to_thread(ask_ollama, v.OLLAMA_URL, v.VISION_MODEL, jpeg, prompt, v.NUM_CTX, v.KEEP_ALIVE, v.TIMEOUT_SECONDS)
            text = tidy(raw, v.MAX_DESCRIPTION_CHARS)
            if not text:
                raise VisionError("the vision model returned nothing")
            _cache[key] = text
            while len(_cache) > max(1, v.CACHE_SIZE):
                _cache.popitem(last=False)
            return text
        except VisionError as e:
            return f"Could not look at the image: {e}."
