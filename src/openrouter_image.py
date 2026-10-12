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


ASPECT_RATIOS = ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "4:5", "5:4", "21:9", "9:21", "2:1", "1:2"]
RESOLUTIONS = ["512", "768", "1K", "1.5K", "2K", "4K"]
OUTPUT_FORMATS = ["png", "jpeg", "webp"]
_FILE_URL_RE = re.compile(r"/api/v1/files/([A-Za-z0-9_-]+)/content")


def pick_option(value: Any, allowed: List[str], what: str) -> Optional[str]:
    """None when unset; the canonical spelling of a valid choice; otherwise a ValueError listing the choices."""
    if is_unset(value):
        return None
    wanted = str(value).strip().lower().replace(" ", "")
    for a in allowed:
        if a.lower() == wanted:
            return a
    if what == "output_format" and wanted == "jpg":
        return "jpeg"
    raise ValueError(f"{what} {str(value).strip()!r} is not supported. Choose one of: {', '.join(allowed)}, or pass \"\" for the default.")


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


def load_reference(valves: "Tools.Valves", ref: str) -> bytes:
    """Bytes of an earlier image: an Open WebUI file link/path, or a file name in OUTPUT_DIR."""
    ref = ref.strip()
    m = _FILE_URL_RE.search(ref)
    if m:
        if not valves.OPEN_WEBUI_API_KEY:
            raise ImageError("OPEN_WEBUI_API_KEY is empty in the tool's valves, so the reference image cannot be read.", 0)
        try:
            r = requests.get(
                f"{valves.OPEN_WEBUI_BASE_URL.rstrip('/')}/api/v1/files/{m.group(1)}/content",
                headers={"Authorization": f"Bearer {valves.OPEN_WEBUI_API_KEY}"},
                timeout=valves.REQUEST_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise ImageError(f"Could not read the reference image {ref!r} from Open WebUI: {e}", 0)
        return r.content
    path = Path(valves.OUTPUT_DIR) / Path(ref).name  # file name only: no folder tricks
    try:
        return path.read_bytes()
    except OSError:
        raise ImageError(
            f"Reference image {ref!r} was not found. Pass the 'chat_url' or 'filename' of an image this tool made earlier.", 0
        )


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
        DEFAULT_ASPECT_RATIO: Literal["default", "1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "4:5", "5:4", "21:9", "9:21", "2:1", "1:2"] = Field(
            default="default", description="Aspect ratio used for every image. 'default' leaves it to the model."
        )
        DEFAULT_RESOLUTION: Literal["default", "512", "768", "1K", "1.5K", "2K", "4K"] = Field(
            default="default", description="Resolution used for every image (larger costs more). 'default' leaves it to the model."
        )
        DEFAULT_OUTPUT_FORMAT: Literal["default", "png", "jpeg", "webp"] = Field(
            default="default", description="Image file format. 'default' leaves it to the model."
        )
        DEFAULT_SEED: str = Field(default="", description="A whole number to get the same look every time. Empty = random.")
        DEFAULT_REFERENCE_IMAGES: str = Field(
            default="",
            description="Reference image(s) sent with every call, e.g. a style or character sheet: an Open WebUI file link ('/api/v1/files/<id>/content') or a file name in OUTPUT_DIR; separate several with commas. Empty = none.",
        )
        MODEL_CAN_SET_OPTIONS: bool = Field(
            default=False,
            description="Off: the model only supplies a prompt and the DEFAULT_* settings above are always used; generate_muse_image_advanced refuses to run. On: the model may choose aspect ratio, resolution, format, seed and reference images per call through generate_muse_image_advanced. (Open WebUI saves a tool's parameter list when the tool is imported, so this switch cannot remove generate_muse_image_advanced from the model's tool list; it only decides whether that method works.)",
        )
        REQUEST_EXTRA: str = Field(
            default="",
            description='Optional JSON object merged into every request body, for options OpenRouter accepts but this tool does not know about. "model" and "prompt" cannot be overridden. Empty = nothing extra.',
        )
        OUTPUT_DIR: str = Field(
            default="/app/backend/data/openrouter_images",
            description="Folder (inside the Open WebUI container) where each image and a small .json sidecar (prompt, options, cost) are kept.",
        )
        SAVE_LOCAL_COPY: bool = Field(default=True, description="Keep each image and its sidecar in OUTPUT_DIR.")
        MAX_REFERENCES: int = Field(default=4, description="Most reference images accepted in one call (OpenRouter allows up to 16).")
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
            description="Address the user's browser uses for Open WebUI. Used to build a full link (share_url) that also works in an email (the user must be logged in to Open WebUI to open it). Empty = no full link.",
        )
        ATTACH_TO_CHAT: bool = Field(
            default=True,
            description="Attach each image to the assistant's message so it is listed in the chat's Files tab. (With IMAGE_DELIVERY 'files_event' the attachment is also what displays the image.)",
        )
        IMAGE_DELIVERY: Literal["model_embed", "files_event", "message_event"] = Field(
            default="model_embed",
            description=(
                "How the finished image is shown in the chat. "
                "'model_embed': the result includes image_markdown and the model is told to put it in its reply. "
                "'files_event': the tool attaches the image to the message via an event, which displays it; the model is told not to embed it. "
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
                "OpenRouter did not accept the request. The prompt may have been blocked by the model's content rules. "
                "Retry once with a rewritten, plainer prompt; if it fails again, tell the user."
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

    def _settings(self, aspect_ratio: Any, resolution: Any, output_format: Any, seed: Any, reference_image: Any) -> Tuple[Dict[str, Any], List[str]]:
        """Valve defaults, overridden by whatever per-call values are set. Raises ValueError on a bad value."""
        v = self.valves
        options: Dict[str, Any] = {}
        for key, call_val, default_val, allowed in (
            ("aspect_ratio", aspect_ratio, v.DEFAULT_ASPECT_RATIO, ASPECT_RATIOS),
            ("resolution", resolution, v.DEFAULT_RESOLUTION, RESOLUTIONS),
            ("output_format", output_format, v.DEFAULT_OUTPUT_FORMAT, OUTPUT_FORMATS),
        ):
            choice = pick_option(call_val, allowed, key) or pick_option(default_val, allowed, key)
            if choice:
                options[key] = choice
        seed_val = seed if not is_unset(seed) else v.DEFAULT_SEED
        if not is_unset(seed_val):
            try:
                options["seed"] = int(str(seed_val).strip())
            except ValueError:
                raise ValueError(f"seed {str(seed_val).strip()!r} is not a whole number. Pass a number, or \"\" for a random one.")
        raw_refs = reference_image if not is_unset(reference_image) else v.DEFAULT_REFERENCE_IMAGES
        refs = [] if is_unset(raw_refs) else [r.strip() for r in str(raw_refs).split(",") if r.strip()]
        if len(refs) > v.MAX_REFERENCES:
            raise ValueError(f"At most {v.MAX_REFERENCES} reference images are allowed; {len(refs)} were given.")
        return options, refs

    async def generate_muse_image(
        self,
        prompt: str,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Generate one image with Meta's Muse Image model on OpenRouter (a paid, hosted model that reasons
        about the prompt before drawing). Use it when the user asks for a picture. Describe everything in
        the one prompt, in plain sentences; size, format and the other settings are chosen for you. A call
        can take a minute or more, and every call costs money, so do not repeat it needlessly.

        :param prompt: A plain-language description of the image: subject, setting, style, lighting, mood, and any text that must appear in it.
        :return: A result with image_markdown (or a note that the image is already shown) and a share_url. Follow its note.
        """
        return await self._generate(prompt, "", "", "", "", "", __event_emitter__)

    async def generate_muse_image_advanced(
        self,
        prompt: str,
        aspect_ratio: str = "",
        resolution: str = "",
        output_format: str = "",
        seed: str = "",
        reference_image: str = "",
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Like generate_muse_image, with per-call settings. Only use this when the user explicitly asks for a
        particular shape or size, a repeatable look, or to change an earlier image; otherwise use
        generate_muse_image. It may be switched off, in which case it says so.

        Every parameter except prompt is optional; pass "" for the ones you don't need.

        :param prompt: A plain-language description of the image. When editing, describe what to change.
        :param aspect_ratio: Pass "" for the usual one. Otherwise one of 1:1, 4:3, 3:4, 3:2, 2:3, 16:9, 9:16, 4:5, 5:4, 21:9, 9:21, 2:1, 1:2.
        :param resolution: Pass "" for the usual one. Otherwise one of 512, 768, 1K, 1.5K, 2K, 4K (larger costs more).
        :param output_format: Pass "" for the usual one. Otherwise png, jpeg or webp.
        :param seed: Pass "" for a random result. A whole number repeats an earlier look.
        :param reference_image: Pass "" for a new image. To edit or build on an earlier image from this tool, pass its 'chat_url' (or 'filename'). Several can be given, separated by commas.
        :return: Same as generate_muse_image.
        """
        if not self.valves.MODEL_CAN_SET_OPTIONS:
            return {
                "success": False,
                "error": "Per-call image settings are turned off. Call generate_muse_image with just the prompt instead; the size and other settings are already chosen.",
            }
        return await self._generate(prompt, aspect_ratio, resolution, output_format, seed, reference_image, __event_emitter__)

    async def _generate(self, prompt, aspect_ratio, resolution, output_format, seed, reference_image, __event_emitter__) -> Dict[str, Any]:
        v = self.valves
        prompt = (prompt or "").strip()
        if not prompt:
            return {"success": False, "error": "prompt is empty. Call again with a description of the image."}
        if len(prompt) > v.MAX_PROMPT_CHARS:
            return {
                "success": False,
                "error": f"The prompt is {len(prompt)} characters; the limit is {v.MAX_PROMPT_CHARS}. Shorten it and call again.",
            }
        try:
            options, refs = self._settings(aspect_ratio, resolution, output_format, seed, reference_image)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        async def status(text: str, done: bool = False):
            await _emit(__event_emitter__, {"type": "status", "data": {"description": text, "done": done}})

        await status("Generating image...")
        try:
            extra = self._extra()
            if refs:
                blobs_in = await asyncio.to_thread(lambda: [load_reference(v, r) for r in refs])
                options["input_references"] = [
                    {"type": "image_url", "image_url": {"url": f"data:{image_ext(b)[1]};base64,{base64.b64encode(b).decode('ascii')}"}}
                    for b in blobs_in
                ]
            extra = {**extra, **options}
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
        chat_files: List[Dict[str, Any]] = []
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
                entry["chat_url"] = url
                base = v.PUBLIC_BASE_URL.strip().rstrip("/")
                if base:
                    entry["full_link"] = f"{base}{url}"
                m = _FILE_URL_RE.search(url)
                chat_files.append({"id": m.group(1) if m else None, "url": url, "name": name, "size": len(blob)})

            if saved:
                try:
                    Path(saved).with_suffix(".json").write_text(
                        json.dumps(
                            {
                                "created": stamp.isoformat(timespec="seconds"),
                                "model": v.IMAGE_MODEL,
                                "prompt": prompt,
                                "extra": {k: val for k, val in extra.items() if k != "input_references"},
                                "references": len(refs),
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

        if not chat_files:
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

        chat_sources = [f["url"] for f in chat_files]
        shows_inline = v.IMAGE_DELIVERY == "files_event" and bool(__event_emitter__)
        if shows_inline:
            # One event both displays the image and lists it in the Files tab.
            payload = {"type": "files", "data": {"files": [{"type": "image", "status": "uploaded", **f} for f in chat_files]}}
            await _emit(__event_emitter__, payload)
        else:
            if v.IMAGE_DELIVERY == "message_event" and __event_emitter__:
                md = "\n\n".join(f"![generated image]({u})" for u in chat_sources)
                await _emit(__event_emitter__, {"type": "message", "data": {"content": md + "\n\n"}})
            if v.ATTACH_TO_CHAT and __event_emitter__:
                # type "file" (not "image") so it is listed without being drawn a second time.
                await _emit(
                    __event_emitter__,
                    {"type": "files", "data": {"files": [{"type": "file", "status": "uploaded", **f} for f in chat_files]}},
                )
        emitted = v.IMAGE_DELIVERY != "model_embed" and bool(__event_emitter__)
        await status("Done", done=True)

        share = next((e["full_link"] for e in images_out if e.get("full_link")), None)
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
            + (
                " To give the user a link (in chat or by email), paste share_url exactly as given; it opens only while they are logged in to Open WebUI."
                if share
                else ""
            )
            + " Do not call this tool again unless the user asks for another image; each call costs money.",
        }
        if share:
            out["share_url"] = share
        if cost is not None:
            out["cost_usd"] = cost
        if warnings:
            out["warnings"] = list(dict.fromkeys(warnings))
        if not emitted:
            out["image_markdown"] = "\n\n".join(f"![generated image]({u})" for u in chat_sources)
        return out
