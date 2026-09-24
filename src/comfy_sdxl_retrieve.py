"""
title: ComfyUI SDXL Retrieve
author: Gordon
version: 1.4.0
description: Companion to ComfyUI SDXL Direct. Given a job id (from queue_only) or an image filename, finds the result on the ComfyUI server and shows it in chat, or reports that the job is still queued/running, or that nothing was found. Also exposes search_jobs (structured search over the shared job database), list_jobs (a browsable Markdown table of job history), and retrieve_graph (fetches the submitted ComfyUI graph itself for a job).
"""

import asyncio
import datetime
import json
import logging
import mimetypes
import re
import sqlite3
import struct
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import requests
from pydantic import BaseModel, Field
from requests.exceptions import RequestException

logger = logging.getLogger("comfy_sdxl_retrieve")
logger.setLevel(logging.INFO)

# All helpers are module-level so `retrieve_image`/`search_jobs` are the only tools exposed.
# (Open WebUI tools are single files, so a few helpers are copied from
# comfy_sdxl_direct.py rather than imported.)

# --------------------------------------------------------------------------- #
# SQLite job database - shared with comfy_sdxl_direct.py / comfy_sdxl_graph.py, which do all the
# WRITING for jobs they run (job_uuid generation, submit/complete/fail lifecycle). This file only
# ever READS the database (search_jobs) or, when it opportunistically finds a file by filename,
# BACKFILLS a job that's missing its comfy_prompt_id (reconciliation, below) - it never creates a
# job row itself, so only a subset of the other files' job-DB code is needed here. Keep
# JOB_DB_SCHEMA/PROMPTS_FTS_SCHEMA byte-identical to the other two files if this schema ever
# changes.
# --------------------------------------------------------------------------- #

JOB_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_uuid TEXT PRIMARY KEY,
    comfy_prompt_id TEXT,
    tool TEXT NOT NULL,
    server TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    execution_start_ts INTEGER,
    execution_end_ts INTEGER,
    duration_s REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_comfy_prompt_id ON jobs(comfy_prompt_id);
CREATE INDEX IF NOT EXISTS idx_jobs_server_status ON jobs(server, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);

CREATE TABLE IF NOT EXISTS inputs (
    job_uuid TEXT PRIMARY KEY REFERENCES jobs(job_uuid),
    raw_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outputs (
    job_uuid TEXT PRIMARY KEY REFERENCES jobs(job_uuid),
    raw_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL REFERENCES jobs(job_uuid),
    stage TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_job_uuid ON results(job_uuid);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL REFERENCES jobs(job_uuid),
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    raw_json TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_job_uuid ON errors(job_uuid);

CREATE TABLE IF NOT EXISTS prompts (
    job_uuid TEXT PRIMARY KEY REFERENCES jobs(job_uuid),
    positive_prompt TEXT,
    negative_prompt TEXT
);

CREATE TABLE IF NOT EXISTS node_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL REFERENCES jobs(job_uuid),
    node_id TEXT NOT NULL,
    class_type TEXT NOT NULL,
    param_name TEXT NOT NULL,
    value_text TEXT,
    value_num REAL
);
CREATE INDEX IF NOT EXISTS idx_node_params_job_uuid ON node_params(job_uuid);
CREATE INDEX IF NOT EXISTS idx_node_params_lookup ON node_params(class_type, param_name, value_text);
"""
PROMPTS_FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(job_uuid UNINDEXED, positive_prompt, negative_prompt);"

JOB_DB_BUSY_TIMEOUT_S = 5.0
_job_db_schema_ready: set = set()  # db_path values confirmed (this process) to have the schema


def job_db_connect(db_path: str):
    """A short-lived connection with the schema ensured. Caller must close() it."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=JOB_DB_BUSY_TIMEOUT_S)
    conn.execute("PRAGMA journal_mode=WAL;")
    if db_path not in _job_db_schema_ready:
        conn.executescript(JOB_DB_SCHEMA)
        try:
            conn.execute(PROMPTS_FTS_SCHEMA)
        except Exception:  # sqlite3.OperationalError if this build lacks FTS5; search falls back to LIKE
            pass
        _job_db_schema_ready.add(db_path)
    return conn


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def has_prompts_fts(conn) -> bool:
    try:
        conn.execute("SELECT 1 FROM prompts_fts LIMIT 0;")
        return True
    except Exception:
        return False


def _execution_timing(history_entry: Dict[str, Any]) -> "tuple[Optional[int], Optional[int], Optional[float]]":
    """(start_ts_ms, end_ts_ms, duration_s), all None if the timestamps aren't present."""
    start_ts = end_ts = None
    for name, data in (history_entry.get("status") or {}).get("messages", []):
        ts = data.get("timestamp") if isinstance(data, dict) else None
        if name == "execution_start" and isinstance(ts, (int, float)):
            start_ts = int(ts)
        elif name in ("execution_success", "execution_error", "execution_interrupted") and isinstance(ts, (int, float)):
            end_ts = int(ts)
    duration = (end_ts - start_ts) / 1000 if start_ts is not None and end_ts is not None else None
    return start_ts, end_ts, duration


def record_job_completed(db_path: str, job_uuid: str, history_entry: Dict[str, Any]) -> None:
    """
    Final write for a job this file resolves: ComfyUI's history entry, with its own reported
    timing. Does NOT touch comfy_prompt_id - only record_job_submitted (in the other two tools)
    or record_job_reconciled (below, this file's own wrapper) do that.
    """
    try:
        start_ts, end_ts, duration = _execution_timing(history_entry)
        conn = job_db_connect(db_path)
        try:
            now = _now_iso()
            conn.execute(
                "UPDATE jobs SET status = 'completed', execution_start_ts = ?, execution_end_ts = ?, duration_s = ? WHERE job_uuid = ?",
                (start_ts, end_ts, duration, job_uuid),
            )
            conn.execute(
                "INSERT INTO results (job_uuid, stage, raw_json, recorded_at) VALUES (?, 'history', ?, ?)",
                (job_uuid, json.dumps(history_entry, default=str), now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Job DB: could not record completed job %s: %s", job_uuid, e)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ANNOTATION_RE = re.compile(r"\s*\[(\w+)\]\s*$")
_UNSET_STRINGS = {"", "default", "auto", "none", "null", "undefined", "n/a"}


class ComfyError(Exception):
    """Raised when ComfyUI can't be reached or answers unexpectedly."""


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

def is_unset(value: Any) -> bool:
    """True for None or placeholder strings models like to send for 'not provided'."""
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


def resolve_server(
    requested: Optional[str], default: str, allowed: List[str], allow_unlisted: bool = False
) -> str:
    """
    Unset -> default. A URL/host matching a configured server -> that server.
    Anything else is accepted as-is if allow_unlisted is on (any well-formed
    http(s) address; a missing port defaults to ComfyUI's 8188), else rejected.
    """
    allowed_norm = [s.rstrip("/") for s in allowed]
    if is_unset(requested):
        return default.rstrip("/")

    wanted = requested.strip().rstrip("/")
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
        return f"{parsed.scheme}://{host}:{port or 8188}"  # origin only; any path/credentials are dropped

    raise ValueError(
        f"Unknown gpu_server {requested!r}. Omit it to search all configured servers, choose one of "
        f"{allowed_norm}, or enable the ALLOW_UNLISTED_SERVERS valve."
    )


def parse_image_ref(ref: str) -> Tuple[str, str]:
    """
    Turn a filename, "subfolder/filename", an annotated "name [output]", a
    ComfyUI /view URL, or a markdown image of one into (subfolder, filename).
    """
    text = (ref or "").strip()

    md = re.search(r"\((https?://[^)\s]+)\)", text)
    if md:
        text = md.group(1)

    if text.startswith(("http://", "https://")):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        filename = (query.get("filename") or [""])[0]
        if not filename:
            raise ValueError("The URL has no 'filename' parameter.")
        if (query.get("type") or ["output"])[0] != "output":
            raise ValueError("Only images in the ComfyUI output folder can be retrieved.")
        subfolder = (query.get("subfolder") or [""])[0]
        text = f"{subfolder}/{filename}" if subfolder else filename

    annotation = _ANNOTATION_RE.search(text)
    if annotation:
        if annotation.group(1) != "output":
            raise ValueError("Only images in the ComfyUI output folder can be retrieved.")
        text = text[: annotation.start()]

    text = text.replace("\\", "/").strip("/")
    parts = text.split("/")
    if not text or ".." in parts:
        raise ValueError(f"Invalid image reference: {ref!r}")
    return "/".join(parts[:-1]), parts[-1]


def _queue_item_id(item: Any) -> Optional[str]:
    return item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else None


def _queue_item_number(item: Any) -> Optional[int]:
    if isinstance(item, (list, tuple)) and item and isinstance(item[0], int):
        return item[0]
    return None


def queue_snapshot(queue: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    """
    Find a job in ComfyUI's /queue response. Returns its state and queue stats,
    or None if it isn't queued or running.
    """
    running = list(queue.get("queue_running") or [])
    pending = sorted(
        list(queue.get("queue_pending") or []),
        key=lambda it: _queue_item_number(it) if _queue_item_number(it) is not None else 10**18,
    )
    stats = {"running_count": len(running), "pending_count": len(pending)}

    for item in running:
        if _queue_item_id(item) == job_id:
            return {"state": "running", "queue_number": _queue_item_number(item), "jobs_ahead": 0, **stats}

    for index, item in enumerate(pending):
        if _queue_item_id(item) == job_id:
            return {
                "state": "pending",
                "queue_number": _queue_item_number(item),
                "position_in_queue": index + 1,
                "jobs_ahead": len(running) + index,  # everything running, plus pending jobs before this one
                **stats,
            }
    return None


def count_jobs_writing_prefix(queue: Dict[str, Any], image_ref: str) -> int:
    """How many queued/running jobs save images under a prefix that could produce image_ref."""
    count = 0
    for item in list(queue.get("queue_running") or []) + list(queue.get("queue_pending") or []):
        prompt = item[2] if isinstance(item, (list, tuple)) and len(item) > 2 and isinstance(item[2], dict) else {}
        for node in prompt.values():
            if isinstance(node, dict) and node.get("class_type") == "SaveImage":
                prefix = str((node.get("inputs") or {}).get("filename_prefix", ""))
                if prefix and image_ref.startswith(prefix + "_"):
                    count += 1
                    break
    return count


# ---- generation parameters -------------------------------------------------

_CKPT_CLASSES = ("CheckpointLoaderSimple", "CheckpointLoader", "unCLIPCheckpointLoader")


def _natural_key(node_id: str) -> Tuple[int, int, str]:
    return (0, int(node_id), "") if node_id.isdigit() else (1, 0, node_id)


def _link_target(nodes: Dict[str, Any], value: Any) -> Optional[Dict[str, Any]]:
    """Follow an API-graph link like ["12", 0] to the node it points at."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
        return nodes.get(str(value[0]))
    return None


def _scalar(nodes: Dict[str, Any], value: Any) -> Any:
    """A plain value, or - if it is wired to a primitive/seed node - that node's value."""
    if isinstance(value, list):
        target = _link_target(nodes, value)
        if target:
            inputs = target.get("inputs") or {}
            for key in ("value", "int", "float", "number", "seed"):
                if key in inputs and not isinstance(inputs[key], list):
                    return inputs[key]
        return None
    return value


def _text_of(nodes: Dict[str, Any], link: Any) -> Optional[str]:
    target = _link_target(nodes, link)
    text = ((target or {}).get("inputs") or {}).get("text")
    return text if isinstance(text, str) else None


def _trace_latent(nodes: Dict[str, Any], link: Any, params: Dict[str, Any]) -> None:
    """Walk back from the sampler's latent input to learn txt2img vs img2img, size, batch and source image."""
    node = _link_target(nodes, link)
    for _ in range(6):
        if not node:
            return
        cls, inputs = node.get("class_type"), node.get("inputs") or {}
        if cls == "EmptyLatentImage":
            params["mode"] = "txt2img"
            params["width"] = _scalar(nodes, inputs.get("width"))
            params["height"] = _scalar(nodes, inputs.get("height"))
            params.setdefault("batch_size", _scalar(nodes, inputs.get("batch_size")))
            return
        if cls == "RepeatLatentBatch":
            params["batch_size"] = _scalar(nodes, inputs.get("amount"))
            node = _link_target(nodes, inputs.get("samples"))
        elif cls in ("VAEEncode", "VAEEncodeForInpaint"):
            params["mode"] = "img2img"
            node = _link_target(nodes, inputs.get("pixels"))
        elif cls in ("LoadImage", "LoadImageOutput"):
            params["mode"] = "img2img"
            params["source_image"] = inputs.get("image")
            params.setdefault("batch_size", 1)
            return
        else:
            return


# Must match SOURCE_IMAGE_NODE_ID in comfy_sdxl_graph.py exactly. Every run_workflow submission
# gets this reserved node id injected into its graph (see that file's prepare_graph()), and no
# generate_image graph ever contains it, so its presence alone reliably tells "graph"-tool jobs
# apart from "direct"-tool jobs from the graph itself - no DB lookup needed, works for a live
# history entry's graph just as well as a stored one.
GRAPH_TOOL_MARKER_NODE_ID = "__comfy_tool_source_image__"


def classify_job_mode(graph: Optional[Dict[str, Any]]) -> Optional[str]:
    """'direct' (comfy_sdxl_direct.py's fixed graph) or 'graph' (comfy_sdxl_graph.py's
    model-authored graph); None if graph is missing/unreadable."""
    if not isinstance(graph, dict):
        return None
    return "graph" if GRAPH_TOOL_MARKER_NODE_ID in graph else "direct"


def extract_parameters(graph: Any) -> Dict[str, Any]:
    """
    Pull the generation settings out of a ComfyUI API-format graph: seed, steps,
    cfg, sampler, scheduler, denoise, prompts, checkpoint, LoRAs, size/batch and
    (for img2img) the source image. Best effort - never raises, omits anything
    it can't find.
    """
    if not isinstance(graph, dict):
        return {}
    try:
        nodes = {str(k): v for k, v in graph.items() if isinstance(v, dict)}
        ordered = sorted(nodes.items(), key=lambda kv: _natural_key(kv[0]))
        params: Dict[str, Any] = {}

        samplers = [n for _, n in ordered if n.get("class_type") in ("KSampler", "KSamplerAdvanced")]
        if samplers:
            inp = samplers[0].get("inputs") or {}
            seed = inp.get("seed", inp.get("noise_seed"))
            params.update(
                {
                    "seed": _scalar(nodes, seed),
                    "steps": _scalar(nodes, inp.get("steps")),
                    "cfg": _scalar(nodes, inp.get("cfg")),
                    "sampler_name": _scalar(nodes, inp.get("sampler_name")),
                    "scheduler": _scalar(nodes, inp.get("scheduler")),
                    "denoise": _scalar(nodes, inp.get("denoise")),
                    "positive_prompt": _text_of(nodes, inp.get("positive")),
                    "negative_prompt": _text_of(nodes, inp.get("negative")),
                }
            )
            _trace_latent(nodes, inp.get("latent_image"), params)
            if len(samplers) > 1:
                params["additional_samplers"] = len(samplers) - 1

        for _, n in ordered:
            ckpt = (n.get("inputs") or {}).get("ckpt_name")
            if n.get("class_type") in _CKPT_CLASSES and ckpt:
                params["checkpoint"] = ckpt
                break

        loras: List[Dict[str, Any]] = []
        for _, n in ordered:
            cls, inp = n.get("class_type"), n.get("inputs") or {}
            if cls == "Power Lora Loader (rgthree)":
                for key, val in inp.items():
                    if key.startswith("lora_") and isinstance(val, dict) and val.get("lora") and val.get("on", True):
                        loras.append({"lora": val["lora"], "strength": val.get("strength")})
            elif cls in ("LoraLoader", "LoraLoaderModelOnly") and inp.get("lora_name"):
                loras.append({"lora": inp["lora_name"], "strength": inp.get("strength_model")})
        if loras:
            params["loras"] = loras

        return {k: v for k, v in params.items() if v is not None}
    except Exception as e:  # parameters are a bonus, never a reason to fail the lookup
        logger.debug("Could not extract parameters: %s", e)
        return {}


def read_png_text_chunks(data: bytes, max_text: int = 4_000_000) -> Dict[str, str]:
    """Minimal PNG reader for the text chunks ComfyUI writes ('prompt' and 'workflow')."""
    out: Dict[str, str] = {}
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return out
    pos = 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length  # length field + type + body + CRC
        if ctype == b"IEND":
            break
        try:
            if ctype == b"tEXt":
                key, _, value = body.partition(b"\x00")
                out[key.decode("latin-1")] = value.decode("latin-1")
            elif ctype == b"zTXt":
                key, _, rest = body.partition(b"\x00")
                out[key.decode("latin-1")] = zlib.decompressobj().decompress(rest[1:], max_text).decode("latin-1")
            elif ctype == b"iTXt":
                key, _, rest = body.partition(b"\x00")
                compressed = rest[0]
                _language, _, rest = rest[2:].partition(b"\x00")
                _translated, _, text = rest.partition(b"\x00")
                if compressed:
                    text = zlib.decompressobj().decompress(text, max_text)
                out[key.decode("latin-1")] = text.decode("utf-8", "replace")
        except (zlib.error, IndexError, UnicodeDecodeError):
            continue
    return out


def png_prompt_graph(data: bytes) -> Optional[Dict[str, Any]]:
    raw = read_png_text_chunks(data).get("prompt")
    if not raw:
        return None
    try:
        graph = json.loads(raw)
    except ValueError:
        return None
    return graph if isinstance(graph, dict) else None


def history_prompt_graph(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """History stores the submission as [number, prompt_id, graph, extra_data, outputs]."""
    prompt = entry.get("prompt")
    if isinstance(prompt, (list, tuple)) and len(prompt) > 2 and isinstance(prompt[2], dict):
        return prompt[2]
    return None


def queue_item_graph(queue: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    for item in list(queue.get("queue_running") or []) + list(queue.get("queue_pending") or []):
        if _queue_item_id(item) == job_id and isinstance(item, (list, tuple)) and len(item) > 2 and isinstance(item[2], dict):
            return item[2]
    return None


def parameters_result(graph: Any, source: str) -> Dict[str, Any]:
    """{'parameters': {...}} to merge into a result, or {} if nothing could be read."""
    params = extract_parameters(graph)
    return {"parameters": {**params, "source": source}} if params else {}


def _job_number(entry: Dict[str, Any]) -> int:
    prompt = entry.get("prompt")
    if isinstance(prompt, (list, tuple)) and prompt and isinstance(prompt[0], int):
        return prompt[0]
    return -1


def _filenames_from_history_entry(entry: Dict[str, Any]) -> List[str]:
    images = [
        img
        for out in (entry.get("outputs") or {}).values()
        for img in (out.get("images") or [])
        if img.get("type", "output") == "output"
    ]
    return [f"{img['subfolder']}/{img['filename']}" if img.get("subfolder") else img["filename"] for img in images]


def summarize_history_entry(job_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    run_status = entry.get("status") or {}
    return {
        "job_id": job_id,
        "queue_number": _job_number(entry),
        "status": "failed" if run_status.get("status_str") == "error" else "complete",
        "filenames": _filenames_from_history_entry(entry),
    }


# --------------------------------------------------------------------------- #
# search_jobs - a read-only search over the shared job database. Never writes; if JOB_DB_PATH
# doesn't exist on disk this returns an empty result without creating one (job_db_connect's
# CREATE TABLE IF NOT EXISTS would otherwise do that as a side effect of a pure search).
# --------------------------------------------------------------------------- #

_KNOWN_JOB_STATUSES = {"built", "queued", "completed", "rejected", "failed"}
_CKPT_PARAM_CLASSES = ("CheckpointLoaderSimple", "CheckpointLoader", "unCLIPCheckpointLoader")
_SEED_PARAM_CLASSES = ("KSampler", "KSamplerAdvanced")


def fts_phrase_query(text: str) -> str:
    """Wrap free text as an FTS5 phrase query so punctuation in a prompt (quotes, *, :, -) can't
    be parsed as FTS5 query syntax and raise."""
    return '"' + text.replace('"', '""') + '"'


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pad_date_to_end_of_day(value: str) -> str:
    """A bare date like '2026-09-01' used as an upper bound should include that whole day."""
    return value if "T" in value else value + "T23:59:59.999999"


def run_job_search(
    db_path: str,
    *,
    prompt_text: Optional[str] = None,
    checkpoint: Optional[str] = None,
    seed: Optional[int] = None,
    server: Optional[str] = None,
    tool: Optional[str] = None,
    status: Optional[List[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    job_search: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """Synchronous; call via asyncio.to_thread. Never raises - returns {'success': False, 'error': ...}."""
    try:
        conn = job_db_connect(db_path)
    except Exception as e:
        return {"success": False, "error": f"Could not open job database at {db_path}: {e}"}
    try:
        conn.row_factory = sqlite3.Row
        conditions: List[str] = []
        params: List[Any] = []

        if tool:
            conditions.append("j.tool = ?")
            params.append(tool)
        if server:
            conditions.append("j.server = ?")
            params.append(server)
        if status:
            known = [s for s in status if s in _KNOWN_JOB_STATUSES]
            if known:
                conditions.append(f"j.status IN ({','.join('?' for _ in known)})")
                params.extend(known)
        if date_from:
            conditions.append("j.created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("j.created_at <= ?")
            params.append(_pad_date_to_end_of_day(date_to))
        if checkpoint:
            conditions.append(
                "EXISTS (SELECT 1 FROM node_params np WHERE np.job_uuid = j.job_uuid "
                f"AND np.class_type IN ({','.join('?' for _ in _CKPT_PARAM_CLASSES)}) "
                "AND np.param_name = 'ckpt_name' AND np.value_text LIKE ? ESCAPE '\\')"
            )
            params.extend(_CKPT_PARAM_CLASSES)
            params.append(f"%{_like_escape(checkpoint)}%")
        if seed is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM node_params np WHERE np.job_uuid = j.job_uuid "
                f"AND np.class_type IN ({','.join('?' for _ in _SEED_PARAM_CLASSES)}) "
                "AND np.param_name IN ('seed','noise_seed') AND np.value_num = ?)"
            )
            params.extend(_SEED_PARAM_CLASSES)
            params.append(float(seed))
        if prompt_text:
            if has_prompts_fts(conn):
                conditions.append("j.job_uuid IN (SELECT job_uuid FROM prompts_fts WHERE prompts_fts MATCH ?)")
                params.append(fts_phrase_query(prompt_text))
            else:
                escaped = _like_escape(prompt_text)
                conditions.append(
                    "j.job_uuid IN (SELECT job_uuid FROM prompts WHERE "
                    "positive_prompt LIKE ? ESCAPE '\\' OR negative_prompt LIKE ? ESCAPE '\\')"
                )
                params.extend([f"%{escaped}%", f"%{escaped}%"])
        if job_search:
            escaped = _like_escape(job_search)
            conditions.append(
                "(j.job_uuid LIKE ? ESCAPE '\\' OR j.comfy_prompt_id LIKE ? ESCAPE '\\' OR EXISTS ("
                "SELECT 1 FROM results r WHERE r.job_uuid = j.job_uuid AND r.stage = 'history' "
                "AND r.raw_json LIKE ? ESCAPE '\\'))"
            )
            params.extend([f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"])

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        total_count = conn.execute(f"SELECT COUNT(*) FROM jobs j {where}", params).fetchone()[0]

        sql = (
            "SELECT j.job_uuid, j.comfy_prompt_id, j.tool, j.server, j.status, j.created_at, "
            "j.submitted_at, j.duration_s, p.positive_prompt, p.negative_prompt "
            "FROM jobs j LEFT JOIN prompts p ON p.job_uuid = j.job_uuid "
            f"{where} ORDER BY j.created_at DESC LIMIT ? OFFSET ?"
        )
        page_params = params + [max(1, limit), max(0, offset)]
        rows = conn.execute(sql, page_params).fetchall()

        completed_uuids = [r["job_uuid"] for r in rows if r["status"] == "completed"]
        filenames_by_job: Dict[str, List[str]] = {}
        if completed_uuids:
            placeholders = ",".join("?" for _ in completed_uuids)
            hist_rows = conn.execute(
                f"SELECT job_uuid, raw_json FROM results WHERE stage = 'history' AND job_uuid IN ({placeholders})",
                completed_uuids,
            ).fetchall()
            for hr in hist_rows:
                try:
                    entry = json.loads(hr["raw_json"])
                except ValueError:
                    continue
                filenames_by_job[hr["job_uuid"]] = _filenames_from_history_entry(entry)

        # img_type/job_mode are small derived strings, not the graph itself - keeping the full
        # graph JSON out of search_jobs' response matters, it's meant to stay a lean finder (use
        # retrieve_graph to actually fetch a graph).
        all_uuids = [r["job_uuid"] for r in rows]
        classification_by_job: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        if all_uuids:
            placeholders = ",".join("?" for _ in all_uuids)
            out_rows = conn.execute(
                f"SELECT job_uuid, raw_json FROM outputs WHERE job_uuid IN ({placeholders})", all_uuids
            ).fetchall()
            for orow in out_rows:
                try:
                    graph = json.loads(orow["raw_json"])
                except ValueError:
                    continue
                classification_by_job[orow["job_uuid"]] = (extract_parameters(graph).get("mode"), classify_job_mode(graph))

        jobs = []
        for r in rows:
            img_type, job_mode = classification_by_job.get(r["job_uuid"], (None, None))
            item = {
                "job_uuid": r["job_uuid"],
                "comfy_prompt_id": r["comfy_prompt_id"],
                "tool": r["tool"],
                "server": r["server"],
                "status": r["status"],
                "created_at": r["created_at"],
                "submitted_at": r["submitted_at"],
                "duration_s": r["duration_s"],
                "positive_prompt": r["positive_prompt"],
                "negative_prompt": r["negative_prompt"],
                "img_type": img_type,
                "job_mode": job_mode,
            }
            if r["status"] == "completed":
                item["filenames"] = filenames_by_job.get(r["job_uuid"], [])
            jobs.append(item)

        return {"success": True, "count": len(jobs), "total_count": total_count, "jobs": jobs}
    except Exception as e:
        logger.exception("search_jobs query failed")
        return {"success": False, "error": f"Search failed: {e}"}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Reconciliation - opportunistically backfill a job's comfy_prompt_id when retrieve_image finds
# its image by filename but the tool that rendered it never recorded the link itself (crash or
# dropped connection between submitting and recording). Lean, best-effort, never raises out of
# reconcile_job_for_file: a bookkeeping failure must never break a successful image retrieval.
# --------------------------------------------------------------------------- #

_RECONCILE_AMBIGUITY_WINDOW_S = 5.0


def any_unlinked_jobs(db_path: str, server: str) -> bool:
    """Cheap existence check so the common case (the file's job was already linked normally)
    never pays for a /history scan."""
    try:
        conn = job_db_connect(db_path)
    except Exception:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE server = ? AND comfy_prompt_id IS NULL LIMIT 1", (server,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def find_history_entry_by_filename(
    client: "ComfyClient", subfolder: str, filename: str, max_items: int
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Scan the server's recent /history for the entry whose outputs include this file. Returns
    (comfy_prompt_id, entry) or None."""
    raw = client.get_history_list(max_items)
    for job_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        for out in (entry.get("outputs") or {}).values():
            for img in out.get("images") or []:
                if (
                    img.get("type", "output") == "output"
                    and img.get("filename") == filename
                    and img.get("subfolder", "") == subfolder
                ):
                    return job_id, entry
    return None


def find_unlinked_job_candidates(db_path: str, server: str, graph: Dict[str, Any]) -> List[sqlite3.Row]:
    """
    Jobs on `server` with comfy_prompt_id IS NULL whose submitted graph (outputs.raw_json)
    matches `graph` exactly. comfy_prompt_id IS NULL alone is a sufficient filter, with no extra
    status condition needed: that column is only ever set by record_job_submitted, so 'built'
    (crashed before submit) and 'rejected' (never got a real prompt_id) are exactly the unlinked
    set; 'queued'/'completed'/'failed' jobs all already have it set.
    """
    conn = job_db_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT j.job_uuid, j.created_at, o.raw_json FROM jobs j "
            "JOIN outputs o ON o.job_uuid = j.job_uuid "
            "WHERE j.server = ? AND j.comfy_prompt_id IS NULL",
            (server,),
        ).fetchall()
    finally:
        conn.close()
    matches = []
    for row in rows:
        try:
            if json.loads(row["raw_json"]) == graph:
                matches.append(row)
        except ValueError:
            continue
    return matches


def resolve_reconciliation_match(
    candidates: List[sqlite3.Row], execution_start_ts_ms: Optional[int]
) -> Dict[str, Any]:
    """
    candidates is non-empty. Always returns a job_uuid to link - ambiguity is reported, not
    treated as 'give up': seeds (and whole graphs) get deliberately reused across jobs, so more
    than one candidate matching is expected sometimes, not necessarily a sign something is wrong.
    """
    if len(candidates) == 1:
        return {"job_uuid": candidates[0]["job_uuid"], "ambiguous": False}

    if execution_start_ts_ms is None:
        ordered = sorted(candidates, key=lambda r: r["created_at"])
        return {
            "job_uuid": ordered[0]["job_uuid"],
            "ambiguous": True,
            "note": (
                f"{len(candidates)} unlinked jobs on this server have the exact same submitted graph "
                "and no execution-start timestamp was available to disambiguate; picked the oldest as "
                "most likely, but can't be certain."
            ),
        }

    exec_dt = datetime.datetime.fromtimestamp(execution_start_ts_ms / 1000, tz=datetime.timezone.utc)

    def _delta_s(row: sqlite3.Row) -> float:
        return abs((datetime.datetime.fromisoformat(row["created_at"]) - exec_dt).total_seconds())

    ordered = sorted(candidates, key=_delta_s)
    best, runner_up = ordered[0], ordered[1]
    if abs(_delta_s(best) - _delta_s(runner_up)) < _RECONCILE_AMBIGUITY_WINDOW_S:
        return {
            "job_uuid": best["job_uuid"],
            "ambiguous": True,
            "note": (
                f"{len(candidates)} unlinked jobs match this graph; picked the one closest in time to "
                f"the render ({_delta_s(best):.1f}s away) as most likely, but another candidate is "
                f"nearly as close ({_delta_s(runner_up):.1f}s), so this can't be certain."
            ),
        }
    return {"job_uuid": best["job_uuid"], "ambiguous": False}


def record_job_reconciled(db_path: str, job_uuid: str, comfy_prompt_id: str, history_entry: Dict[str, Any]) -> None:
    """
    Reconciliation's own write: backfill the comfy_prompt_id that was never recorded, then reuse
    record_job_completed verbatim for status/timing/results - it already does exactly the right
    thing for everything except the id itself, which only record_job_submitted normally sets.
    """
    try:
        conn = job_db_connect(db_path)
        try:
            conn.execute(
                "UPDATE jobs SET comfy_prompt_id = ? WHERE job_uuid = ? AND comfy_prompt_id IS NULL",
                (comfy_prompt_id, job_uuid),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Job DB: could not backfill comfy_prompt_id for %s: %s", job_uuid, e)
    record_job_completed(db_path, job_uuid, history_entry)


def reconcile_job_for_file(
    v: Any, client: "ComfyClient", server: str, subfolder: str, filename: str
) -> Optional[Dict[str, Any]]:
    """
    Best-effort attempt to backfill a job row that's missing comfy_prompt_id, using the file that
    retrieve_image just found by filename on `server`. Returns None when there's nothing worth
    reporting (no unlinked jobs on this server, no history entry for this file, or no graph
    match) - the normal case, where the file's job was already properly linked. Returns a dict
    only when a job_uuid was actually resolved and linked. Never raises.
    """
    try:
        if not any_unlinked_jobs(v.JOB_DB_PATH, server):
            return None

        found = find_history_entry_by_filename(client, subfolder, filename, v.MAX_HISTORY_ITEMS)
        if not found:
            return None
        comfy_prompt_id, entry = found

        graph = history_prompt_graph(entry)
        if not graph:
            return None

        candidates = find_unlinked_job_candidates(v.JOB_DB_PATH, server, graph)
        if not candidates:
            return None

        start_ts, _end_ts, _duration = _execution_timing(entry)
        resolution = resolve_reconciliation_match(candidates, start_ts)

        record_job_reconciled(v.JOB_DB_PATH, resolution["job_uuid"], comfy_prompt_id, entry)
        result: Dict[str, Any] = {
            "matched": True,
            "job_uuid": resolution["job_uuid"],
            "comfy_prompt_id": comfy_prompt_id,
        }
        if resolution["ambiguous"]:
            result["ambiguous"] = True
            result["note"] = resolution["note"]
        return result
    except Exception as e:
        logger.warning("Reconciliation error for %s/%s on %s: %s", subfolder, filename, server, e)
        return None


async def list_recent_jobs(v: Any, server: str, count: int) -> Dict[str, Any]:
    """The last `count` jobs ComfyUI's history has for this server, newest first."""
    client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)
    raw = await asyncio.to_thread(client.get_history_list, count)
    jobs = [summarize_history_entry(job_id, entry) for job_id, entry in raw.items() if isinstance(entry, dict)]
    jobs.sort(key=lambda j: j["queue_number"], reverse=True)
    jobs = jobs[:count]
    return {
        "success": True,
        "server": server,
        "jobs": jobs,
        "note": (
            "Each entry has 'job_id' (pass to retrieve_image to fetch that image and its parameters) and "
            "'filenames'. This only covers jobs still in the server's history, which is cleared on restart "
            "and can drop old entries; it does not include anything currently queued or running."
        ),
    }


def format_execution_error(status: Dict[str, Any]) -> str:
    for name, data in status.get("messages", []):
        if name == "execution_error":
            return (
                f"{data.get('node_type', 'node')} {data.get('node_id', '?')}: "
                f"{data.get('exception_message', 'execution failed')}".strip()
            )
    return "ComfyUI reported an execution error."


# --------------------------------------------------------------------------- #
# ComfyUI / Open WebUI I/O
# --------------------------------------------------------------------------- #

class ComfyClient:
    def __init__(self, base_url: str, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def view_url(self, image: Dict[str, Any]) -> str:
        query = urllib.parse.urlencode(
            {
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
        )
        return f"{self.base_url}/view?{query}"

    def fetch_image(self, image: Dict[str, Any]) -> Optional[bytes]:
        """Image bytes, or None if the file isn't there (HTTP 404)."""
        try:
            r = requests.get(self.view_url(image), timeout=self.timeout)
        except RequestException as e:
            raise ComfyError(f"Cannot reach ComfyUI at {self.base_url}: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ComfyError(f"{self.base_url} answered HTTP {r.status_code} for {image['filename']}")
        return r.content

    def get_history(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(f"{self.base_url}/history/{job_id}", timeout=self.timeout)
            r.raise_for_status()
            return r.json().get(job_id)
        except (RequestException, ValueError, AttributeError) as e:
            raise ComfyError(f"Cannot read history from {self.base_url}: {e}") from e

    def get_queue(self) -> Dict[str, Any]:
        try:
            r = requests.get(f"{self.base_url}/queue", timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (RequestException, ValueError) as e:
            raise ComfyError(f"Cannot read the queue from {self.base_url}: {e}") from e
        return data if isinstance(data, dict) else {}

    def get_history_list(self, max_items: int) -> Dict[str, Any]:
        try:
            r = requests.get(f"{self.base_url}/history", params={"max_items": max_items}, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (RequestException, ValueError) as e:
            raise ComfyError(f"Cannot read history from {self.base_url}: {e}") from e
        return data if isinstance(data, dict) else {}


def upload_to_open_webui(
    base_url: str, api_key: str, image_bytes: bytes, filename: str, timeout: int
) -> "Tuple[Optional[str], Optional[str]]":
    """
    Store an image in Open WebUI's own file storage so the browser only ever
    needs to reach Open WebUI. Returns (relative_url, None) or (None, error_text).
    """
    if not api_key:
        return None, "OPEN_WEBUI_API_KEY is empty in the tool's valves"
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/v1/files/",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, image_bytes, mimetypes.guess_type(filename)[0] or "image/png")},
            # Images have no text to extract/embed; skip Open WebUI's RAG processing.
            params={"process": "false"},
            timeout=timeout,
        )
        resp.raise_for_status()
        file_id = resp.json().get("id")
        if not file_id:
            return None, f"upload response had no 'id' field: {str(resp.text)[:200]}"
        return f"/api/v1/files/{file_id}/content", None
    except RequestException as e:
        detail = ""
        response = getattr(e, "response", None)
        if response is not None:
            detail = f" (HTTP {response.status_code}: {str(response.text)[:200]})"
        return None, f"{e}{detail}"
    except ValueError as e:
        return None, f"unexpected response from Open WebUI: {e}"


async def _emit(emitter: Optional[Callable], payload: Dict[str, Any]) -> None:
    if emitter:
        await emitter(payload)


# --------------------------------------------------------------------------- #
# Delivery (same three methods as generate_image)
# --------------------------------------------------------------------------- #

async def deliver_images(
    v: Any,
    client: ComfyClient,
    server: str,
    images: List[Dict[str, Any]],
    tag: str,
    emitter: Optional[Callable],
    return_img_url: bool,
    prefetched: Optional[Dict[str, bytes]] = None,
) -> Dict[str, Any]:
    """
    Get the images in front of the user according to IMAGE_DELIVERY.
    Returns the fields to merge into the tool result.
    """
    prefetched = prefetched or {}
    images_out: List[Dict[str, Any]] = []
    chat_sources: List[str] = []
    warnings: List[str] = []

    for img in images:
        subfolder = img.get("subfolder", "")
        ref = f"{subfolder}/{img['filename']}" if subfolder else img["filename"]
        comfy_url = client.view_url(img)
        delivered_url, delivery = comfy_url, "direct_comfy_url"

        data = prefetched.get(ref)
        if data is None and (v.UPLOAD_TO_OPEN_WEBUI or v.SAVE_LOCAL_COPY):
            try:
                data = await asyncio.to_thread(client.fetch_image, img)
                if data is None:
                    warnings.append(f"{ref} disappeared from the server before it could be copied.")
            except ComfyError as e:  # don't fail the lookup over a delivery hiccup
                warnings.append(str(e))

        if data is not None and v.SAVE_LOCAL_COPY:
            out_dir = Path(v.OUTPUT_DIR)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{tag}_{img['filename']}").write_bytes(data)

        if data is not None and v.UPLOAD_TO_OPEN_WEBUI:
            path, err = await asyncio.to_thread(
                upload_to_open_webui,
                v.OPEN_WEBUI_BASE_URL,
                v.OPEN_WEBUI_API_KEY,
                data,
                img["filename"],
                v.REQUEST_TIMEOUT_SECONDS,
            )
            if path:
                delivered_url, delivery = path, "openwebui_upload"
            else:
                logger.warning("Open WebUI upload failed: %s", err)
                warnings.append(
                    f"Open WebUI upload failed ({err}); using a direct ComfyUI link, "
                    f"which only displays if the browser can reach {server}."
                )

        chat_sources.append(delivered_url)
        entry: Dict[str, Any] = {
            "filename": img["filename"],
            "subfolder": subfolder,
            "delivery": delivery,
            "use_as_source_image": ref,
        }
        if return_img_url:
            entry["comfy_url"] = comfy_url
            entry["chat_url"] = delivered_url
            entry["link_markdown"] = f"[{img['filename']}]({delivered_url})"
        images_out.append(entry)

    # Event delivery needs an emitter; without one we fall back to the model embedding it.
    emitted = v.IMAGE_DELIVERY != "model_embed" and bool(emitter)
    if emitted:
        if v.IMAGE_DELIVERY == "files_event":
            payload = {"type": "files", "data": {"files": [{"type": "image", "url": u} for u in chat_sources]}}
        else:
            md = "\n\n".join(f"![image]({u})" for u in chat_sources)
            payload = {"type": "message", "data": {"content": md + "\n\n"}}
        await _emit(emitter, payload)

    result: Dict[str, Any] = {
        "images": images_out,
        "displayed_via": v.IMAGE_DELIVERY if emitted or v.IMAGE_DELIVERY == "model_embed" else "none",
    }
    note = (
        "Images are already displayed to the user; don't embed them again. "
        if emitted
        else "Include image_markdown exactly as given in your reply so the user can see the image. "
    )
    note += f"To refine an image, call generate_image with source_image=<use_as_source_image> and gpu_server={server}."
    if return_img_url:
        note += (
            " To show a link to the user, paste link_markdown exactly as given so it renders as a "
            "clickable link; don't put URLs in backticks or code blocks."
        )
    result["note"] = note
    if not emitted:
        result["image_markdown"] = "\n\n".join(f"![image]({u})" for u in chat_sources)
    if warnings:
        result["warnings"] = list(dict.fromkeys(warnings))
    return result


# --------------------------------------------------------------------------- #
# Lookup logic
# --------------------------------------------------------------------------- #

def _servers_note(servers: List[str], unreachable: Dict[str, str]) -> str:
    text = f"Searched: {servers}."
    if unreachable:
        text += " Could not be reached or read: " + "; ".join(f"{u} ({e})" for u, e in unreachable.items()) + "."
    return text


async def retrieve(
    v: Any,
    job_id_or_filename: Optional[str],
    gpu_server: Optional[str],
    return_img_url: bool,
    emitter: Optional[Callable],
    status: Callable[..., Any],
    history: Optional[int] = None,
) -> Dict[str, Any]:
    # ---- interpret the input --------------------------------------------
    try:
        text = (job_id_or_filename or "").strip()
        default = v.DEFAULT_GPU_SERVER.rstrip("/")
        allowed = list(dict.fromkeys(list(v.GPU_SERVERS) + [v.DEFAULT_GPU_SERVER]))

        if is_unset(text):
            if history is None:
                raise ValueError(
                    "Provide a job id (UUID) or an image filename. To list recent jobs instead, "
                    "give history=<count> and gpu_server=<url>."
                )
            if is_unset(gpu_server):
                raise ValueError("Listing recent jobs needs a specific gpu_server (it isn't searched across servers).")
            if not isinstance(history, int) or history < 1:
                raise ValueError("history must be a positive integer.")
            server = resolve_server(gpu_server, default, allowed, v.ALLOW_UNLISTED_SERVERS)
            await status(f"Listing the last {history} job(s) on {server}...")
            return await list_recent_jobs(v, server, min(history, v.MAX_HISTORY_ITEMS))

        if is_unset(gpu_server):  # no server named: search the default first, then the rest
            servers = [default] + [s.rstrip("/") for s in allowed if s.rstrip("/") != default]
        else:
            servers = [resolve_server(gpu_server, default, allowed, v.ALLOW_UNLISTED_SERVERS)]

        by_job = bool(_UUID_RE.match(text))
        subfolder, filename = ("", "") if by_job else parse_image_ref(text)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    unreachable: Dict[str, str] = {}

    # ---- by job id -------------------------------------------------------
    if by_job:
        job_id = text.lower()
        for server in servers:
            client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)
            await status(f"Looking up job {job_id[:8]} on {server}...")
            try:
                entry = await asyncio.to_thread(client.get_history, job_id)
                queue = None if entry else await asyncio.to_thread(client.get_queue)
            except ComfyError as e:
                unreachable[server] = str(e)
                continue

            if entry:  # finished (or failed)
                run_status = entry.get("status") or {}
                if run_status.get("status_str") == "error":
                    return {
                        "success": False,
                        "found": True,
                        "ready": False,
                        "status": "failed",
                        "job_id": job_id,
                        "server": server,
                        "error": f"The job failed on the server: {format_execution_error(run_status)}",
                    }
                images = [
                    img
                    for out in (entry.get("outputs") or {}).values()
                    for img in (out.get("images") or [])
                    if img.get("type", "output") == "output"
                ]
                if not images:
                    return {
                        "success": False,
                        "found": True,
                        "ready": False,
                        "status": "finished_without_images",
                        "job_id": job_id,
                        "server": server,
                        "error": "The job finished but produced no images.",
                    }
                delivered = await deliver_images(
                    v, client, server, images, job_id[:8], emitter, return_img_url
                )
                return {
                    "success": True,
                    "found": True,
                    "ready": True,
                    "status": "complete",
                    "job_id": job_id,
                    "server": server,
                    **parameters_result(history_prompt_graph(entry), "server_history"),
                    **delivered,
                }

            snap = queue_snapshot(queue or {}, job_id)
            if snap:
                state = snap.pop("state")
                where = (
                    "is rendering now"
                    if state == "running"
                    else f"is waiting in the queue, position {snap.get('position_in_queue')} "
                    f"with {snap['jobs_ahead']} job(s) ahead of it"
                )
                return {
                    "success": True,
                    "found": True,
                    "ready": False,
                    "status": state,
                    "job_id": job_id,
                    "server": server,
                    "queue": snap,
                    **parameters_result(queue_item_graph(queue or {}, job_id), "queue"),
                    "note": f"Job {where}. No image exists yet. Call retrieve_image again later with the same job id.",
                }

        return {
            "success": False,
            "found": False,
            "error": (
                f"Job {job_id} was not found: it is not queued or running, and it is not in the server's "
                "history. History is cleared when ComfyUI restarts and old entries are dropped, so it may "
                "have expired, or the job id or server may be wrong. " + _servers_note(servers, unreachable)
            ),
        }

    # ---- by filename -----------------------------------------------------
    ref = f"{subfolder}/{filename}" if subfolder else filename
    image = {"filename": filename, "subfolder": subfolder, "type": "output"}
    for server in servers:
        client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)
        await status(f"Looking for {ref} on {server}...")
        try:
            data = await asyncio.to_thread(client.fetch_image, image)
        except ComfyError as e:
            unreachable[server] = str(e)
            continue
        if data is None:
            continue

        recon: Optional[Dict[str, Any]] = None
        if v.LOG_TO_SQLITE:
            try:
                recon = await asyncio.to_thread(reconcile_job_for_file, v, client, server, subfolder, filename)
            except Exception as e:  # bookkeeping must never break a successful retrieval
                logger.warning("Reconciliation failed for %s on %s: %s", ref, server, e)

        delivered = await deliver_images(
            v, client, server, [image], "retrieved", emitter, return_img_url, prefetched={ref: data}
        )
        meta = parameters_result(png_prompt_graph(data), "png_metadata") or {
            "parameters_note": "No generation metadata was found in this file (it may have been saved with metadata disabled, or edited), so its settings are unknown."
        }
        return {
            "success": True,
            "found": True,
            "ready": True,
            "status": "complete",
            "server": server,
            **meta,
            **delivered,
            **({"reconciliation": recon} if recon else {}),
        }

    # Not there. If jobs that would write this name are still queued, say so.
    hints: List[str] = []
    for server in servers:
        if server in unreachable:
            continue
        try:
            queue = await asyncio.to_thread(ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS).get_queue)
        except ComfyError:
            continue
        n = count_jobs_writing_prefix(queue, ref)
        if n:
            hints.append(
                f"{n} queued/running job(s) on {server} save under a matching filename prefix "
                f"({len(queue.get('queue_running') or [])} running, {len(queue.get('queue_pending') or [])} pending), "
                "so the file may simply not exist yet."
            )
    return {
        "success": False,
        "found": False,
        "error": f"No file {ref!r} exists in the output folder. {_servers_note(servers, unreachable)}"
        + ((" " + " ".join(hints)) if hints else ""),
    }


# --------------------------------------------------------------------------- #
# list_jobs - a browsable, formatted-for-display job library, distinct from search_jobs (which
# returns structured JSON for filtering/chaining). Two data sources, normalized to the same
# display-row shape so one table formatter covers both:
#   - live (default, or an explicit GPU server address): reads straight from that server's own
#     /history, same data retrieve_image's history=<count> mode uses. No search filters - "no
#     need to do any searching, maintain existing functionality" was the explicit steer here.
#   - database: browses the persistent job log via run_job_search, completed jobs only, with
#     job_search/prompt_search available.
# --------------------------------------------------------------------------- #


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _format_ts_for_display(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return datetime.datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def _live_rows_from_history(raw: Dict[str, Any], server: str) -> List[Dict[str, Any]]:
    """Normalize a server's raw /history dict into display rows, newest first."""
    entries = [(job_id, entry) for job_id, entry in raw.items() if isinstance(entry, dict)]
    entries.sort(key=lambda kv: _job_number(kv[1]), reverse=True)

    rows = []
    for job_id, entry in entries:
        run_status = entry.get("status") or {}
        images = [
            img
            for out in (entry.get("outputs") or {}).values()
            for img in (out.get("images") or [])
            if img.get("type", "output") == "output"
        ]
        first = images[0] if images else None
        start_ts, _end_ts, _duration = _execution_timing(entry)
        created_at = (
            datetime.datetime.fromtimestamp(start_ts / 1000, tz=datetime.timezone.utc).isoformat()
            if start_ts is not None
            else None
        )
        graph = history_prompt_graph(entry)
        params = extract_parameters(graph) if graph else {}
        rows.append(
            {
                "id": job_id,
                "created_at": created_at,
                "server": server,
                "status": "failed" if run_status.get("status_str") == "error" else "completed",
                "filename": first.get("filename") if first else None,
                "subfolder": first.get("subfolder", "") if first else "",
                "positive_prompt": params.get("positive_prompt"),
                "negative_prompt": params.get("negative_prompt"),
                "img_type": params.get("mode"),  # 'txt2img' / 'img2img' - extract_parameters' own "mode" key
                "job_mode": classify_job_mode(graph),  # 'direct' / 'graph' - which tool made it
            }
        )
    return rows


def _db_rows_to_display_rows(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize run_job_search's job dicts (already completed-only, by construction here) into
    the same display-row shape _live_rows_from_history produces."""
    rows = []
    for job in jobs:
        filenames = job.get("filenames") or []
        subfolder, filename = "", None
        if filenames:
            try:
                subfolder, filename = parse_image_ref(filenames[0])
            except ValueError:
                filename = filenames[0]  # malformed ref (shouldn't happen); show it verbatim rather than drop the row
        rows.append(
            {
                "id": job["job_uuid"],
                "created_at": job.get("created_at"),
                "server": job.get("server"),
                "status": job.get("status"),
                "filename": filename,
                "subfolder": subfolder,
                "positive_prompt": job.get("positive_prompt"),
                "negative_prompt": job.get("negative_prompt"),
                "img_type": job.get("img_type"),
                "job_mode": job.get("job_mode"),
            }
        )
    return rows


def format_jobs_table(
    rows: List[Dict[str, Any]], start_index: int, total_count: int, link_images: bool, request_timeout: int
) -> str:
    """
    '# | UUID | Job Timestamp | Information' - everything else (filename, server, status,
    prompts) lives inside one tall Information cell rather than more columns, since chat windows
    are narrow and Markdown tables don't wrap gracefully.

    Filenames are a plain Markdown link to the full image, not an embedded '![]()' image: OWUI
    escapes raw HTML (so a width/style size hint on an <img> never survives to render) and plain
    Markdown has no image-sizing syntax either, so an embedded image always shows at full size.
    A link avoids dumping a full-size image inline for every row while still getting there in
    one click - and unlike an image tag, link text/attributes aren't touched by that escaping.
    """
    if not rows:
        return "No jobs found."

    lines = [
        f"Showing {start_index + 1}-{start_index + len(rows)} of {total_count}",
        "",
        "| # | UUID | Job Timestamp | Information |",
        "|---|------|----------------|-------------|",
    ]
    for i, row in enumerate(rows):
        parts: List[str] = []
        filename = row.get("filename")
        if filename and link_images and row.get("server"):
            image_url = ComfyClient(row["server"], request_timeout).view_url(
                {"filename": filename, "subfolder": row.get("subfolder", ""), "type": "output"}
            )
            parts.append(f"**File:** [{filename}]({image_url})")
        else:
            parts.append(f"**File:** {filename}" if filename else "**File:** (none)")
        parts.append(f"**Server:** {row.get('server') or '—'}")
        parts.append(f"**Status:** {row.get('status') or 'unknown'}")
        parts.append(f"**Type:** {row.get('img_type') or 'unknown'}, **Mode:** {row.get('job_mode') or 'unknown'}")
        if row.get("positive_prompt"):
            parts.append(f"**Positive:** {_truncate(row['positive_prompt'], 200)}")
        if row.get("negative_prompt"):
            parts.append(f"**Negative:** {_truncate(row['negative_prompt'], 200)}")

        # A table cell can't contain a literal newline or an unescaped '|' without breaking the
        # row, so both are neutralized here rather than relied on to never occur in prompt text.
        information = "<br>".join(parts).replace("\n", " ").replace("|", "\\|")
        uuid_cell = (row.get("id") or "—").replace("|", "\\|")
        lines.append(f"| {start_index + i + 1} | {uuid_cell} | {_format_ts_for_display(row.get('created_at'))} | {information} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# retrieve_graph - fetches the submitted ComfyUI graph itself (the input to run_workflow), not
# the rendered image. Most useful for "graph"-mode jobs, where the graph is bespoke and worth
# pulling back up to inspect or resubmit (with edits); works for "direct"-mode jobs too, though
# their fixed graph shape is already documented in comfy_sdxl_direct.py.
# --------------------------------------------------------------------------- #


async def find_graph_live(
    v: Any, job_id_or_filename: str, gpu_server: Optional[str], status: Callable[..., Any]
) -> Dict[str, Any]:
    """Mirrors retrieve()'s by-job-id-or-filename server search, but returns the graph instead of
    delivering an image. A job still queued/running has a graph too (queue_item_graph), so this
    doesn't require the job to have finished."""
    try:
        text = job_id_or_filename.strip()
        default = v.DEFAULT_GPU_SERVER.rstrip("/")
        allowed = list(dict.fromkeys(list(v.GPU_SERVERS) + [v.DEFAULT_GPU_SERVER]))
        if is_unset(gpu_server):
            servers = [default] + [s.rstrip("/") for s in allowed if s.rstrip("/") != default]
        else:
            servers = [resolve_server(gpu_server, default, allowed, v.ALLOW_UNLISTED_SERVERS)]
        by_job = bool(_UUID_RE.match(text))
        subfolder, filename = ("", "") if by_job else parse_image_ref(text)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    unreachable: Dict[str, str] = {}

    if by_job:
        job_id = text.lower()
        for server in servers:
            client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)
            await status(f"Looking up job {job_id[:8]} on {server}...")
            try:
                entry = await asyncio.to_thread(client.get_history, job_id)
            except ComfyError as e:
                unreachable[server] = str(e)
                continue
            if entry:
                graph = history_prompt_graph(entry)
                if graph:
                    run_status = entry.get("status") or {}
                    job_status = "failed" if run_status.get("status_str") == "error" else "completed"
                    return {"success": True, "found": True, "graph": graph, "server": server, "comfy_prompt_id": job_id, "status": job_status}
            try:
                queue = await asyncio.to_thread(client.get_queue)
            except ComfyError as e:
                unreachable[server] = str(e)
                continue
            graph = queue_item_graph(queue, job_id)
            if graph:
                snap = queue_snapshot(queue, job_id) or {}
                return {"success": True, "found": True, "graph": graph, "server": server, "comfy_prompt_id": job_id, "status": snap.get("state", "queued")}
        return {
            "success": False,
            "found": False,
            "error": f"Job {job_id} was not found: not in history or the queue on any configured server. "
            + _servers_note(servers, unreachable),
        }

    for server in servers:
        client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)
        await status(f"Looking for the job that produced {filename} on {server}...")
        try:
            found = await asyncio.to_thread(find_history_entry_by_filename, client, subfolder, filename, v.MAX_HISTORY_ITEMS)
        except ComfyError as e:
            unreachable[server] = str(e)
            continue
        if found:
            comfy_prompt_id, entry = found
            graph = history_prompt_graph(entry)
            if graph:
                return {"success": True, "found": True, "graph": graph, "server": server, "comfy_prompt_id": comfy_prompt_id, "status": "completed"}

    ref = f"{subfolder}/{filename}" if subfolder else filename
    return {
        "success": False,
        "found": False,
        "error": f"No history entry producing {ref!r} was found on any configured server. " + _servers_note(servers, unreachable),
    }


def find_graph_in_db(db_path: str, job_id_or_filename: str) -> Dict[str, Any]:
    """job_uuid/comfy_prompt_id exact match, or filename substring within the stored history
    JSON. Never raises."""
    if not Path(db_path).exists():
        return {"success": False, "error": "No job database found yet at JOB_DB_PATH - nothing has been logged, or JOB_DB_PATH doesn't match generate_image's/run_workflow's valve."}
    try:
        conn = job_db_connect(db_path)
    except Exception as e:
        return {"success": False, "error": f"Could not open job database at {db_path}: {e}"}
    try:
        conn.row_factory = sqlite3.Row
        text = job_id_or_filename.strip()
        escaped = _like_escape(text)
        row = conn.execute(
            "SELECT j.job_uuid, j.comfy_prompt_id, j.server, j.status, o.raw_json FROM jobs j "
            "JOIN outputs o ON o.job_uuid = j.job_uuid WHERE j.job_uuid = ? OR j.comfy_prompt_id = ? "
            "OR EXISTS (SELECT 1 FROM results r WHERE r.job_uuid = j.job_uuid AND r.stage = 'history' "
            "AND r.raw_json LIKE ? ESCAPE '\\') ORDER BY j.created_at DESC LIMIT 1",
            (text, text, f"%{escaped}%"),
        ).fetchone()
        if not row:
            return {"success": False, "found": False, "error": f"No job matching {text!r} was found in the database."}
        try:
            graph = json.loads(row["raw_json"])
        except ValueError:
            return {"success": False, "error": "The stored graph JSON for this job was corrupt."}
        return {
            "success": True,
            "found": True,
            "graph": graph,
            "server": row["server"],
            "comfy_prompt_id": row["comfy_prompt_id"],
            "status": row["status"],
            "job_uuid": row["job_uuid"],
        }
    except Exception as e:
        logger.exception("find_graph_in_db query failed")
        return {"success": False, "error": f"Search failed: {e}"}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Open WebUI tool
# --------------------------------------------------------------------------- #

class Tools:
    class Valves(BaseModel):
        GPU_SERVERS: List[str] = Field(
            default=["http://192.168.10.11:8188", "http://192.168.10.12:8188"],
            description="ComfyUI servers to look on. Copy these from the generate_image tool's valves.",
        )
        DEFAULT_GPU_SERVER: str = Field(
            default="http://192.168.10.11:8188",
            description="Searched first when the model doesn't name a server.",
        )
        ALLOW_UNLISTED_SERVERS: bool = Field(
            default=True,
            description="Let the model target any well-formed ComfyUI address, not just the ones in GPU_SERVERS. Turn off to restrict the tool to the list (stops a model from making the Open WebUI backend send requests to arbitrary hosts).",
        )
        OUTPUT_DIR: str = Field(
            default="/app/backend/data/comfy_outputs",
            description="Where local copies are written when SAVE_LOCAL_COPY is on.",
        )
        SAVE_LOCAL_COPY: bool = Field(default=False, description="Also keep a copy of each retrieved image in OUTPUT_DIR.")
        UPLOAD_TO_OPEN_WEBUI: bool = Field(
            default=True,
            description="Upload each image into Open WebUI's own file storage and show it from there, so your browser never has to reach the GPU server. Needs OPEN_WEBUI_API_KEY. If the upload fails, falls back to a direct ComfyUI link.",
        )
        OPEN_WEBUI_BASE_URL: str = Field(
            default="http://localhost:8080",
            description="Address the tool uses to call Open WebUI's own API, from the backend's point of view.",
        )
        OPEN_WEBUI_API_KEY: str = Field(
            default="",
            description="Personal API key from Open WebUI (Settings -> Account -> API Keys). Set it here in the valve, not in the code.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(default=15, description="Per-HTTP-request timeout.")
        MAX_HISTORY_ITEMS: int = Field(default=50, description="Upper bound on how many jobs a history=<count> listing can return.")
        IMAGE_DELIVERY: Literal["model_embed", "files_event", "message_event"] = Field(
            default="model_embed",
            description=(
                "How the image reaches the chat. "
                "'model_embed': the result includes image_markdown and the model is told to put it in its reply. "
                "'files_event': the tool attaches the image to the message via an event; the model is told not to embed it. "
                "'message_event': the tool appends markdown to the message text via an event; this text can be overwritten when the model streams its reply."
            ),
        )
        LOG_TO_SQLITE: bool = Field(
            default=True,
            description=(
                "Gates this tool's WRITES to the shared job database: whether retrieve_image tries to "
                "backfill (reconcile) a job that's missing its comfy_prompt_id when it finds that job's "
                "image by filename. Unlike generate_image's/run_workflow's same-named valve, this does "
                "NOT gate search_jobs, which always reads JOB_DB_PATH if it exists - turn this off only "
                "to stop retrieve_image from ever modifying the database, not to hide it from search."
            ),
        )
        JOB_DB_PATH: str = Field(
            default="/app/backend/data/comfy_outputs/comfy_jobs.sqlite3",
            description="SQLite database file for the job index. MUST be set to the exact same path as generate_image's and run_workflow's JOB_DB_PATH valves, since all three read/write one shared file. Used by search_jobs (read) and by retrieve_image's reconciliation (write).",
        )
        MAX_SEARCH_RESULTS: int = Field(default=25, description="Upper bound on how many rows search_jobs can return, regardless of the caller's limit.")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def retrieve_image(
        self,
        job_id_or_filename: Optional[str] = None,
        gpu_server: Optional[str] = None,
        return_img_url: bool = False,
        history: Optional[int] = None,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve an image rendered earlier on the ComfyUI server, or list recent jobs.

        TOOLKIT: this is one of five related tools, all sharing the same GPU_SERVERS. Use
        generate_image for ordinary SDXL renders, or run_workflow for a fully custom graph
        (ControlNet, compositing, etc.); use THIS tool to fetch what either of them produced -
        by job id, by filename, or (with history=<count>) by listing a server's recent jobs when
        you don't have either. It's also how to check on a job either tool queued with
        queue_only=true. A result's 'server' can be passed as gpu_server to another call. To find
        a job or filename by prompt text, checkpoint, seed, date, or status instead, use
        search_jobs first, then pass its job_uuid/comfy_prompt_id/filename here. For a quick,
        human-readable browse instead of a single lookup, use list_jobs. To get the submitted
        GRAPH itself rather than the rendered image (most useful for run_workflow jobs), use
        retrieve_graph.

        Normal use: give the job id returned by generate_image(queue_only=true), or an image
        filename from the server's output folder. If the image exists it is shown to the user,
        along with the settings used to render it ('parameters': seed, steps, cfg, sampler,
        scheduler, denoise, prompts, checkpoint, LoRAs, size); if the job is still queued or
        running you get its queue status; otherwise you are told it wasn't found.

        Listing mode: omit job_id_or_filename and give gpu_server and history=<count> to get a
        list of the last <count> jobs on that server (job ids and filenames), e.g. to find a job
        id when only the filename or nothing at all is known.

        :param job_id_or_filename: A job id (UUID) or an image filename, e.g. "sdxl_simple_00076_.png" or "subfolder/name.png". Omit only when using history=<count> to list recent jobs instead.
        :param gpu_server: Omit to search all configured servers (default server first). Only set this to look on one specific server (full URL or IP) - required when using history=<count>.
        :param return_img_url: If true, include download URLs for each image in the result: 'comfy_url' (direct link on the GPU server), 'chat_url' (the copy shown in chat) and a ready-made 'link_markdown'. Off by default to keep results small.
        :param history: List the last <count> jobs on gpu_server instead of looking up a single image. Requires gpu_server and no job_id_or_filename.
        """

        async def status(text: str, done: bool = False):
            await _emit(__event_emitter__, {"type": "status", "data": {"description": text, "done": done}})

        await status("Listing recent jobs..." if history is not None else "Looking up image...")
        try:
            result = await retrieve(
                self.valves, job_id_or_filename, gpu_server, return_img_url, __event_emitter__, status, history
            )
        except Exception as e:  # keep the model from seeing a raw traceback
            logger.exception("Unexpected error during retrieval")
            result = {"success": False, "error": f"Unexpected error: {e}"}

        if "jobs" in result:
            summary = f"Found {len(result['jobs'])} job(s)" if result.get("success") else "Failed"
        elif result.get("images"):
            summary = "Done"
        elif result.get("status") in ("pending", "running"):
            summary = f"Job is {result['status']}"
        elif result.get("found") is False:
            summary = "Not found"
        else:
            summary = "Failed"
        await status(summary, done=True)
        return result

    async def search_jobs(
        self,
        prompt_text: Optional[str] = None,
        job_search: Optional[str] = None,
        checkpoint: Optional[str] = None,
        seed: Optional[int] = None,
        gpu_server: Optional[str] = None,
        tool: Optional[Literal["generate_image", "run_workflow"]] = None,
        status: Optional[List[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 20,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Search the shared job database for past render attempts - a finder, not a fetcher: use
        retrieve_image afterward (job_id_or_filename=<a job_uuid, comfy_prompt_id, or filename
        from this result>, gpu_server=<server>) to actually display an image.

        TOOLKIT: this searches the durable SQLite log that generate_image and run_workflow write
        to (when their own LOG_TO_SQLITE valve is on), covering every attempt - including jobs
        the server rejected or that timed out, not just completed renders. For a quick,
        human-readable browse instead (no filters needed), use list_jobs.

        :param prompt_text: Free-text search over positive/negative prompts, e.g. "lighthouse dusk". Omit to not filter by prompt text.
        :param job_search: Matches a job's id (its own UUID or ComfyUI's own prompt id) or filename, by substring. Omit to not filter by id/filename.
        :param checkpoint: Substring match (case-insensitive) against the checkpoint filename used, e.g. "epicrealism".
        :param seed: Exact seed value used by the job's sampler.
        :param gpu_server: Restrict to one server (full URL or a configured short name). Omit to search all servers.
        :param tool: Restrict to jobs made by generate_image or run_workflow. Omit for both.
        :param status: One or more of "built", "queued", "completed", "rejected", "failed". Omit for all statuses.
        :param date_from: ISO 8601 date/time (UTC), e.g. "2026-09-01" or "2026-09-01T00:00:00Z". Jobs created on/after this.
        :param date_to: ISO 8601 date/time (UTC). Jobs created on/before this (a bare date includes that whole day).
        :param limit: Max rows to return (capped by the MAX_SEARCH_RESULTS valve).
        """
        v = self.valves

        async def status_cb(text: str, done: bool = False):
            await _emit(__event_emitter__, {"type": "status", "data": {"description": text, "done": done}})

        await status_cb("Searching job history...")
        try:
            if not Path(v.JOB_DB_PATH).exists():
                result: Dict[str, Any] = {
                    "success": True,
                    "count": 0,
                    "jobs": [],
                    "note": (
                        "No job database found yet at JOB_DB_PATH; nothing has been logged, or JOB_DB_PATH "
                        "doesn't match generate_image's/run_workflow's valve."
                    ),
                }
            else:
                allowed = list(dict.fromkeys(list(v.GPU_SERVERS) + [v.DEFAULT_GPU_SERVER]))
                server = (
                    resolve_server(gpu_server, v.DEFAULT_GPU_SERVER, allowed, v.ALLOW_UNLISTED_SERVERS)
                    if not is_unset(gpu_server)
                    else None
                )
                result = await asyncio.to_thread(
                    run_job_search,
                    v.JOB_DB_PATH,
                    prompt_text=None if is_unset(prompt_text) else prompt_text,
                    job_search=None if is_unset(job_search) else job_search,
                    checkpoint=None if is_unset(checkpoint) else checkpoint,
                    seed=seed,
                    server=server,
                    tool=None if is_unset(tool) else tool,
                    status=status or None,
                    date_from=None if is_unset(date_from) else date_from,
                    date_to=None if is_unset(date_to) else date_to,
                    limit=min(limit, v.MAX_SEARCH_RESULTS) if limit else v.MAX_SEARCH_RESULTS,
                )
                if result.get("success"):
                    result["note"] = (
                        "Pass a row's job_uuid or comfy_prompt_id (or a filename) to retrieve_image, "
                        "with the same server, to fetch and display the image."
                    )
        except ValueError as e:
            result = {"success": False, "error": str(e)}
        except Exception as e:  # keep the model from seeing a raw traceback
            logger.exception("Unexpected error during search_jobs")
            result = {"success": False, "error": f"Unexpected error: {e}"}

        await status_cb(f"Found {result.get('count', 0)} job(s)" if result.get("success") else "Search failed", done=True)
        return result

    async def list_jobs(
        self,
        data_source: Optional[str] = None,
        job_count: int = 10,
        skip_to: int = 0,
        job_search: Optional[str] = None,
        prompt_search: Optional[str] = None,
        link_images: bool = True,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Browse job history as a Markdown table meant to be shown to the user directly - a
        librarian for past renders, not a fetcher. Each row's filename is a clickable link to the
        full image rather than an embedded image, since OWUI has no way to display an embedded
        image smaller than full size; click through (or use retrieve_image with a row's UUID or
        filename) to actually see one.

        TOOLKIT: this is one of five related tools sharing GPU_SERVERS. Unlike search_jobs (which
        returns structured JSON for filtering/chaining), list_jobs returns a ready-to-paste table,
        and needs no arguments at all for the common case - just call it to see the last 10 jobs.

        :param data_source: Omit for the default: recent jobs read straight from a GPU server's
            own live /history (the same data retrieve_image's history=<count> mode uses) - fast,
            no setup needed, but limited to whatever that server currently retains (cleared on
            restart, bounded retention) and no search filters apply there, just job_count/skip_to.
            Pass "database" instead to browse the full persistent job log across all servers
            (completed jobs only), where job_search/prompt_search work. Any other value is treated
            as a specific GPU server address to read live history from, instead of the default
            server.
        :param job_count: How many rows to show.
        :param skip_to: Skip this many jobs before listing (pagination - combine with job_count to
            page through results).
        :param job_search: database mode only. Matches a job's id (its own UUID or ComfyUI's own
            prompt id) or filename, by substring.
        :param prompt_search: database mode only. Free-text match over prompts.
        :param link_images: Make each row's filename a clickable link straight to the image on the
            GPU server, instead of plain text. Best-effort - the link may not open if the file's
            since been deleted, or if your browser can't reach that server directly.
        """
        v = self.valves

        async def status_cb(text: str, done: bool = False):
            await _emit(__event_emitter__, {"type": "status", "data": {"description": text, "done": done}})

        job_count = max(1, job_count)
        skip_to = max(0, skip_to)
        ds = None if is_unset(data_source) else data_source.strip()
        is_database = ds is not None and ds.lower() == "database"

        await status_cb("Building the job library table...")
        try:
            if is_database:
                if not Path(v.JOB_DB_PATH).exists():
                    table = "No job database found yet at JOB_DB_PATH - nothing has been logged, or JOB_DB_PATH doesn't match generate_image's/run_workflow's valve."
                else:
                    found = await asyncio.to_thread(
                        run_job_search,
                        v.JOB_DB_PATH,
                        prompt_text=None if is_unset(prompt_search) else prompt_search,
                        job_search=None if is_unset(job_search) else job_search,
                        status=["completed"],
                        limit=job_count,
                        offset=skip_to,
                    )
                    if not found.get("success"):
                        raise ValueError(found.get("error") or "search failed")
                    rows = _db_rows_to_display_rows(found["jobs"])
                    table = format_jobs_table(
                        rows, skip_to, found.get("total_count", len(rows)), link_images, v.REQUEST_TIMEOUT_SECONDS
                    )
            else:
                allowed = list(dict.fromkeys(list(v.GPU_SERVERS) + [v.DEFAULT_GPU_SERVER]))
                server = resolve_server(ds, v.DEFAULT_GPU_SERVER, allowed, v.ALLOW_UNLISTED_SERVERS)
                client = ComfyClient(server, v.REQUEST_TIMEOUT_SECONDS)
                raw = await asyncio.to_thread(client.get_history_list, v.MAX_HISTORY_ITEMS)
                all_rows = _live_rows_from_history(raw, server)
                page = all_rows[skip_to : skip_to + job_count]
                table = format_jobs_table(page, skip_to, len(all_rows), link_images, v.REQUEST_TIMEOUT_SECONDS)

            result: Dict[str, Any] = {
                "success": True,
                "markdown_table": table,
                "note": "Paste markdown_table exactly as given into your reply so it renders as a table; don't reformat or summarize it.",
            }
        except ValueError as e:
            result = {"success": False, "error": str(e)}
        except ComfyError as e:
            result = {"success": False, "error": f"Could not reach the GPU server: {e}"}
        except Exception as e:  # keep the model from seeing a raw traceback
            logger.exception("Unexpected error during list_jobs")
            result = {"success": False, "error": f"Unexpected error: {e}"}

        await status_cb("Done" if result.get("success") else "Failed", done=True)
        return result

    async def retrieve_graph(
        self,
        job_id_or_filename: str,
        gpu_server: Optional[str] = None,
        data_source: Optional[str] = None,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch the exact ComfyUI graph (JSON) that produced a job - the input run_workflow was
        given, not the rendered image. Most useful for "graph"-mode jobs (made with run_workflow's
        custom graphs), where the graph is bespoke and worth pulling back up to inspect or
        resubmit with edits; works for "direct"-mode jobs too, though their fixed graph shape is
        already documented in generate_image.

        TOOLKIT: this is one of five related tools sharing GPU_SERVERS. Unlike retrieve_image
        (which fetches and displays the rendered image), this fetches the graph itself. Pass the
        result's 'graph' straight to run_workflow's workflow argument to resubmit it, optionally
        edited first.

        :param job_id_or_filename: A job id (this tool's own UUID, or ComfyUI's own prompt id) or
            an image filename the job produced.
        :param gpu_server: Omit to search all configured servers (default server first, live mode
            only - database mode already knows each job's server).
        :param data_source: Omit for the default: read the graph from a GPU server's own live
            /history or queue - fails if that job's since rotated out of the server's retention.
            Pass "database" to read it from the persistent job log instead, which keeps it forever
            (as long as LOG_TO_SQLITE was on for that job).
        """
        v = self.valves

        async def status_cb(text: str, done: bool = False):
            await _emit(__event_emitter__, {"type": "status", "data": {"description": text, "done": done}})

        ds = None if is_unset(data_source) else data_source.strip()
        is_database = ds is not None and ds.lower() == "database"

        await status_cb("Looking up the submitted graph...")
        try:
            if is_database:
                found = await asyncio.to_thread(find_graph_in_db, v.JOB_DB_PATH, job_id_or_filename)
            else:
                found = await find_graph_live(v, job_id_or_filename, gpu_server, status_cb)

            if not found.get("success"):
                result = {"success": False, "error": found.get("error") or "not found"}
            else:
                graph = found["graph"]
                result = {
                    "success": True,
                    "found": True,
                    "graph": graph,
                    "server": found.get("server"),
                    "comfy_prompt_id": found.get("comfy_prompt_id"),
                    "status": found.get("status"),
                    "job_mode": classify_job_mode(graph),
                    "img_type": extract_parameters(graph).get("mode"),
                    "note": "This is the exact ComfyUI graph that produced this job. To resubmit it (optionally edited), pass it as run_workflow's workflow argument.",
                }
        except Exception as e:  # keep the model from seeing a raw traceback
            logger.exception("Unexpected error during retrieve_graph")
            result = {"success": False, "error": f"Unexpected error: {e}"}

        await status_cb("Done" if result.get("success") else "Not found", done=True)
        return result
