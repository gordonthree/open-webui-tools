"""
title: OpenRouter Image
author: Gordon
version: 0.1.0
description: Generate an image with Meta's Muse Image model (meta/muse-image) through OpenRouter's images API.
    A paid, hosted alternative to the ComfyUI generate_image tool: the model writes one plain-language prompt
    and gets back one image, shown in the chat. Unrelated to ComfyUI.
requirements: requests
"""

import asyncio
import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import requests
from pydantic import BaseModel, Field

# Same spellings every tool in this project treats as "not given".
_UNSET_STRINGS = {"", "none", "null", "default", "n/a", "undefined"}


def is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


class ImageError(RuntimeError):
    """A failed OpenRouter call, keeping the HTTP status so errors can be explained."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-") or "image"


def _error_text(r: Any) -> str:
    """OpenRouter's error message from a failed response (its two error shapes), else the raw text."""
    try:
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    except Exception:
        pass
    return (getattr(r, "text", "") or "")[:300]


def request_images(valves: "Tools.Valves", prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    """POST to OpenRouter's /images. Returns the parsed JSON body."""
    if not valves.OPENROUTER_API_KEY:
        raise ImageError("OPENROUTER_API_KEY is not set in the tool's valves.", 401)
    body = {**extra, "model": valves.IMAGE_MODEL, "prompt": prompt}
    try:
        r = requests.post(
            f"{valves.OPENROUTER_BASE_URL.rstrip('/')}/images",
            headers={"Authorization": f"Bearer {valves.OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=valves.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise ImageError(f"Could not reach OpenRouter: {e}", -1)
    if r.status_code >= 400:
        raise ImageError(f"OpenRouter HTTP {r.status_code}: {_error_text(r)}", r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise ImageError(f"OpenRouter returned something that is not JSON: {(r.text or '')[:200]}", 502)
    if not isinstance(data, dict):
        raise ImageError("OpenRouter returned an unexpected response shape.", 502)
    return data


def extract_images(data: Dict[str, Any], timeout: int) -> List[bytes]:
    """Image bytes from a response's data[] items: b64_json, or a url that is downloaded."""
    out: List[bytes] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            try:
                out.append(base64.b64decode(item["b64_json"]))
            except Exception:
                raise ImageError("OpenRouter returned image data that could not be decoded.", 502)
        elif item.get("url"):
            try:
                r = requests.get(item["url"], timeout=timeout)
                r.raise_for_status()
                out.append(r.content)
            except requests.RequestException as e:
                raise ImageError(f"Could not download the generated image: {e}", 502)
    return [b for b in out if b]


def image_ext(blob: bytes) -> Tuple[str, str]:
    """(extension, mime type) from the image's first bytes; PNG when unsure."""
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "png", "image/png"


def upload_to_open_webui(base_url: str, api_key: str, data: bytes, filename: str, mime: str, timeout: int) -> Tuple[Optional[str], Optional[str]]:
    """Store the image in Open WebUI's file storage. Returns (relative_url, None) or (None, error_text)."""
    if not api_key:
        return None, "OPEN_WEBUI_API_KEY is empty in the tool's valves"
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/v1/files/",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, data, mime)},
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
        OPENROUTER_API_KEY: str = Field(default="", description="OpenRouter API key (https://openrouter.ai/settings/keys). This model is paid.")
        OPENROUTER_BASE_URL: str = Field(default="https://openrouter.ai/api/v1", description="OpenRouter API base URL.")
        IMAGE_MODEL: str = Field(
            default="meta/muse-image",
            description="OpenRouter's own model id, with no Open WebUI connection prefix such as 'or.'.",
        )
        REQUEST_EXTRA: str = Field(
            default="",
            description='Optional JSON object merged into every request body, for options OpenRouter accepts but this tool does not know about, e.g. {"aspect_ratio": "16:9"}. "model" and "prompt" cannot be overridden. Empty = nothing extra.',
        )
        OUTPUT_DIR: str = Field(
            default="/app/backend/data/openrouter_images",
            description="Folder (inside the Open WebUI container) where each image and a small .json sidecar (prompt, model, cost) are kept.",
        )
        SAVE_LOCAL_COPY: bool = Field(default=True, description="Keep each image and its sidecar in OUTPUT_DIR.")
        MAX_PROMPT_CHARS: int = Field(default=4000, description="Longest prompt accepted, in characters.")
        UPLOAD_TO_OPEN_WEBUI: bool = Field(
            default=True,
            description="Upload each image into Open WebUI's own file storage and show it from there. Needs OPEN_WEBUI_API_KEY.",
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
            description="Address the user's browser uses for Open WebUI. Used to build a full link that also works in an email (the user must be logged in to Open WebUI to open it). Empty = no full link.",
        )
        IMAGE_DELIVERY: Literal["model_embed", "files_event", "message_event"] = Field(
            default="model_embed",
            description=(
                "How the finished image reaches the chat. "
                "'model_embed': the result includes image_markdown and the model is told to put it in its reply. "
                "'files_event': the tool attaches the image to the message via an event; the model is told not to embed it. "
                "'message_event': the tool appends markdown to the message text via an event; this text can be overwritten when the model streams its reply."
            ),
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=300, description="Per-HTTP-request timeout. Muse Image reasons before it renders, so a call can take a while."
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    @staticmethod
    def _fail(e: Exception) -> str:
        """Turn an exception into an error message that tells the model what to do next."""
        text = f"Error: {e}"
        status = getattr(e, "status", 0)
        if status in (401, 403):
            hint = "OpenRouter rejected the API key or the spending limit is reached. This is a configuration problem. Tell the user; retrying will not help."
        elif status == 402:
            hint = "The OpenRouter account is out of credits. Tell the user; retrying will not help."
        elif status == 400:
            hint = (
                "OpenRouter did not accept the prompt. It may have been blocked by the model's content rules, or the "
                "request is malformed. Retry once with a rewritten, plainer prompt; if it fails again, tell the user."
            )
        elif status == 404:
            hint = "OpenRouter does not know this image model. This is a configuration problem. Tell the user; retrying will not help."
        elif status == 429 and re.search(r"per-day|daily|free-models", str(e), re.I):
            hint = "OpenRouter's daily limit has been reached. Do not retry; tell the user it resets by itself in a few hours."
        elif status == 429 or status >= 500 or status == -1:
            hint = "A temporary problem on the OpenRouter side. Retry once; if it fails again, tell the user."
        else:
            return text
        return f"{text}\nWhat to do: {hint}"

    def _extra(self) -> Dict[str, Any]:
        raw = (self.valves.REQUEST_EXTRA or "").strip()
        if not raw:
            return {}
        try:
            extra = json.loads(raw)
        except ValueError as e:
            raise ImageError(f"The tool's REQUEST_EXTRA valve is not valid JSON ({e}). Tell the user; retrying will not help.", 0)
        if not isinstance(extra, dict):
            raise ImageError("The tool's REQUEST_EXTRA valve must be a JSON object. Tell the user; retrying will not help.", 0)
        extra.pop("model", None)
        extra.pop("prompt", None)
        return extra

    async def generate_muse_image(
        self,
        prompt: str,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Generate one image with Meta's Muse Image model on OpenRouter (a paid, hosted model that reasons
        about the prompt before drawing). Use it when the user asks for a picture and wants this model; use
        generate_image instead for ComfyUI/SDXL renders with seeds, LoRAs and negative prompts. This tool
        has no size, seed or negative-prompt options: describe everything in the one prompt, in plain sentences.
        A call can take a minute or more, and every call costs money, so do not repeat it needlessly.

        :param prompt: A plain-language description of the image: subject, setting, style, lighting, mood, and any text that must appear in it.
        :return: A result with image_markdown (or a note that the image is already shown). Follow its note.
        """
        v = self.valves
        prompt = (prompt or "").strip()
        if not prompt:
            return {"success": False, "error": "prompt is empty. Call again with a description of the image."}
        if len(prompt) > v.MAX_PROMPT_CHARS:
            return {
                "success": False,
                "error": f"The prompt is {len(prompt)} characters; the limit is {v.MAX_PROMPT_CHARS}. Shorten it and call again.",
            }

        async def status(text: str, done: bool = False):
            await _emit(__event_emitter__, {"type": "status", "data": {"description": text, "done": done}})

        await status("Generating image...")
        try:
            extra = self._extra()
            data = await asyncio.to_thread(request_images, v, prompt, extra)
            blobs = await asyncio.to_thread(extract_images, data, v.REQUEST_TIMEOUT_SECONDS)
            if not blobs:
                raise ImageError("OpenRouter returned no image (the response had no usable data).", 502)
        except ImageError as e:
            await status("Image generation failed", done=True)
            return {"success": False, "error": self._fail(e)}
        except Exception as e:  # keep the model from seeing a raw traceback
            await status("Image generation failed", done=True)
            return {"success": False, "error": f"Unexpected error: {e}"}

        stamp = datetime.now().astimezone()
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = usage.get("cost")
        images_out: List[Dict[str, Any]] = []
        chat_sources: List[str] = []
        warnings: List[str] = []
        for i, blob in enumerate(blobs, start=1):
            ext, mime = image_ext(blob)
            name = f"{stamp:%Y%m%d-%H%M%S}-{slugify(prompt)}{'' if len(blobs) == 1 else f'-{i}'}.{ext}"
            entry: Dict[str, Any] = {"filename": name, "bytes": len(blob)}

            saved = None
            if v.SAVE_LOCAL_COPY:
                try:
                    d = Path(v.OUTPUT_DIR)
                    d.mkdir(parents=True, exist_ok=True)
                    (d / name).write_bytes(blob)
                    saved = str(d / name)
                    entry["saved_as"] = saved
                except OSError as e:
                    warnings.append(f"Could not keep a copy on the server ({e}).")

            url = None
            if v.UPLOAD_TO_OPEN_WEBUI:
                url, err = await asyncio.to_thread(
                    upload_to_open_webui, v.OPEN_WEBUI_BASE_URL, v.OPEN_WEBUI_API_KEY, blob, name, mime, v.REQUEST_TIMEOUT_SECONDS
                )
                if err:
                    warnings.append(f"Could not upload the image to Open WebUI ({err}).")
            if url:
                chat_sources.append(url)
                entry["chat_url"] = url
                base = v.PUBLIC_BASE_URL.strip().rstrip("/")
                if base:
                    entry["full_link"] = f"{base}{url}"

            if saved:
                try:
                    Path(saved).with_suffix(".json").write_text(
                        json.dumps(
                            {
                                "created": stamp.isoformat(timespec="seconds"),
                                "model": v.IMAGE_MODEL,
                                "prompt": prompt,
                                "extra": extra,
                                "cost": cost,
                                "generation_id": data.get("id"),
                                "url": url,
                            },
                            indent=1,
                        )
                    )
                except OSError:
                    pass
            images_out.append(entry)

        if not chat_sources:
            await status("Image generated, but could not be shown", done=True)
            return {
                "success": False,
                "error": (
                    "The image was generated (and billed) but could not be put in the chat: "
                    + (" ".join(warnings) or "uploading to Open WebUI is turned off")
                    + " Tell the user; do not call this tool again to retry, as that costs money again."
                ),
                "images": images_out,
            }

        emitted = v.IMAGE_DELIVERY != "model_embed" and bool(__event_emitter__)
        if emitted:
            if v.IMAGE_DELIVERY == "files_event":
                payload = {"type": "files", "data": {"files": [{"type": "image", "url": u} for u in chat_sources]}}
            else:
                md = "\n\n".join(f"![generated image]({u})" for u in chat_sources)
                payload = {"type": "message", "data": {"content": md + "\n\n"}}
            await _emit(__event_emitter__, payload)
        await status("Done", done=True)

        out: Dict[str, Any] = {
            "success": True,
            "model": v.IMAGE_MODEL,
            "images": images_out,
            "displayed_via": v.IMAGE_DELIVERY if emitted or v.IMAGE_DELIVERY == "model_embed" else "none",
            "note": (
                "The image is already displayed to the user; don't embed it again."
                if emitted
                else "Include image_markdown exactly as given in your reply so the user can see the image."
            )
            + " To send the user a link (e.g. by email), use an image's full_link; it opens only while they are logged in to Open WebUI."
            + " Do not call this tool again unless the user asks for another image; each call costs money.",
        }
        if cost is not None:
            out["cost_usd"] = cost
        if warnings:
            out["warnings"] = list(dict.fromkeys(warnings))
        if not emitted:
            out["image_markdown"] = "\n\n".join(f"![generated image]({u})" for u in chat_sources)
        return out
