# ComfyUI Open WebUI Tools — SQLite Job Database: Handoff Summary

Three Open WebUI tool scripts, each a single self-contained `.py` file (Open WebUI tools can't
import from each other, so shared logic is duplicated by hand across files — keep that in mind
when editing schema or helper logic in one file, it needs the same edit in the others):

| File | Tool exposed | Job-DB status |
|---|---|---|
| `comfy_sdxl_direct.py` | `generate_image` | **Done, tested** |
| `comfy_sdxl_graph.py` | `run_workflow` (raw ComfyUI graph, e.g. ControlNet) | **Done, tested** |
| `comfy_sdxl_retrieve.py` | `retrieve_image`, `search_jobs`, `list_jobs`, `retrieve_graph` | **Done, tested** |

Matching test files: `test_comfy_sdxl_direct.py`, `test_comfy_sdxl_graph.py`,
`test_comfy_sdxl_retrieve.py`. All simulate ComfyUI's HTTP API with a fake `requests` module — no
real ComfyUI server needed. Currently **136 tests**, all passing except one pre-existing,
unrelated failure on Windows only (`test_unreachable_syslog_falls_back_to_file` in
`test_comfy_sdxl_direct.py` — a Windows syslog-socket quirk, not a job-DB issue).

**`list_jobs`** is a browsing/display counterpart to `search_jobs`: instead of structured JSON,
it returns a ready-to-paste Markdown table (`# | UUID | Job Timestamp | Information`, with
filename/server/status/prompts all folded into the one "Information" cell — narrow chat windows
don't render wide tables well). Each filename is a plain Markdown *link* to the image
(`link_images` valve/param, default on), not an embedded `![]()` image: OWUI's
`sanitizeResponseContent` escapes raw HTML before Markdown parsing (so a `<img width=...>` size
hint never survives to render), and plain Markdown has no image-sizing syntax either — an embedded
image always shows at full size, which is unusable inline in a multi-row table. A link avoids that
without losing one-click access to the image. It has a `data_source` argument picking
between two independent code paths that get normalized into the same display-row shape before
formatting:
- **live** (default, or an explicit GPU server address) — reads a server's own `/history`
  directly, the same data `retrieve_image`'s `history=<count>` mode uses. No search filters here
  by design (explicit steer: "no need to do any searching... maintain existing functionality") —
  just `job_count`/`skip_to` pagination over whatever that server currently retains.
- **`data_source="database"`** — browses the persistent job log via `run_job_search` (extended
  with `job_search` and `offset`/`total_count` support), completed jobs only, with `job_search`
  (matches a job's id or filename, substring) and `prompt_search` available.

**Correction (2026-09-24):** `search_jobs` itself never actually exposed `job_search` as an
argument, despite `run_job_search` supporting it since `list_jobs` was built — an oversight caught
by the project owner reading the README's new argument table. Fixed: `search_jobs` now also takes
`job_search`, wired straight through to the same `run_job_search` parameter. Bumped
`comfy_sdxl_retrieve.py` to 1.4.0.

Each row's Information cell also shows a **Type** (`txt2img`/`img2img`, from
`extract_parameters(graph)`'s existing `mode` key) and **Mode** (`direct`/`graph` — which tool made
it) line. Mode is detected purely from the graph's own shape: `run_workflow` injects a fixed
reserved node id (`__comfy_tool_source_image__`, see `comfy_sdxl_graph.py`'s
`SOURCE_IMAGE_NODE_ID`) into every graph it submits, and `generate_image`'s fixed graph never
contains it — so `classify_job_mode()` works the same way regardless of data source, no DB `tool`
column lookup needed. `run_job_search` now also batch-fetches each job's `outputs.raw_json` to
compute `img_type`/`job_mode` server-side, but returns only those two small derived strings, not
the graph itself — `search_jobs` stays a lean finder; `outputs.raw_json` was already being read
for the checkpoint/seed `EXISTS` filters, so this doesn't add a new table read.

**`retrieve_graph`** fetches the exact submitted graph (JSON) for a job — the input `run_workflow`
was given, not the rendered image — most useful for `graph`-mode jobs, where the graph is bespoke
and worth pulling back up to inspect or resubmit (optionally edited). Same `data_source` split as
`list_jobs`: live (default, or an explicit server) reads a server's own `/history` **or queue**
(a still-running/pending job has a graph too, via the already-existing `queue_item_graph()`), by
job id or filename, mirroring `retrieve()`'s own lookup logic but returning the graph instead of
delivering an image; `data_source="database"` matches on `job_uuid`, `comfy_prompt_id`, or a
filename substring (via the same `results.raw_json LIKE` technique `list_jobs`'s `job_search`
uses) against `outputs.raw_json`, and survives the server's own history being rotated/cleared,
since the DB keeps it forever.

Run tests with (from the directory containing the files):
```
python -m unittest test_comfy_sdxl_direct test_comfy_sdxl_graph test_comfy_sdxl_retrieve -v
```

`scripts/job_db_common.py` has a fourth copy of `JOB_DB_SCHEMA`/`PROMPTS_FTS_SCHEMA` (plus
`extract_node_params`/`extract_prompts`/`classify_job_mode`/etc.), kept byte-identical/logically
identical to the three tools' own copies. Unlike `src/*.py`, `scripts/*.py` has no OWUI
cross-import restriction, so this is imported by every script that needs it rather than
hand-duplicated a fifth/sixth time — still update it alongside the three tools if the schema ever
changes, just in one place instead of per-script. Two scripts use it:
- `scripts/init_job_db.py` — a standalone script for creating/verifying the database file
  directly on the Docker host (no ComfyUI/OWUI/docker exec needed, since a bind-mounted sqlite
  file is just a regular file on the host). Not required for the tools to work — they create the
  schema lazily on first write regardless — but useful for checking a volume mount's permissions
  before the first real render, or for poking at an empty database with a host-side tool.
- `scripts/import_server_history.py` — sweeps a ComfyUI server's own `/history` directly (no OWUI
  involved) and adds whatever completed jobs aren't already tracked: backfills an existing
  unlinked job row where the graph matches (the exact same matching + closest-in-time tie-break
  `comfy_sdxl_retrieve.py`'s reconciliation uses, ported rather than imported, so this script has
  no `pydantic`/OWUI Tools-class dependency), or records a brand-new row (`tool='external'`) for
  a job with no matching row at all — e.g. rendered directly through ComfyUI's own UI, or from
  before job logging existed. `--dry-run` previews without writing (except: it still creates the
  database's schema if the path doesn't exist yet, even under `--dry-run` — unlike `search_jobs`'s
  read-only guard against this, this script's whole purpose is to manage that exact path, so the
  side effect is expected here, not surprising). Its own test file,
  `scripts/test_import_server_history.py` (11 tests, run from `scripts/`), caught a real bug
  during development: the original "no history entries" early-return skipped schema creation
  entirely, so an empty server response left the database file half-initialized (no tables) even
  under `--dry-run` against a fresh path.

---

## Why this exists

The person wants a durable, queryable record of every image-generation job these tools run, so
they can ask months later "how did I make that image?" and get a real answer — not just the
final result, but the exact request, the exact graph sent to ComfyUI, what the server said back,
and any errors along the way. A prior syslog/JSON-lines log (still present, see below) covered
successful renders only and wasn't queryable. This SQLite layer is additive to that, not a
replacement.

## Design goals, as stated by the project owner

- Store **verbatim JSON** at each stage: what the tool received from the model (`inputs`), what
  it sent to ComfyUI (`outputs`), what ComfyUI sent back (`results`), and anything that went
  wrong (`errors`) — as separate tables, not one blob.
- Also break the JSON down into **queryable, non-JSON columns**: prompt text, and generic
  per-node parameters (works for both the fixed `generate_image` graph and arbitrary
  `run_workflow` graphs).
- Everything is keyed by a **UUID the tool generates itself**, before any network call — because
  if ComfyUI rejects the job or never responds, that's still a real event worth cataloguing, and
  there's no server-assigned ID yet at that point.
- Once/if ComfyUI does assign its own `prompt_id`, record the mapping between the two IDs.
- Performance data: **kept intentionally minimal** per the owner's explicit steer (see "Decisions
  made along the way" below) — just what ComfyUI reports natively, nothing fancier.
- Lean error handling throughout — this is a hobby project, not something built to survive every
  edge case. Best-effort, never let logging trouble break an actual render.

---

## Schema (identical in both finished files — `comfy_sdxl_direct.py` and `comfy_sdxl_graph.py`)

Search each file for `JOB_DB_SCHEMA` to see the literal DDL. Summary:

- **`jobs`** — one row per job attempt. `job_uuid` (tool-generated, PK), `comfy_prompt_id`
  (nullable — filled in once/if the server assigns one), `tool` (`generate_image` /
  `run_workflow`), `server`, `status` (`built` → `queued` → `completed` / `rejected` / `failed`),
  `created_at`, `submitted_at`, `execution_start_ts`, `execution_end_ts`, `duration_s`.
- **`inputs`** — raw JSON of what the model passed the tool. Written first, before submission.
- **`outputs`** — raw JSON of the *prepared* graph actually sent to ComfyUI.
- **`results`** — raw JSON ComfyUI sent back. Two `stage` values so far: `submit_response` (the
  immediate `/prompt` ack) and `history` (the final `/history` entry).
- **`errors`** — `stage` (`submit` or `wait`), message, and raw JSON if there was any. A job can
  have zero or more rows here.
- **`prompts`** — `positive_prompt` / `negative_prompt` as plain text (not JSON), plus an FTS5
  virtual table `prompts_fts` for full-text search, with a graceful fallback (search still works
  via `LIKE`, just not indexed) if a given SQLite build lacks the FTS5 extension.
- **`node_params`** — generic `(job_uuid, node_id, class_type, param_name, value_text,
  value_num)`. Populated by walking every node's `inputs` in the *actual submitted graph* and
  recording every literal (non-link) value. This one mechanism covers both tools without needing
  per-node-type schema — "find every job where any node had `cfg=7.5`" works regardless of which
  tool made it.

**Lifecycle functions** (same names in both files): `record_job_built`, `record_job_submitted`,
`record_job_completed`, `record_job_failed`. Each opens its own short-lived SQLite connection
(WAL mode, busy timeout), does one transaction, and is best-effort — logging trouble is caught
and warned about, never allowed to fail the actual render. Schema creation is idempotent and
cached per-process (`_job_db_schema_ready`) so it isn't re-run on every call.

**Where they're called from**, in both tools' main method:
1. `job_uuid = str(uuid.uuid4())` generated right after the graph is fully built (checkpoint
   resolved, source image copied if needed) — not at the very top of the function, so an
   argument-validation error never gets a DB row at all (deliberate — see decisions below).
2. `record_job_built` — immediately after, before any network call.
3. Inside `ComfyClient.run()`: `record_job_submitted` right after ComfyUI hands back a
   `prompt_id`; `record_job_completed` right before returning success; `record_job_failed` at
   **two separate `try/except` points inside `run()`** — one around the submit call (stage=
   `submit` → status becomes `rejected`), one around the polling loop (stage=`wait` → status
   becomes `failed`, covers both timeouts and `execution_error`). This stage-awareness lives
   **inside `run()`**, not in the caller — see the bug described below for why.
4. `queue_only` path (submits but doesn't wait) has its own submit/reject recording, separate
   from `run()`.
5. A `render_completed` flag in the caller guards the outer catch-all `except Exception` — if the
   render itself already succeeded and something fails afterward (e.g. the Open WebUI upload
   throws), the job must **not** be overwritten back to `failed`. There's a test for this in both
   suites (`test_delivery_failure_after_success_keeps_status_completed` /
   `..._writes_full_lifecycle` equivalents).

**Valves** (same names, same defaults, in both files' `Tools.Valves`):
```
LOG_TO_SQLITE: bool = True
JOB_DB_PATH: str = "/app/backend/data/comfy_outputs/comfy_jobs.sqlite3"
```
Keep `JOB_DB_PATH` identical across all three tools' valves so they share one database file. The
default path matches the existing `OUTPUT_DIR`/`JOB_LOG_PATH` convention — if that directory is
already a mounted Docker volume, the `.sqlite3` file just appears there on the host automatically,
no extra mount needed. (The person confirmed they want it written from inside the container but
land on the host filesystem via a volume mount — this default path is that mount point.)

This SQLite logging is **independent of** the older `LOG_JOBS`/`LOG_BACKEND` syslog-or-file log
(also still present in both files, unchanged) — that one only logs successful renders, in a
non-queryable format, and is a separate, earlier feature. Both can be on at once; neither depends
on the other.

---

## What's built now (as of this update)

`comfy_sdxl_retrieve.py` now has both pieces that were pending, plus full test coverage in
`test_comfy_sdxl_retrieve.py` (27 tests: `SearchJobsTests`, `ReconciliationTests`,
`ReconciliationHelperTests`). It ports only the subset of the other files' job-DB code it
actually needs (`JOB_DB_SCHEMA`/`PROMPTS_FTS_SCHEMA`, `job_db_connect`, `has_prompts_fts`,
`_now_iso`, `_execution_timing`, `record_job_completed`) — it never generates a `job_uuid` or
submits a graph, so it has no `record_job_built`/`record_job_submitted`/`record_job_failed` of
its own, and only ever reads or backfills existing rows.

### `search_jobs` (new tool method)
Read-only. Filters: `prompt_text` (via `prompts_fts MATCH`, falling back to `LIKE` when FTS5
isn't available), `checkpoint` (substring match via an `EXISTS` subquery against `node_params`,
since it isn't a `jobs` column), `seed` (same, exact match on `KSampler`/`KSamplerAdvanced`
`seed`/`noise_seed`), `gpu_server`, `tool`, `status` (list), `date_from`/`date_to` (a bare
`date_to` is padded to end-of-day so it's inclusive of that whole day). Returns `filenames` only
for `completed` rows, read from the matching `results` row (`stage='history'`). If `JOB_DB_PATH`
doesn't exist on disk yet, returns an empty result **without** creating the file — `job_db_connect`
would otherwise do that as a side effect via its `CREATE TABLE IF NOT EXISTS`.

Two new valves specific to this file: `LOG_TO_SQLITE` (gates reconciliation **writes** only —
unlike the same-named valve in the other two tools, it does *not* gate `search_jobs`, which always
reads if the database exists) and `MAX_SEARCH_RESULTS` (default 25, caps the `limit` argument).
`JOB_DB_PATH` must still match the other two tools' valve exactly.

### Reconciliation (inside `retrieve()`'s filename-lookup branch)
Built per the mechanism designed below, with one correction found during implementation: **`record_job_completed` alone never writes `comfy_prompt_id`** — only `record_job_submitted` does,
and reconciliation's whole premise is a job that never got that call. So there's a small
file-local wrapper, `record_job_reconciled`, that backfills `comfy_prompt_id` first (guarded by
`AND comfy_prompt_id IS NULL`, a harmless no-op against a concurrent double-reconcile) and then
delegates to `record_job_completed` verbatim for status/timing/results.

Runs only when a file is found by filename (not by job id — a job-id lookup already has a real
`comfy_prompt_id` by definition), guarded by the new `LOG_TO_SQLITE` valve, and wrapped in
`try/except` at both the orchestrating function (`reconcile_job_for_file`, which itself never
raises) and the call site in `retrieve()`, so a bookkeeping failure can never break a successful
image retrieval. A cheap `any_unlinked_jobs` existence check means the common case (the file's job
was already linked normally) never pays for a `/history` scan.

The ambiguity handling matches the design below exactly: multiple graph matches are resolved by
proximity to the history entry's `execution_start` timestamp, with a `5.0s` window — inside that
window, or with no `execution_start` to compare against at all, the closest/oldest candidate is
still linked (not left unresolved), but the result carries `"ambiguous": true` and a `"note"`
explaining the uncertainty, exactly as specified.

### Original design (kept for reference)

**The problem:** if the tool loses contact with ComfyUI after submitting but before recording the
server's `prompt_id` (crash, dropped connection), the `jobs` row is stuck with `comfy_prompt_id =
NULL` forever, even though the render may have completed fine server-side. `retrieve_image` is
the natural place to notice and fix this, since it's already fetching images by filename.

**The mechanism agreed on** (see full discussion in the chat this was built in, but summarized
here): a filename+server lookup alone isn't enough to identify *which* job produced it, because
`job_uuid` was never sent to ComfyUI — the server only ever sees the graph. So:
1. `retrieve_image` finds the file by filename on a server (as it already does).
2. It fetches that server's `/history` entry for that file — giving both the real
   `comfy_prompt_id` **and** the exact graph that produced it.
3. It compares that graph against the `outputs` table for that server's jobs still missing a
   `comfy_prompt_id`, looking for an exact match.
4. **Important correction from the project owner:** don't assume a graph match is unique. Seeds
   (and entire graphs) get deliberately reused across jobs — e.g. rerunning the same settings, or
   iterating on a prompt while holding the seed fixed. Plan:
   - Among multiple exact-graph matches, prefer the one closest in time (`jobs.created_at` vs.
     the history entry's own `execution_start` timestamp) — ComfyUI's queue is FIFO, so this
     resolves the ordinary case.
   - If timing doesn't clearly separate them, **say so explicitly** in the result rather than
     silently picking one — something like "N unlinked jobs match this graph; picked the oldest
     as most likely, but can't be certain." Don't report false confidence.
5. On a resolved match: backfill `jobs.comfy_prompt_id`, set `status = 'completed'`, insert the
   `results` row (stage=`history`). Reuse `record_job_completed` — see the correction above for
   why this alone isn't quite enough, and what `record_job_reconciled` adds.
6. If nothing matches at all, that's the honest outcome for a job ComfyUI genuinely never
   received — leave it as `failed`/`rejected`, don't invent a match.

This was explicitly scoped as **lean, not exhaustive** — the project owner said plainly: "if we
plan for every edge case we'll end up with a huge project that needs a team to support it... I'm
fine if things get messy from time to time, this is a hobby project." Don't over-build this.

## Workflow templates (added 2026-09-24, `comfy_sdxl_graph.py` only)

A second, unrelated table in the same database file: `workflow_templates` (`name` PK, `description`,
`graph_json`, `placeholders_json`, `tags`, `created_at`, `updated_at`, `created_by`, `version`).
Not part of the jobs lifecycle above — no `job_uuid`, mutable in place rather than append-only
(saving over an existing name bumps `version`; no revision history is kept, since a job that used a
given version already has its exact graph in `outputs` regardless). Lets `run_workflow` take
`workflow_id=<name>` instead of a full `workflow` JSON string, so a model doesn't have to resend a
proven graph's JSON on every call. See README.md's 2026-09-24 status note for the full design and
`save_workflow`/`list_workflows`/`get_workflow`/`delete_workflow` argument reference.

## What's left to do

Nothing from the original job-DB scope. Possible future work, not currently planned:
- Consider surfacing `search_jobs` results with richer parameter info (currently just prompts;
  `retrieve_image`'s own `parameters` extraction — seed/steps/cfg/sampler/etc. — isn't duplicated
  into `search_jobs` results, since that would mean parsing every matching job's `outputs` graph
  up front rather than on demand).
- No pagination on `search_jobs` beyond `limit`/`MAX_SEARCH_RESULTS` — fine for a hobby-scale
  database, would need revisiting if the job history grows very large.

---

## Decisions made along the way (context, in case they get questioned later)

- **Performance/timing data was intentionally cut down.** An earlier draft of this feature
  included per-node timing derived from websocket `executing` events, plus tracking which nodes
  ComfyUI reported as cache-hits. The project owner said this was "just my inner geek" and has
  "little relevance to future me" — so it was dropped. All that's kept is what ComfyUI's
  `/history` response reports natively: `execution_start`/`execution_success` timestamps (both in
  ms), giving `duration_s`. Nothing fancier. If asked to re-add richer timing, that's a deliberate
  reversal of an explicit request to simplify, not a bug.
- **"Token rate / total tokens"** was in the original spec (copied from a more general/LLM-
  oriented template) but doesn't apply to an SDXL diffusion model. Flagged to the owner; agreed
  to drop rather than substitute a diffusion-specific stand-in (e.g. steps/sec) — simplicity won.
- **A real bug was caught by the test suite, not just theorized:** the first draft only
  distinguished `rejected` (bad graph, no `prompt_id` ever issued) from `failed` (accepted, then
  timed out or errored) in the `queue_only` code path. A rejection during the *normal* (non-
  `queue_only`) path was landing as `failed` instead of `rejected`, because the two-stage
  distinction lived in the wrong place (the outer caller, which couldn't tell which stage failed).
  Fixed by moving `try/except` blocks *inside* `ComfyClient.run()` itself, one around the submit
  call and one around the polling loop — that's the only place that actually knows which stage is
  failing. Worth remembering if this pattern gets copied into `comfy_sdxl_retrieve.py` or
  extended further: **stage-aware error recording belongs at the point where the stage is known,
  not pushed up to a generic caller.**
- **Docker + journald tagging all stderr as `err`/red** was investigated and confirmed to be a
  Docker log-driver quirk (stream-based severity tagging, unrelated to this project's own syslog
  writes, which go through their own path with correct per-line priority). Not something to "fix"
  in these scripts — it's an Open WebUI/Docker deployment detail, mentioned here only so it isn't
  mistaken for a bug in the job logger if it comes up again.
- **`extract_prompts()` in `comfy_sdxl_graph.py` is deliberately narrow.** It only follows the
  common `KSampler`/`KSamplerAdvanced` → `CLIPTextEncode` wiring to populate the `prompts` table
  for custom graphs; anything more exotic just gets `(None, None)` rather than an attempt to
  guess. Consistent with the "lean" instruction — not worth generalizing further unless it turns
  out to matter in practice.

---

## Quick orientation for picking this up cold

1. Read `JOB_DB_SCHEMA` in `comfy_sdxl_direct.py` first — it's the canonical copy (written
   first, then ported verbatim into `comfy_sdxl_graph.py`).
2. Read `generate_image`'s body top-to-bottom to see where each lifecycle function gets called —
   `run_workflow` in `comfy_sdxl_graph.py` mirrors it almost exactly, same call sites, same
   ordering.
3. `test_comfy_sdxl_direct.py`'s `JobDatabaseTests` class is the reference for how to test this
   kind of thing: real temporary SQLite files (not mocked), asserting on actual row contents
   across all seven tables for the built/submitted/completed/rejected/failed/delivery-failure
   cases. `test_comfy_sdxl_graph.py`'s `JobDatabaseTests` mirrors it.
4. All three tools' non-DB features (image delivery, checkpoint auto-matching, cross-server
   source-image copying, live progress via websocket, `queue_only`) are unrelated to this
   project and already stable — no need to touch them for this work.
