"""
Shared, dependency-free (stdlib only) job-database helpers for scripts/*.py.

Unlike src/*.py (OWUI loads each tool as an isolated module and they can't import from each
other, so logic is hand-duplicated there), scripts/ has no such restriction - these are ordinary
local Python files, so this module is imported rather than copy-pasted a third time. Still kept
free of third-party dependencies (no pydantic/requests) so any script here can run on a bare host
Python install with nothing else set up.

JOB_DB_SCHEMA/PROMPTS_FTS_SCHEMA are kept byte-identical to the three tools' own copies - see
src/SQLITE_JOB_DB_HANDOFF.md. Update all four (three tools + this file) together if the schema
ever changes.
"""
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# Must match SOURCE_IMAGE_NODE_ID in src/comfy_sdxl_graph.py exactly - see the identical constant
# and classify_job_mode() in src/comfy_sdxl_retrieve.py for the full rationale.
GRAPH_TOOL_MARKER_NODE_ID = "__comfy_tool_source_image__"


def job_db_connect(db_path: str) -> sqlite3.Connection:
    """A connection with the schema ensured (idempotent - CREATE TABLE/VIRTUAL TABLE IF NOT
    EXISTS). Caller must close() it."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(JOB_DB_SCHEMA)
    try:
        conn.execute(PROMPTS_FTS_SCHEMA)
    except sqlite3.OperationalError:
        pass  # this SQLite build lacks FTS5; search falls back to LIKE
    conn.commit()
    return conn


def has_prompts_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM prompts_fts LIMIT 0;")
        return True
    except Exception:
        return False


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def execution_timing(history_entry: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[float]]:
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


def history_prompt_graph(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """History stores the submission as [number, prompt_id, graph, extra_data, outputs]."""
    prompt = entry.get("prompt")
    if isinstance(prompt, (list, tuple)) and len(prompt) > 2 and isinstance(prompt[2], dict):
        return prompt[2]
    return None


def filenames_from_history_entry(entry: Dict[str, Any]) -> List[str]:
    images = [
        img
        for out in (entry.get("outputs") or {}).values()
        for img in (out.get("images") or [])
        if img.get("type", "output") == "output"
    ]
    return [f"{img['subfolder']}/{img['filename']}" if img.get("subfolder") else img["filename"] for img in images]


def extract_node_params(graph: Dict[str, Any]) -> List[tuple]:
    """(node_id, class_type, param_name, value_text, value_num) for every scalar input in the
    graph. List-typed inputs are links to other nodes (e.g. ["12", 0]), not literal values, and
    are skipped - this only captures the literal parameters actually set on each node."""
    rows = []
    for node_id, node in graph.items():
        class_type = node.get("class_type", "")
        for name, value in (node.get("inputs") or {}).items():
            if isinstance(value, list):  # a link to another node's output, not a literal
                continue
            value_num = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            rows.append((str(node_id), str(class_type), str(name), json.dumps(value) if not isinstance(value, str) else value, value_num))
    return rows


def extract_prompts(graph: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort positive/negative prompt text. Only follows the common KSampler ->
    CLIPTextEncode wiring; returns (None, None) for anything else rather than guessing."""
    sampler = next((n for n in graph.values() if n.get("class_type") in ("KSampler", "KSamplerAdvanced")), None)
    if not sampler:
        return None, None

    def _text(link: Any) -> Optional[str]:
        if isinstance(link, list) and len(link) == 2:
            target = graph.get(str(link[0])) or {}
            text = (target.get("inputs") or {}).get("text")
            return text if isinstance(text, str) else None
        return None

    inputs = sampler.get("inputs") or {}
    return _text(inputs.get("positive")), _text(inputs.get("negative"))


def classify_job_mode(graph: Optional[Dict[str, Any]]) -> Optional[str]:
    """'direct' (comfy_sdxl_direct.py's fixed graph) or 'graph' (comfy_sdxl_graph.py's
    model-authored graph); None if graph is missing/unreadable."""
    if not isinstance(graph, dict):
        return None
    return "graph" if GRAPH_TOOL_MARKER_NODE_ID in graph else "direct"
