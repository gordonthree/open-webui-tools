# open-webui-tools

Custom tool scripts for [Open WebUI](https://openwebui.com/) (referred to as **OWUI** throughout
this project) that let LLM models drive image generation on self-hosted ComfyUI GPU servers, in
support of a creative-writing project. It's also a general vehicle for exploring LLM/API interop —
expect more tool scripts of this kind over time, not just ComfyUI ones. The OWUI server and GPU
servers are self-hosted on a private cloud.

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
- `comfy_sdxl_retrieve.py` → `retrieve_image`, `search_jobs`, `list_jobs`, `retrieve_graph` —
  fetches a rendered image by job id or filename (or lists a server's recent jobs), searches the
  shared job history (structured JSON, for filtering/chaining), browses it as a formatted Markdown
  table (for showing the user directly — a "librarian" view, not a fetcher), and fetches the
  submitted ComfyUI graph itself for a job (most useful for run_workflow jobs, whose graphs are
  bespoke — pass it back to run_workflow, optionally edited, to resubmit).

All three files share a `GPU_SERVERS` valve list and a `resolve_server`/`resolve_checkpoint`-style
matching approach, and are meant to be used together — see each tool's own `TOOLKIT` docstring
paragraph for how they hand off to one another.

## Tool arguments

Quick reference for each tool method's arguments. This is the simple version — types and what
each argument expects, not the full docstring (see each tool's own `:param:` lines in `src/` for
complete detail, e.g. exact sampler/scheduler name lists).

### `generate_image` (comfy_sdxl_direct.py)

| Argument | Expected value |
|---|---|
| `positive_prompt` | Text, required |
| `negative_prompt` | Text, required |
| `batch_size` | Integer, default `1` |
| `source_image` | Omit for txt2img; otherwise an earlier result's `use_as_source_image` value (switches to img2img) |
| `source_server` | Omit unless `source_image` is on a different server than `gpu_server` |
| `denoise` | Float 0.0–1.0; default `1.0` (txt2img) / `0.6` (img2img) |
| `seed` | Integer; `-1` (default) picks a random seed |
| `steps` | Integer, default `25` |
| `cfg` | Float, default `5.0` |
| `sampler_name` | KSampler sampler name, default `dpmpp_2m` |
| `scheduler` | KSampler scheduler name, default `karras` |
| `width` | Pixels, txt2img only, default `1216` |
| `height` | Pixels, txt2img only, default `824` |
| `checkpoint_name` | Omit for the default checkpoint; otherwise an exact filename on the server |
| `loras` | Omit for none; otherwise a list like `[{"lora": "name.safetensors", "strength": 0.8}]` |
| `gpu_server` | Omit for the default server; otherwise a configured server or address |
| `return_img_url` | Boolean, default `false` |
| `queue_only` | Boolean, default `false` — submit without waiting for the render |
| `verbose` | Boolean, default `false` — also return the full submitted workflow and server history |

### `run_workflow` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `workflow` | Required. A ComfyUI API-format graph, as a JSON string (node-specific fields aren't listed here — see the tool's own docstring) |
| `source_image` | Omit to leave the fixed source-image node pointed at a placeholder; otherwise an earlier result's `use_as_source_image` value |
| `source_server` | Omit unless `source_image` is on a different server than `gpu_server` |
| `gpu_server` | Omit for the default server |
| `return_img_url` | Boolean, default `false` |
| `queue_only` | Boolean, default `false` |
| `verbose` | Boolean, default `false` — also return the prepared graph and server history |

### `retrieve_image` (comfy_sdxl_retrieve.py)

| Argument | Expected value |
|---|---|
| `job_id_or_filename` | A job UUID or an image filename; omit only when using `history` |
| `gpu_server` | Omit to search all configured servers; required when using `history` |
| `return_img_url` | Boolean, default `false` |
| `history` | Omit for a single lookup; otherwise a count of recent jobs to list on `gpu_server` |

### `search_jobs` (comfy_sdxl_retrieve.py)

| Argument | Expected value |
|---|---|
| `prompt_text` | Free text; omit to not filter by prompt |
| `checkpoint` | Substring of a checkpoint filename |
| `seed` | Exact integer seed |
| `gpu_server` | Omit to search all servers |
| `tool` | `"generate_image"` or `"run_workflow"`; omit for both |
| `status` | One or more of `built`, `queued`, `completed`, `rejected`, `failed` |
| `date_from` | ISO 8601 date/time (UTC), e.g. `2026-09-01` |
| `date_to` | ISO 8601 date/time (UTC); a bare date includes that whole day |
| `limit` | Integer, default `20`, capped by the `MAX_SEARCH_RESULTS` valve |

### `list_jobs` (comfy_sdxl_retrieve.py)

| Argument | Expected value |
|---|---|
| `data_source` | Omit for the default live server; `"database"` for the persistent job log; any other value is treated as a specific GPU server address (live) |
| `job_count` | Integer, default `10` |
| `skip_to` | Integer, default `0` — pagination offset |
| `job_search` | Database mode only. Matches a job's id or filename, by substring |
| `prompt_search` | Database mode only. Free-text match over prompts |
| `link_images` | Boolean, default `true` — link each row's filename to the image instead of plain text |

### `retrieve_graph` (comfy_sdxl_retrieve.py)

| Argument | Expected value |
|---|---|
| `job_id_or_filename` | Required. A job id (UUID or ComfyUI's own prompt id) or an image filename |
| `gpu_server` | Omit to search all configured servers (live mode only) |
| `data_source` | Omit for the default live server/queue; `"database"` for the persistent job log |

## Job database

Every render attempt across all three tools — including ones ComfyUI rejected or that timed
out — is durably logged to a **shared SQLite database** (one file, `JOB_DB_PATH`, identical across
all three tools' valves). Full schema, design rationale, and current status:
`src/SQLITE_JOB_DB_HANDOFF.md`. Read that file before touching job-DB code.

The tools create this database's schema lazily and automatically on first write, so nothing needs
to be run ahead of time. `python scripts/init_job_db.py <path>` exists anyway, as a standalone,
dependency-free way to pre-create/verify it directly on the Docker host — useful for checking a
volume mount's permissions before the first real render.

`python scripts/import_server_history.py <server> <db_path>` sweeps a ComfyUI server's own
`/history` directly (no OWUI involved) and adds whatever completed jobs the database doesn't
already know about: backfilling an existing-but-unlinked job row where the graph matches (the
same reconciliation `retrieve_image` does opportunistically for one file at a time), or recording
a brand-new row (`tool='external'`) for anything with no matching row at all — e.g. rendered
directly through ComfyUI's own UI, or from before job logging existed. `--dry-run` previews
without writing. Bounded by the server's own history retention (cleared on restart), same as
`retrieve_image`'s `history=<count>` mode.

## Testing

Each tool has a matching `test_*.py` file that simulates ComfyUI's HTTP API with a fake `requests`
module — no live ComfyUI or Open WebUI server needed. Run all of them from `src/`:

```
python -m unittest test_comfy_sdxl_direct test_comfy_sdxl_graph test_comfy_sdxl_retrieve -v
```

`scripts/import_server_history.py` has its own test file the same way — run from `scripts/`:

```
python -m unittest test_import_server_history -v
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
and `search_jobs` read that database and opportunistically reconcile jobs that lost their
`comfy_prompt_id` link; `list_jobs` browses it as a formatted Markdown table; `retrieve_graph`
fetches a job's submitted ComfyUI graph directly (live server or database). 134 tests across the
three `test_*.py` files, 133 passing — the one failure is a pre-existing, Windows-only
syslog-socket quirk unrelated to the job database (see `src/SQLITE_JOB_DB_HANDOFF.md`).

Also added: `scripts/import_server_history.py`, a standalone offline sweep of a ComfyUI server's
own `/history` that backfills or imports whatever the database doesn't already know about (own
test file, `scripts/test_import_server_history.py`, 11 tests); `scripts/job_db_common.py`, shared
stdlib-only job-DB helpers for `scripts/*.py` (no OWUI cross-import restriction there, so this one
isn't hand-duplicated a third/fourth time the way the three tools are); `scripts/push_tool.py` to
deploy a tool revision to a running OWUI instance without the manual web UI import, with a
pre-overwrite backup to `tmp/`; documentation moved from `CLAUDE.md` into this file.

**Left to do:** nothing currently planned for the job-DB scope itself. Possible future work (not
started): richer parameter extraction in `search_jobs` results (seed/steps/cfg/sampler, currently
only prompts are surfaced); pagination if the job history grows large enough for `limit`/
`MAX_SEARCH_RESULTS` to stop being sufficient. More broadly, this project is expected to grow
beyond ComfyUI tooling as the user explores more LLM/API interop via OWUI.
