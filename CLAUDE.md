# open-webui-tools

Custom tool scripts for [Open WebUI](https://openwebui.com/) (referred to as **OWUI** throughout
this project and in conversation) that let LLM models drive image generation on self-hosted
ComfyUI GPU servers, in support of a creative-writing project. It's also a general vehicle for
exploring LLM/API interop — expect more tool scripts of this kind over time, not just ComfyUI
ones. The OWUI server and GPU servers are self-hosted on a private cloud.

This file is checked into the repo so it follows the project across machines (the user works on
this from several computers); anything here should be durable project context, not a specific
task's notes.

## Layout and constraints

All tools live in `src/`, one `.py` file per tool. **Open WebUI loads each tool as an isolated
module — they cannot import from each other.** Any logic shared between tools (HTTP client code,
server/checkpoint resolution, the job database helpers, etc.) is duplicated by hand across files.
When you change shared logic in one file, check whether the same change is needed in the others.

Current tools:
- `comfy_sdxl_direct.py` → `generate_image` — the primary SDXL render tool (txt2img/img2img,
  LoRAs), a fixed ComfyUI graph built by the tool itself.
- `comfy_sdxl_graph.py` → `run_workflow` — accepts a full model-authored ComfyUI API-format graph,
  for anything the fixed graph can't do (ControlNet, compositing, custom node combinations).
- `comfy_sdxl_retrieve.py` → `retrieve_image`, `search_jobs`, `list_jobs` — fetches a rendered
  image by job id or filename (or lists a server's recent jobs), searches the shared job history
  (structured JSON, for filtering/chaining), and browses it as a formatted Markdown table (for
  showing the user directly — a "librarian" view, not a fetcher).

All three files share a `GPU_SERVERS` valve list and a `resolve_server`/`resolve_checkpoint`-style
matching approach, and are meant to be used together — see each tool's own `TOOLKIT` docstring
paragraph for how they hand off to one another.

## Job database

Every render attempt across all three tools — including ones ComfyUI rejected or that timed
out — is durably logged to a **shared SQLite database** (one file, `JOB_DB_PATH`, identical across
all three tools' valves). Full schema, design rationale, and current status:
`src/SQLITE_JOB_DB_HANDOFF.md`. Read that file before touching job-DB code; don't duplicate its
content here, and keep it updated when the job-DB scope changes.

The tools create this database's schema lazily and automatically on first write, so nothing needs
to be run ahead of time. `python scripts/init_job_db.py <path>` exists anyway, as a standalone,
dependency-free way to pre-create/verify it directly on the Docker host — useful for checking a
volume mount's permissions before the first real render.

## Testing

Each tool has a matching `test_*.py` file that simulates ComfyUI's HTTP API with a fake `requests`
module — no live ComfyUI or Open WebUI server needed. Run all of them from `src/`:

```
python -m unittest test_comfy_sdxl_direct test_comfy_sdxl_graph test_comfy_sdxl_retrieve -v
```

`pydantic` and `requests` need to be installed in whatever Python environment runs the tests
(Open WebUI provides both at runtime; a bare local Python install won't have them by default).
`aiohttp` is used for live websocket progress and is optional — its tests are skipped, not failed,
when it isn't installed.

## Deploying a revision to OWUI

`scripts/push_tool.py` pushes a tool's local `.py` source straight into a running OWUI instance
via its Tools API (`POST /api/v1/tools/id/{id}/update`), so a revision doesn't need manually
re-importing through the OWUI web UI. It reads connection details and per-tool ids from
`secrets.md` at the repo root — gitignored, never committed; copy `secrets.example.md` to
`secrets.md` and fill it in (once per machine, since it's not checked into git). Usage:
`python scripts/push_tool.py <tool_name|all> [--dry-run]`.

## Status notes

### 2026-09-23
First time this project's actual content (all three tools, their tests, and the job-DB handoff
doc) was committed to git — everything up to this point existed only as local files copied in
from a prior web-Claude session's handoff package.

**Done:** all three tools are feature-complete for the job database: `generate_image` and
`run_workflow` write every job attempt (built/queued/completed/rejected/failed); `retrieve_image`
and the new `search_jobs` method read that database and opportunistically reconcile jobs that lost
their `comfy_prompt_id` link. 110 tests across the three `test_*.py` files, 108 passing — the one
failure is a pre-existing, Windows-only syslog-socket quirk unrelated to the job database (see
`src/SQLITE_JOB_DB_HANDOFF.md`).

**Left to do:** nothing currently planned for the job-DB scope itself. Possible future work (not
started): richer parameter extraction in `search_jobs` results (seed/steps/cfg/sampler, currently
only prompts are surfaced); pagination if the job history grows large enough for `limit`/
`MAX_SEARCH_RESULTS` to stop being sufficient. More broadly, this project is expected to grow
beyond ComfyUI tooling as the user explores more LLM/API interop via OWUI.
