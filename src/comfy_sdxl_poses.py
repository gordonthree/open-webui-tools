"""
title: ComfyUI Pose Catalog
author: Gordon
version: 1.0.0
description: Companion to ComfyUI SDXL Direct/Graph. Catalogs reusable ControlNet pose reference
    images - pre-extracted skeleton/keypoint images ready for a ControlNetApply node, or raw
    reference photos/drawings that still need a pose preprocessor node first - in the shared job
    database, so a model can find one by description instead of needing a file path. list_poses/
    get_pose browse the catalog; select_pose resolves a chosen pose to a ready-to-use source_image
    reference on a specific GPU server (copying the file across servers first if needed); add_pose/
    delete_pose maintain the catalog. This tool never renders anything itself - pass select_pose's
    result into generate_image's or run_workflow's source_image argument to actually draw with it.
"""

import asyncio
import datetime
import json
import logging
import mimetypes
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import requests
from pydantic import BaseModel, Field
from requests.exceptions import RequestException

logger = logging.getLogger("comfy_sdxl_poses")
logger.setLevel(logging.INFO)


# Everything below mirrors comfy_sdxl_direct.py / comfy_sdxl_graph.py / comfy_sdxl_retrieve.py.
# Open WebUI tools are single files, so shared logic is copied rather than imported; keep all four
# in sync by hand.

# --------------------------------------------------------------------------- #
# SQLite pose catalog - same database file as the other three tools' job tables (identical
# JOB_DB_PATH valve), but this is the only one of the four that reads/writes this table. A pose
# entry just points at an image that already lives in some ComfyUI server's output folder; the
# image bytes themselves are never stored in the database or resent through chat.
# --------------------------------------------------------------------------- #

POSE_REFERENCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS pose_references (
    pose_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    home_server TEXT NOT NULL,
    subfolder TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_pose_references_tags ON pose_references(tags);
CREATE INDEX IF NOT EXISTS idx_pose_references_kind ON pose_references(kind);
"""

JOB_DB_BUSY_TIMEOUT_S = 5.0
_job_db_schema_ready: set = set()  # db_path values confirmed (this process) to have the schema


def job_db_connect(db_path: str):
    """A short-lived connection with the schema ensured. Caller must close() it."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=JOB_DB_BUSY_TIMEOUT_S)
    conn.execute("PRAGMA journal_mode=WAL;")
    if db_path not in _job_db_schema_ready:
        conn.executescript(POSE_REFERENCES_SCHEMA)
        _job_db_schema_ready.add(db_path)
    return conn


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save_pose(
    db_path: str,
    pose_id: str,
    description: str,
    tags: str,
    kind: str,
    home_server: str,
    subfolder: str,
    filename: str,
    width: Optional[int],
    height: Optional[int],
    created_by: str,
    existing_created_at: Optional[str],
) -> None:
    conn = job_db_connect(db_path)
    try:
        now = _now_iso()
        conn.execute(
            "INSERT INTO pose_references "
            "(pose_id, description, tags, kind, home_server, subfolder, filename, width, height, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pose_id) DO UPDATE SET description=excluded.description, tags=excluded.tags, kind=excluded.kind, "
            "home_server=excluded.home_server, subfolder=excluded.subfolder, filename=excluded.filename, "
            "width=excluded.width, height=excluded.height, updated_at=excluded.updated_at, created_by=excluded.created_by",
            (pose_id, description, tags, kind, home_server, subfolder, filename, width, height, existing_created_at or now, now, created_by),
        )
        conn.commit()
    finally:
        conn.close()


def load_pose(db_path: str, pose_id: str) -> Optional[Any]:
    conn = job_db_connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM pose_references WHERE pose_id = ?", (pose_id,)).fetchone()
    finally:
        conn.close()


def list_poses_db(
    db_path: str, search: Optional[str] = None, tags: Optional[str] = None, kind: Optional[str] = None, limit: int = 20
) -> List[Any]:
    conn = job_db_connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        query = "SELECT pose_id, description, tags, kind, width, height, updated_at FROM pose_references WHERE 1=1"
        params: List[Any] = []
        if not is_unset(kind):
            query += " AND kind = ?"
            params.append(kind.strip().lower())
        if not is_unset(tags):
            query += " AND (',' || tags || ',') LIKE ? ESCAPE '\\'"
            params.append(f"%,{_like_escape(tags.strip().lower())},%")
        if not is_unset(search):
            query += " AND (pose_id LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
            like = f"%{_like_escape(search)}%"
            params += [like, like]
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def delete_pose_db(db_path: str, pose_id: str) -> bool:
    conn = job_db_connect(db_path)
    try:
        cur = conn.execute("DELETE FROM pose_references WHERE pose_id = ?", (pose_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

_UNSET_STRINGS = {"", "default", "auto", "none", "null", "undefined", "n/a"}
_ANNOTATION_RE = re.compile(r"\s*\[(\w+)\]\s*$")
_POSE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_OWUI_FILE_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
KIND_CHOICES = ("skeleton", "reference")


def is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def validate_pose_id(pose_id: str) -> str:
    normalized = (pose_id or "").strip().lower()
    if not _POSE_ID_RE.match(normalized):
        raise ValueError(
            f"Invalid pose_id {pose_id!r}: use 2-64 lowercase letters, digits, underscores or "
            "hyphens, starting with a letter or digit."
        )
    return normalized


def parse_source_image(ref: str) -> "tuple[str, str]":
    text = (ref or "").strip()
    md = re.search(r"\((https?://[^)\s]+)\)", text)
    if md:
        text = md.group(1)
    if text.startswith(("http://", "https://")):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        filename = (query.get("filename") or [""])[0]
        if not filename:
            raise ValueError("source_image URL has no 'filename' parameter.")
        if (query.get("type") or ["output"])[0] != "output":
            raise ValueError("source_image must be in the ComfyUI output folder.")
        subfolder = (query.get("subfolder") or [""])[0]
        text = f"{subfolder}/{filename}" if subfolder else filename
    annotation = _ANNOTATION_RE.search(text)
    if annotation:
        if annotation.group(1) != "output":
            raise ValueError("source_image must be in the ComfyUI output folder.")
        text = text[: annotation.start()]
    text = text.replace("\\", "/").strip("/")
    parts = text.split("/")
    if not text or ".." in parts:
        raise ValueError(f"Invalid source_image: {ref!r}")
    return "/".join(parts[:-1]), parts[-1]


def parse_openwebui_file_id(ref: str) -> str:
    """Extract a bare Open WebUI file id from a UUID, a '/api/v1/files/<id>/content' path, or a
    full URL - deliberately never trusts a model-supplied host, since that ID is then used to
    build a request carrying our own OPEN_WEBUI_API_KEY bearer token."""
    text = (ref or "").strip()
    m = _OWUI_FILE_ID_RE.search(text)
    if m:
        return m.group(0)
    if text and "/" not in text and " " not in text:
        return text
    raise ValueError(
        f"Could not find an Open WebUI file id in {ref!r}; pass the bare file id or the "
        "/api/v1/files/<id>/content path Open WebUI gave you."
    )


def resolve_server(requested: Optional[str], default: str, allowed: List[str], allow_unlisted: bool = False) -> str:
    allowed_norm = [s.rstrip("/") for s in allowed]
    if is_unset(requested):
        return default.rstrip("/")

    wanted = requested.strip()
    if wanted.endswith("/") and not wanted.endswith("://"):
        wanted = wanted[:-1]
    if wanted in allowed_norm:
        return wanted

    with_scheme = wanted if "://" in wanted else f"http://{wanted}"
    parsed = urllib.parse.urlparse(with_scheme)
    for server in allowed_norm:
        s = urllib.parse.urlparse(server)
        if parsed.hostname == s.hostname and (parsed.port is None or parsed.port == s.port):
            return server

    if allow_unlisted:
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"gpu_server {requested!r} is not a valid http(s) address.")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError(f"gpu_server {requested!r} has an invalid port.") from None
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return f"{parsed.scheme}://{host}:{port or 8188}"

    raise ValueError(
        f"Unknown gpu_server {requested!r}. Omit it to use the default, choose one of {allowed_norm}, "
        "or enable the ALLOW_UNLISTED_SERVERS valve."
    )


def probe_image_size(data: bytes) -> "tuple[Optional[int], Optional[int]]":
    """Best-effort width/height for the catalog entry - purely a convenience for whoever picks
    this pose later (e.g. to match generate_image's width/height). Never fatal: Open WebUI's
    backend may or may not have Pillow installed, and either way a pose is still fully usable
    without dimensions on record."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.width, im.height
    except Exception:
        return None, None


# --------------------------------------------------------------------------- #
# ComfyUI / Open WebUI I/O - just enough to move a pose image between servers. This tool never
# submits a render, so it carries none of the /prompt, /history or websocket-progress machinery
# the other three files have.
# --------------------------------------------------------------------------- #

class ComfyError(Exception):
    def __init__(self, message: str, body: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.body = body


class ComfyClient:
    def __init__(self, base_url: str, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def view_url(self, image: Dict[str, Any]) -> str:
        query = urllib.parse.urlencode({"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")})
        return f"{self.base_url}/view?{query}"

    def fetch_image(self, image: Dict[str, Any]) -> Optional[bytes]:
        try:
            r = requests.get(self.view_url(image), timeout=self.timeout)
        except RequestException as e:
            raise ComfyError(f"Could not download {image['filename']}: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ComfyError(f"{self.base_url} answered HTTP {r.status_code} for {image['filename']}")
        return r.content

    def upload_image(self, image_bytes: bytes, filename: str, subfolder: str = "", type_: str = "output") -> Dict[str, str]:
        try:
            r = requests.post(
                f"{self.base_url}/upload/image",
                files={"image": (filename, image_bytes, mimetypes.guess_type(filename)[0] or "image/png")},
                data={"subfolder": subfolder, "type": type_, "overwrite": "true"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except RequestException as e:
            raise ComfyError(f"Could not upload {filename} to {self.base_url}: {e}") from e
        except ValueError as e:
            raise ComfyError(f"Unexpected upload response from {self.base_url}: {e}") from e
        return {"filename": data.get("name", filename), "subfolder": data.get("subfolder", subfolder), "type": data.get("type", type_)}


async def copy_source_image(source_server: str, target_client: "ComfyClient", subfolder: str, filename: str, timeout: int) -> None:
    src = ComfyClient(source_server, timeout)
    data = await asyncio.to_thread(src.fetch_image, {"filename": filename, "subfolder": subfolder, "type": "output"})
    if data is None:
        raise ComfyError(f"{filename!r} was not found in {source_server}'s output folder.")
    await asyncio.to_thread(target_client.upload_image, data, filename, subfolder, "output")


def fetch_from_open_webui(base_url: str, api_key: str, file_id: str, timeout: int) -> "tuple[Optional[bytes], Optional[str]]":
    if not api_key:
        return None, "OPEN_WEBUI_API_KEY is empty in the tool's valves"
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/v1/files/{file_id}/content",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.content, None
    except RequestException as e:
        detail = ""
        response = getattr(e, "response", None)
        if response is not None:
            detail = f" (HTTP {response.status_code}: {str(response.text)[:200]})"
        return None, f"{e}{detail}"


# --------------------------------------------------------------------------- #
# Open WebUI tool
# --------------------------------------------------------------------------- #

class Tools:
    class Valves(BaseModel):
        GPU_SERVERS: List[str] = Field(default=["http://192.168.10.11:8188", "http://192.168.10.12:8188"], description="ComfyUI servers, same list as the other tools.")
        DEFAULT_GPU_SERVER: str = Field(default="http://192.168.10.11:8188", description="Server used when the model doesn't pick one.")
        ALLOW_UNLISTED_SERVERS: bool = Field(default=True, description="Let the model target any well-formed ComfyUI address, not just the ones in GPU_SERVERS.")
        REQUEST_TIMEOUT_SECONDS: int = Field(default=15, description="Per-HTTP-request timeout.")
        JOB_DB_PATH: str = Field(
            default="/app/backend/data/comfy_outputs/comfy_jobs.sqlite3",
            description="SQLite database file for the pose catalog. Keep this identical to the other tools' JOB_DB_PATH valve so everything shares one database.",
        )
        MAX_POSE_LIST_RESULTS: int = Field(default=40, description="Caps list_poses results, so a broad search still returns something chat-sized.")
        OPEN_WEBUI_BASE_URL: str = Field(default="http://localhost:8080", description="Address the tool uses to call Open WebUI's own API, from the backend's point of view. Only needed for add_pose(openwebui_file=...).")
        OPEN_WEBUI_API_KEY: str = Field(default="", description="Personal API key from Open WebUI (Settings -> Account -> API Keys). Only needed for add_pose(openwebui_file=...).")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def list_poses(
        self, search: Optional[str] = None, tags: Optional[str] = None, kind: Optional[Literal["skeleton", "reference"]] = None, limit: int = 20
    ) -> Dict[str, Any]:
        """
        Browse the pose catalog as a Markdown table (pose_id, kind, tags, description) - find a
        reusable pose reference by what it looks like, without ever seeing the image itself. Call
        select_pose next to actually use one in a render.

        :param search: Substring match (case-insensitive) over pose_id and description. Pass "" (or omit) for no text filter.
        :param tags: Only poses whose tags include this one, exactly. Pass "" (or omit) for no tag filter.
        :param kind: "skeleton" (pre-extracted, ready for ControlNetApply) or "reference" (raw photo/drawing, needs a preprocessor first). Pass "" (or omit) for both.
        :param limit: Maximum rows returned, default 20.
        """
        v = self.valves
        try:
            if not is_unset(kind) and kind.strip().lower() not in KIND_CHOICES:
                raise ValueError(f"kind must be one of {list(KIND_CHOICES)} or omitted, got {kind!r}.")
            capped_limit = min(20 if is_unset(limit) else int(limit), v.MAX_POSE_LIST_RESULTS)
            rows = await asyncio.to_thread(list_poses_db, v.JOB_DB_PATH, search, tags, kind, capped_limit)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not rows:
            return {
                "success": True,
                "count": 0,
                "table": "No poses matched." if (search or tags or kind) else "No poses registered yet - use add_pose to catalog one.",
            }
        lines = ["| Pose ID | Kind | Tags | Description |", "|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['pose_id']} | {r['kind']} | {r['tags'] or ''} | {_truncate(r['description'], 120)} |")
        return {"success": True, "count": len(rows), "table": "\n".join(lines)}

    async def get_pose(self, pose_id: str) -> Dict[str, Any]:
        """
        Fetch one pose's full catalog entry (description, tags, kind, home server, dimensions) -
        for inspecting what's catalogued. To actually use a pose in a render, call select_pose
        instead; it resolves this into a ready-to-use source_image reference.

        :param pose_id: A pose's id, as shown by list_poses.
        """
        try:
            pid = validate_pose_id(pose_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        row = await asyncio.to_thread(load_pose, self.valves.JOB_DB_PATH, pid)
        if not row:
            return {"success": False, "error": f"No pose named {pid!r}. Use list_poses to see what's catalogued."}
        return {
            "success": True,
            "pose_id": row["pose_id"],
            "kind": row["kind"],
            "description": row["description"],
            "tags": [t for t in (row["tags"] or "").split(",") if t],
            "home_server": row["home_server"],
            "width": row["width"],
            "height": row["height"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "note": f"Call select_pose(pose_id={pid!r}, gpu_server=...) to get a ready-to-use source_image reference for rendering.",
        }

    async def select_pose(self, pose_id: str, gpu_server: Optional[str] = None) -> Dict[str, Any]:
        """
        Resolve a catalogued pose to a ready-to-use source_image reference on a specific GPU
        server - copying the underlying image across servers first if it isn't already there.
        Pass the result's 'source_image' straight into generate_image's or run_workflow's
        source_image argument (with the same gpu_server) to actually render with it; for a
        ControlNet workflow, that's the image run_workflow's fixed LoadImageOutput node
        (__comfy_tool_source_image__) makes available to wire into your graph.

        :param pose_id: A pose's id, as shown by list_poses.
        :param gpu_server: The server you intend to render on. Pass "" (or omit) for the default server.
        """
        try:
            pid = validate_pose_id(pose_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        v = self.valves
        row = await asyncio.to_thread(load_pose, v.JOB_DB_PATH, pid)
        if not row:
            return {"success": False, "error": f"No pose named {pid!r}. Use list_poses to see what's catalogued."}

        try:
            allowed = list(dict.fromkeys(list(v.GPU_SERVERS) + [v.DEFAULT_GPU_SERVER]))
            server = resolve_server(gpu_server, v.DEFAULT_GPU_SERVER, allowed, v.ALLOW_UNLISTED_SERVERS)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        copied_from = None
        if server != row["home_server"]:
            client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)
            try:
                await copy_source_image(row["home_server"], client, row["subfolder"], row["filename"], v.REQUEST_TIMEOUT_SECONDS)
            except ComfyError as e:
                return {"success": False, "error": str(e)}
            copied_from = row["home_server"]

        ref = f"{row['subfolder']}/{row['filename']}" if row["subfolder"] else row["filename"]
        kind = row["kind"]
        note = (
            "Pre-extracted pose control image - wire it straight into a ControlNetApply(Advanced) "
            "node (fed by a ControlNetLoader); no preprocessor needed."
            if kind == "skeleton"
            else "Raw reference photo/drawing, not yet a pose control image - your graph needs a pose "
            "preprocessor node (e.g. an OpenPose/DWPose preprocessor - check list_node_types/"
            "get_node_info on this server) between the source image and ControlNetApply."
        )
        out: Dict[str, Any] = {
            "success": True,
            "pose_id": pid,
            "kind": kind,
            "description": row["description"],
            "tags": [t for t in (row["tags"] or "").split(",") if t],
            "server": server,
            "source_image": ref,
            "width": row["width"],
            "height": row["height"],
            "note": note,
        }
        if copied_from:
            out["source_image_note"] = f"Copied source image from {copied_from} to {server}."
        return out

    async def add_pose(
        self,
        pose_id: str,
        description: str,
        kind: Literal["skeleton", "reference"],
        source_image: Optional[str] = None,
        openwebui_file: Optional[str] = None,
        gpu_server: Optional[str] = None,
        tags: Optional[str] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """
        Register a pose reference image in the shared catalog, so list_poses/select_pose can find
        it later by description instead of a file path. Give it EITHER `source_image` (an image
        already sitting in a ComfyUI server's output folder - e.g. one this tool chain just
        rendered) OR `openwebui_file` (an image already in Open WebUI's own file storage - e.g.
        one the user attached in chat), never both. The image itself is never resent through this
        chat.

        :param pose_id: A short slug (2-64 chars: lowercase letters, digits, underscore, hyphen), e.g. "standing_hands_on_hips". This becomes select_pose's/get_pose's/delete_pose's argument.
        :param description: Plain-language description of the pose (e.g. "standing, three-quarter view, both hands on hips, facing camera"). This is what list_poses searches, so write it for a model choosing a pose later, not for yourself.
        :param kind: "skeleton" for an already pre-extracted OpenPose/DWPose-style control image, ready to feed a ControlNetApply node directly. "reference" for a raw photo or drawing that still needs a pose preprocessor node first. Required - get it wrong and whoever builds the graph will wire it incorrectly.
        :param source_image: An image reference already in a ComfyUI server's output folder (e.g. an earlier render's 'use_as_source_image'). Give this XOR openwebui_file. Pass "" (or omit) when using openwebui_file instead.
        :param openwebui_file: An image already in Open WebUI's own file storage - the bare file id, or the '/api/v1/files/<id>/content' path/URL Open WebUI gives out for an uploaded/attached image. Downloaded and copied into gpu_server's output folder automatically. Give this XOR source_image. Pass "" (or omit) when using source_image instead.
        :param gpu_server: For source_image, the server that reference already lives on. For openwebui_file, the server to upload the copy to. Pass "" (or omit) for the default server.
        :param tags: Comma-separated tags (e.g. "standing,action,openpose") to help list_poses filtering. Pass "" (or omit) for none.
        :param overwrite: true to replace an existing pose with this id; false (the normal choice) makes an id collision an error instead, to avoid silently clobbering someone else's catalogued pose.
        """
        v = self.valves
        try:
            pid = validate_pose_id(pose_id)
            if not description or not description.strip():
                raise ValueError("description is required - it's how a model finds this pose later.")
            kind_norm = (kind or "").strip().lower()
            if kind_norm not in KIND_CHOICES:
                raise ValueError(f"kind must be one of {list(KIND_CHOICES)}, got {kind!r}.")

            has_source = not is_unset(source_image)
            has_owui = not is_unset(openwebui_file)
            if has_source == has_owui:
                raise ValueError("Pass exactly one of source_image or openwebui_file.")

            allowed = list(dict.fromkeys(list(v.GPU_SERVERS) + [v.DEFAULT_GPU_SERVER]))
            server = resolve_server(gpu_server, v.DEFAULT_GPU_SERVER, allowed, v.ALLOW_UNLISTED_SERVERS)
            client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)

            if has_source:
                subfolder, filename = parse_source_image(source_image)
                data = await asyncio.to_thread(client.fetch_image, {"filename": filename, "subfolder": subfolder, "type": "output"})
                if data is None:
                    raise ValueError(f"{source_image!r} was not found in {server}'s output folder.")
            else:
                file_id = parse_openwebui_file_id(openwebui_file)
                data, err = await asyncio.to_thread(fetch_from_open_webui, v.OPEN_WEBUI_BASE_URL, v.OPEN_WEBUI_API_KEY, file_id, v.REQUEST_TIMEOUT_SECONDS)
                if data is None:
                    raise ValueError(f"Could not fetch Open WebUI file {file_id!r}: {err}")
                uploaded = await asyncio.to_thread(client.upload_image, data, f"pose_{pid}.png", "", "output")
                subfolder, filename = uploaded["subfolder"], uploaded["filename"]

            width, height = probe_image_size(data)

            existing = await asyncio.to_thread(load_pose, v.JOB_DB_PATH, pid)
            if existing and not overwrite:
                return {
                    "success": False,
                    "error": f"A pose named {pid!r} already exists ({existing['description']!r}). "
                    "Pass overwrite=true to replace it, or choose a different pose_id.",
                }

            tag_list = sorted({t.strip().lower() for t in (tags or "").split(",") if t.strip()})
            await asyncio.to_thread(
                save_pose, v.JOB_DB_PATH, pid, description.strip(), ",".join(tag_list), kind_norm,
                server, subfolder, filename, width, height, "add_pose",
                existing["created_at"] if existing else None,
            )
        except (ValueError, ComfyError) as e:
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "pose_id": pid,
            "kind": kind_norm,
            "description": description.strip(),
            "tags": tag_list,
            "server": server,
            "width": width,
            "height": height,
            "note": f"Registered. Call select_pose(pose_id={pid!r}, gpu_server=...) to get a ready-to-use source_image reference for rendering.",
        }

    async def delete_pose(self, pose_id: str) -> Dict[str, Any]:
        """
        Permanently remove a pose from the catalog. Does not delete the underlying image file
        from whatever ComfyUI server it lives on.

        :param pose_id: A pose's id, as shown by list_poses.
        """
        try:
            pid = validate_pose_id(pose_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        removed = await asyncio.to_thread(delete_pose_db, self.valves.JOB_DB_PATH, pid)
        if not removed:
            return {"success": False, "error": f"No pose named {pid!r}."}
        return {"success": True, "pose_id": pid, "note": "Deleted from the catalog (the image file itself was left in place)."}
