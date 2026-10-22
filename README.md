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
  `list_node_types`/`get_node_info` discover what's actually installed on a server (vanilla and
  custom node packs) before designing a graph. `save_workflow`/`list_workflows`/`get_workflow`/
  `delete_workflow` — once a graph works, save it as a named template in the shared job database so
  later calls pass `workflow_id=<name>` (plus small `overrides`) instead of resending the full
  graph JSON every time.
- `comfy_sdxl_retrieve.py` → `retrieve_image`, `search_jobs`, `list_jobs`, `retrieve_graph` —
  fetches a rendered image by job id or filename (or lists a server's recent jobs), searches the
  shared job history (structured JSON, for filtering/chaining), browses it as a formatted Markdown
  table (for showing the user directly — a "librarian" view, not a fetcher), and fetches the
  submitted ComfyUI graph itself for a job (most useful for run_workflow jobs, whose graphs are
  bespoke — pass it back to run_workflow, optionally edited, to resubmit).
- `comfy_sdxl_poses.py` → `list_poses`, `get_pose`, `select_pose`, `add_pose`, `delete_pose` — a
  catalog of reusable ControlNet pose reference images (pre-extracted skeleton/keypoint images
  ready for `ControlNetApply`, or raw reference photos/drawings that still need a pose
  preprocessor node), so a model can find a pose by description instead of a file path.
  `select_pose` resolves a chosen pose to a ready-to-use `source_image` reference on whichever GPU
  server you're about to render on (copying the file across servers first if needed), to pass
  straight into `generate_image`'s or `run_workflow`'s own `source_image` argument. This tool never
  renders anything itself — it's a catalog, same spirit as `run_workflow`'s workflow templates.

- `agent_notes.py` (generated, see below) → `agent_notes_help`, `agent_notes_list`, `agent_notes_list_tags`, `agent_notes_search`,
  `agent_notes_search_summary`, `agent_notes_read`, `agent_notes_read_tagged`, `agent_notes_create`, `agent_notes_append`,
  `agent_notes_edit_entry`, `agent_notes_set_weight`, `agent_notes_rank_entries` — the everyday half of a persistent notebook for agents,
  and `agent_notes_curation.py` (generated) → `agent_notes_add_tags`, `agent_notes_remove_tag`, `agent_notes_rename_tag`,
  `agent_notes_update`, `agent_notes_update_summary`, `agent_notes_delete_summary`, `agent_notes_delete`,
  `agent_notes_delete_entry`, `agent_notes_reset_hits` — the upkeep half. Two tools over one database so a small-context
  conversational model carries only the descriptions it needs (about 2,300 tokens against 3,400 for all 21 methods).
  Both are generated from `src/agent_notes_full.py`. Together they are an easier-to-drive alternative to Open WebUI's built-in `note` tool. Unrelated to ComfyUI. A note
  is a name plus an append-only log of short numbered entries, in its **own** SQLite file
  (`agent_notes.sqlite3`, `NOTES_DB_PATH` valve, default `/app/backend/data/agent_notes.sqlite3` —
  OWUI's data folder, not the ComfyUI output folder). Notes are addressed by name (unique, case-insensitive) and entries by number; there
  are no UUIDs, since a model reproduces a plain-language name more reliably than an opaque id.

- `vision_describe.py` (an Open WebUI **Filter**, pushed like `agent_duo` via `/api/v1/functions/`) and `describe_image.py` (a tool →
  `describe_image`) — let a text-only model "see" by handing images to a small vision model on an Ollama server (valves `OLLAMA_URL`,
  default `http://100.92.139.77:11434`, i.e. `ubu-gpu`; `VISION_MODEL`, default `hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M`; both send
  `think:false`, `num_ctx` 4096, `keep_alive` 10m). **The filter** runs on each request (attach it to the model in the web UI; it
  runs before the model sees the messages): every `image_url` part of a user message - a data URL, or an Open WebUI file path read
  through Open WebUI's own helper so ownership checks apply - is shrunk to `MAX_IMAGE_SIDE` (1024 px; an image costs about one token per
  28x28 pixels), described, and replaced by `[Image: ...]` text (`IMAGE_TEMPLATE`); the message becomes a plain string again. At most
  `MAX_IMAGES` (4) newest images per request are described, older ones become `SKIPPED_TEMPLATE`; descriptions are cached (`CACHE_SIZE`
  64, keyed on image + model + prompt) because an image reappears in every later request of its chat; if the vision server is down the
  image becomes `ERROR_TEMPLATE` ("could not be viewed... say so rather than guessing") and the chat carries on. **The tool**
  `describe_image(image, question)` is for images the model only has a reference to: a link (markdown image, `/api/v1/files/...`
  path, web link, data URL), a ComfyUI result file name (fetched from `COMFY_SERVERS`), or blank for the newest image in the
  conversation; `question` asks something specific instead of the general description. Measured on `ubu-gpu` (5 GB GPU, 2026-10-18):
  Qwen3.5-4B Q4_K_M uses 3.24 GB and takes about 13 s per image warm (about 20 s with a cold load); `huihui_ai/qwen2.5-vl-abliterated:3b`
  2.81 GB, about 16 s. The 7B caption model is 5.3 GB and would spill to the CPU. Create/update with
  `push_tool.py vision_describe describe_image --create` (a new Function is switched on but not global). Tests:
  `cd src && python3 -m unittest test_vision_describe`.
- `nextcloud_mail.py` (v0.5.1) — mail through the Nextcloud Mail app. `read_email` hands the model a clean plain-text body: HTML entities decoded
  (Nextcloud sends even plain-text mail HTML-escaped, with links as `<a>` tags, which showed up as `&gt;` and `<a href=...>` noise), links
  reduced to their words (`label (url)`, mailto links to the address), HTML tags and `<blockquote>`s handled like plain `>` quoting. The valve
  `QUOTED_TEXT` decides what happens to the quoted earlier messages in a reply: `omit` (default) replaces a quote at the end of the mail with
  one line (`[Quoted earlier message omitted from "Hannah Lee" ...: 4 lines]`); if the sender wrote answers between the quoted lines the
  quotes are kept, indented, without the `>` marks, because the answers need them; `clean` always does that; `keep` leaves the quoting as
  written. `MAX_BODY_CHARS` applies after cleaning. `list_emails` previews get the same treatment (entities, links, and the lone `>` marks Nextcloud leaves in a flattened preview of a reply). Tests: `cd src && python3 -m unittest test_nextcloud_mail`.
- `memory_entry_check.py` (v0.1.0) → `check_memory_entry(text)` — a writing-rules checker for the `mara-stories` memory index. Models can't count characters
  and skip steps in long instructions, so the rules live in code: given a draft entry it returns the character count, the **problems** to fix
  (over the limit, no `Title (file-name.md).` start, line breaks or markdown, labelled parts, digits, spelled-out numbers, exact schedules such as
  "monthly", a banned stock phrase, "Mara" in the third person) and **warnings** worth a look (about 50 over the target, too short, no "I", no feeling
  word, a list of things, more than one quotation, "Gordon" in a backstory memory). The story's own title is exempt from the number checks.
  Valves `TARGET_CHARS` (400), `LIMIT_CHARS` (500), `BANNED_PHRASES` (default: spin cycle, low-voltage, quiet strength). It saves nothing. Checked against
  the 19 lab drafts of 2026-10-20: it passes the good ones (all four of the local model's variant-D entries pass), and catches the real failures
  (spelled-out "nine"/"seventeen", "recording daily", "too many spin cycles", a missing title, a 510-character draft). Attached to `medium-mara`
  and `mara-duo`; `memory-guidance.md` ("Before I write") tells her to run it until it reports no problems. Tests: `cd src && python3 -m unittest test_memory_entry_check`.
- `append_file.py` (v0.3.0) → `append_file(path, text)` — adds text to the end of a file in Mara's drafts folder. Open Terminal's file API has `read`, `write` (overwrites)
  and `replace` (needs the exact old text) but no append, so a model that was told to "append, don't edit" tried `replace_file_content` with text quoted from memory and failed
  (2026-10-21, the lighthouse notes). The tool reads the file through Open Terminal's HTTP API (`GET /files/read`, `POST /files/write`, bearer key), keeps the old text
  byte for byte, adds a blank line and the new text, writes it back and checks the saved size; it creates a missing file. An Open WebUI tool cannot see `/companion` itself (the
  `open-webui` container does not mount it; `open-terminal` does), hence the API. **Open Terminal does not limit paths** (it will read `/etc/hostname`), so the tool does:
  valve `ALLOWED_ROOTS` (comma-separated folders, default `/companion/drafts`; `..` and look-alike folders such as `/companion/drafts-old` are refused) and valve `PROTECTED_FILES`
  (comma-separated; an entry is a file name matched in any folder, case ignored, or a full path; default `charter.md`; checked after the path is normalised, so `drafts/../autonomy/charter.md` is refused too).
  It refuses to rewrite a file when the read looks short (`total_lines` does not match), is not text, or is over `MAX_FILE_CHARS` (200000). It sends the calling user's Open WebUI id as
  `X-User-Id` (`SEND_USER_ID`), so the file ends up owned by that user's Linux account (Mara: uid 1001) and not by the default uid 1000. Other valves: `TERMINAL_URL`
  (`http://open-terminal:8000`, reachable from the `open-webui` container), `API_KEY` (**Gordon fills this in the web UI**; it is `OPEN_TERMINAL_API_KEY` from the compose file; the tool is useless without it),
  `MAX_APPEND_CHARS` (20000), `CREATE_IF_MISSING`, `DEDUPE_MIN_CHARS` (40). **An exact repeat is skipped:** if the file already ends with the same text (matched from the start of a line, ignoring blank lines around it) nothing is written and the tool returns a normal success reply without `added_chars`, so a model that called it twice is not confused (Mara wrote her step-5 plan twice on 2026-10-21); shorter texts such as `---` are always added. Read-modify-write, not atomic: fine for one user. Tests: `cd src && python3 -m unittest test_append_file`; also checked against the live Open Terminal on 2026-10-21 (scratch folders, deleted).
  **What Open Terminal's file API does with permissions (tested 2026-10-21):** it writes as its own process user (uid 1000) whatever `X-User-Id` says, and then hands the file to that user, so
  a file owned by 1000 with mode 644 (like `autonomy/charter.md`) *can* be overwritten through the API by any key holder, Mara's own `write_file` included; a **mode 444** file is refused ("Permission denied").
  Sending an `X-User-Id` that Open Terminal has not seen creates a Linux account and home folder for it (a probe with `test` did; removed with `userdel -r`), so only use real Open Webui user ids.
- `folder_chat_search.py` → `search_folder_chats(query, count, start_timestamp, end_timestamp)` and `view_folder_chat(chat_id, first_message)` (v0.2.0) — Open WebUI's built-in `search_chats` has no way to
  limit where it looks; this one searches only the chat folders named in the owner's valve `FOLDERS` (comma-separated, folder names in any case
  or ids; `INCLUDE_SUBFOLDERS` default on). The model cannot pick, see or override the folders, and an empty `FOLDERS` makes the tool refuse
  rather than search everything. `MAX_RESULTS` (10) is a hard cap on results, applied on top of the model's `count` (`DEFAULT_COUNT` 5 when
  it gives none). It reads `webui.db` read-only (`CHATS_DB_PATH`), only the calling user's chats, skipping the current chat, archived chats
  (`INCLUDE_ARCHIVED`) and internal ones. Query words: chats with every word first, else some of them (`matched: all|any`). Results are
  `{id, title, snippet, updated_at, folder}`. `view_folder_chat` is the folder-limited `view_chat`: it opens only a chat inside those folders (anything else answers "chat not found", the same as a missing id), follows the current branch in order, and returns whole messages up to `MAX_VIEW_CHARS` (8000), saying `first_message=N` to continue. Together they replace the built-in `search_chats` / `view_chat`, which can be hidden from the agents. Tests:
  `cd src && python3 -m unittest test_folder_chat_search`.
- `drum_machine.py` → `make_beat`, `make_song`, `list_beats` — a step-sequencer for agents: drums, bass and melody; one pattern or a whole track (v0.4.0). The model writes one line per drum
  (`kick: x...x...x...x...`; `x` hit, `X` accent, `g` ghost, `.` rest; 16 steps = one 4/4 bar; drums: kick, snare, rim, clap, hat,
  open_hat, low_tom, mid_tom, high_tom, crash, ride, cowbell, plus common spellings like `bd`/`hh`/`oh`; a JSON object is accepted too; a
  shorter lane that divides the longest repeats), with `bpm` (40-240, default 100), `repeats` (default 2), `swing` (0-60) and `title`. The
  tool renders it with drum sounds synthesized in pure Python (no numpy, soundfont, GPU or other package; MP3 via ffmpeg if present,
  else WAV) and writes a General MIDI file (drums on channel 10) the user can open in any music program; both are uploaded to OWUI's file
  storage like `voice_memo` (valves `OPEN_WEBUI_API_KEY`, `OPEN_WEBUI_BASE_URL`, `PUBLIC_BASE_URL`, `ATTACH_TO_CHAT`), kept in `BEAT_DIR`
  (default `/app/backend/data/drum_beats`) with a `.json` sidecar of the pattern, and the model gets back Markdown links. Limits:
  `MAX_SECONDS` (300; a 150 s track renders in about 5 s and peaks near 140 MB), `MAX_STEPS` (128), `repeats` up to 128. v0.3.0: lines that are not all the same length no longer fail - the pattern length is the one most lines fit (a lane whose length divides it still repeats), a line one or two steps off is padded with rests or cut, and the reply says exactly what was changed (models are poor at counting characters; the 2026-10-18 Hannah chat made 14 failed calls in a row on 15- and 17-step lines). The docstring now shows drum lanes in groups of four (`x... x... x... x...`) and `|` bar lines in bass/melody. v0.2.0 adds `bass` and `melody` (and `melody_sound`: square, saw, sine, pluck, pad): one
  token per step, separated by spaces - a note name (`C2`, `F#3`, `Bb4`; C4 is middle C; no octave = the line's default, 2 for bass and 4
  for melody), a chord joined with `+` (`C4+E4+G4`), `.` rest, `-` hold the previous note. The pitched lines must be the same length as
  the drums or a length that divides it (a shorter drum pattern loops to fill the longer tonal line; a bass-only or melody-only call
  needs no drums). Sounds are synthesized too (bass: sine plus two harmonics; square/saw softened by a low-pass; pluck: Karplus-Strong;
  pad: three detuned saws). The MIDI file puts bass on channel 1 (General MIDI program 38) and the melody on channel 2 (program 80 square,
  81 saw, 11 sine, 24 pluck, 89 pad). v0.4.0 adds `make_song`: named sections played in an order, as one audio file and one MIDI file. The `song` text has
  `[name]` headers (also `# name` or `name:` alone; text with no header is one section called `main`), each followed by the same lines as
  `make_beat` (drum lines, `bass:`, `melody:`) and optional settings `repeats:` (times the section's pattern plays each time it comes up,
  default 1, max 64), `bpm:` (a tempo change for just that section, e.g. a half-time drop), `swing:` and `sound:`. `order` lists section
  names separated by commas or `>` (`intro, build x2, drop`; blank = each once, in the order written; `x3` or `*3` repeats a name). Each
  section's lines are length-fitted like `make_beat`'s and the reply names what was fixed (`[drop] hat had 15 steps...`). The MIDI file
  carries tempo changes and the melody's program changes. Limits: `MAX_SECONDS` for the whole track, `MAX_SECTIONS` (12), `MAX_ORDER_ITEMS`
  (64). A 79 s seven-part track renders in about 2 s. `list_beats` shows a song's order. Not built yet (the MP3 from the built-in synth is good enough to
  get an agent's intent across; these are for later):
  - **Soundfont renderer** - render the same MIDI with FluidSynth (or `pyfluidsynth`) and a General MIDI soundfont (SF2, e.g. a free GM
    bank) for realistic drum kits and instruments. Needs `fluidsynth` and a soundfont file inside the Open WebUI container, which a
    container rebuild wipes unless they live on the mounted data volume or in the image, so it would be an optional `RENDERER` valve
    (`builtin` | `fluidsynth`) falling back to the built-in synth when the binary is missing.
  - **Better built-in sounds** - velocity-sensitive layers, a proper bass with a filter envelope, stereo panning/reverb, band-limited
    (anti-aliased) square/saw for high notes.
  - **Chord names** (`Am7`, `Cmaj`) expanded to notes, **per-note velocity/accents** in the tonal lines, **more than one melody line**, and
    **sample playback** (the agent uploads a WAV and uses it as a drum) .
 Tests:
  `cd src && python3 -m unittest test_drum_machine`. Created on the server with `push_tool.py drum_machine --create`; set its
  `OPEN_WEBUI_API_KEY` valve and attach it to the agent's model in the web UI.
- `voice_memo.py` → `record_voice_memo`, `list_voice_memos`, `show_last_memo` (original and spoken text of the newest memo), `record_conversation` (several named speakers as one clip, each in the voice the `SPEAKER_VOICES` valve maps to that name; limit `MAX_CONVERSATION_LINES`. With `FISH_API_KEY` set it is ONE request to Fish's own API: `<|speaker:N|>` tags in the text plus a `reference_id` list, which is Fish's documented multi-speaker form; without it, or with `NATIVE_MULTI_SPEAKER` off or more than 4 speakers, it makes one speech call per turn and joins the mp3 clips), `record_voice_file` (reads a text, Markdown, PDF or Word file from the Open Terminal container aloud, in the default voice, as one recording: the tool calls Open Terminal's HTTP API `GET /files/read` with the Open WebUI user's id in `X-User-Id`, so in multi-user mode `~` and relative paths resolve in that user's own home on the server (Hannah's `~/companion` is `/home/u36ca2354/companion`) while `/companion` is the shared mount; Open WebUI itself cannot see those folders, which is why it goes through the API. Needs the `OPEN_TERMINAL_API_KEY` valve (the compose `.env` value) and `OPEN_TERMINAL_URL` (default `http://open-terminal:8000`); `ALLOWED_FILE_PATHS` (default `/companion,/home`) restricts what it may read, checked before the request and again on the path the terminal resolved. Front matter, code blocks, HTML-style tags such as a stray `</think>`, and Markdown marks (including a `#` heading mark that follows a sentence end inside a line) are removed before speaking; text over about 1500 characters is tagged by the formatter in pieces of about 1200, and each memo's `.json` records `formatter` (`ok`, `formatter off`, or why some parts were left untagged, e.g. `1 of 3 parts: formatter changed the words`), which `show_last_memo` prints; the text is split at paragraph ends into requests of at most `MAX_CHARS`, tagged by the formatter, and joined; `MAX_FILE_CHARS` (default 9000) limits one call and the result gives the `start_line` to continue from), `voice_memo_help` — lets a model record a spoken memo: the text goes to
  a TTS model on OpenRouter (valve `TTS_MODEL`, default `fish-audio/s2.1-pro-free:free`, OpenRouter's own id
  with no OWUI connection prefix like `or.`), the MP3 is kept in `MEMO_DIR` (default
  `/app/backend/data/voice_memos`, with a `.json` sidecar holding the text) and uploaded to OWUI's file storage,
  and the model gets back a Markdown link the user clicks to play it. `DEFAULT_VOICE` is a Fish Audio
  `reference_id` from fish.audio's voice library. Expression uses `[square bracket]` tags, never asterisks
  (read aloud). Before speaking, a small chat model (valve `FORMATTER_MODEL`, default
  `mistralai/mistral-small-24b-instruct-2501`, empty = off) adds the tags: it is skipped when the text is already tagged, falls back
  to the original text on any error or if it changes the words (`FORMATTER_MIN_WORD_MATCH`), and the `.json` sidecar keeps both `text` and
  `spoken_text`; `FORMATTER_PROMPT` overrides the built-in prompt; the full tag reference for a knowledge file is `docs/fish_audio_tags.md`. Needs `OPEN_WEBUI_API_KEY` plus a speech key: `LEAD_IN_MS` (default 250: silence added in front of each recording, because Fish's audio starts after only ~40 ms and the first word can be clipped by a waking player or earbuds), `FISH_API_KEY` (Fish Audio direct, `FISH_MODEL` default `s2.1-pro-free`, avoids OpenRouter's free-model daily cap) or `OPENROUTER_API_KEY` (also used by the tag formatter).
  `PUBLIC_BASE_URL` (default the OWUI address) makes it also return a full link the model can paste into an email;
  it opens only for a browser logged in to OWUI. `ATTACH_TO_CHAT` (on) also attaches the memo to the assistant message via a `files` event so it
  shows in the chat's Files tab. No delete method on purpose. Unrelated to ComfyUI.

- `openrouter_image.py` → `generate_muse_image`, `generate_muse_image_advanced` — a paid, hosted alternative to
  `generate_image`: sends a plain-language prompt to Meta's `meta/muse-image` through OpenRouter's `/api/v1/images`
  and shows the result in the chat. The model normally supplies only a prompt (`generate_muse_image`); the image
  options are valves (`DEFAULT_ASPECT_RATIO`, `DEFAULT_RESOLUTION` 512-4K, `DEFAULT_OUTPUT_FORMAT`, `DEFAULT_SEED`,
  `DEFAULT_REFERENCE_IMAGES` for a style/character reference sent with every call). `MODEL_CAN_SET_OPTIONS` (off)
  decides whether `generate_muse_image_advanced` (per-call aspect ratio/resolution/format/seed/`reference_image`,
  where a reference is an earlier result's `chat_url` or `filename`) works; OWUI freezes a tool's parameter list at
  import time, so the switch can't remove that method from the model's tool list, it just makes it refuse. The option
  names come from the standalone ComfyUI node `openrouter_image_node.py` in the repo root (untracked, not an OWUI
  tool), since OpenRouter's own pages don't list them. `ATTACH_TO_CHAT` (on) lists each image in the chat's Files
  tab via a `files` event (OWUI's browser code replaces a message's file list with each event's, so for a second image in
  the same message the tool also sends the full list so far as a `chat:message:files` event, which the backend doesn't save); the result is kept small (`success`, `filenames`, `share_url`, `cost_usd`, `image_markdown` only in `model_embed` mode) with no
  instruction note, because models repeated or re-embedded anything URL-like they were handed; usage guidance lives in the tool's docstring. The result carries a `share_url` (`PUBLIC_BASE_URL` + the OWUI file path, opens only for
  a browser logged in to OWUI). Spending limits: `DAILY_BUDGET_USD` (rolling 24 h, default $2) and `MAX_IMAGES_PER_HOUR` (default 20), 0 = off, counted from
  the cost OpenRouter reports and kept in `spend_ledger.jsonl` in `OUTPUT_DIR` (seeded from the existing `.json` sidecars;
  calls still running count at `ESTIMATED_IMAGE_COST_USD`). A blocked call makes no request and tells the model to stop;
  this is a second layer behind the spending limit on the OpenRouter key itself. `REQUEST_EXTRA` (JSON) is merged into the request for anything else. Each image
  and a `.json` sidecar (prompt, options, cost, generation id) are kept in `OUTPUT_DIR`. No ComfyUI, no job
  database. Needs `OPENROUTER_API_KEY` and `OPEN_WEBUI_API_KEY` valves.

- `agent_duo.py` — an Open WebUI **Pipe** (a Function, not a Tool) that shows up as one model, "Agent + Agent2",
  and puts two agents living on **two different OWUI servers** into one chat with the human. See "Two-agent
  chat (agent_duo)" below. Unrelated to ComfyUI.

The four ComfyUI files share a `GPU_SERVERS` valve list and a `resolve_server`/`resolve_checkpoint`-style
matching approach, and are meant to be used together — see each tool's own `TOOLKIT` docstring
paragraph for how they hand off to one another.

## Tool arguments

Quick reference for each tool method's arguments. This is the simple version — types and what
each argument expects, not the full docstring (see each tool's own `:param:` lines in `src/` for
complete detail, e.g. exact sampler/scheduler name lists).

**"Omit" always also means "or pass `\"\"` for text, `false` for booleans"** — every tool treats an
empty string (and `null`/`none`/`default`/etc.) as equivalent to not passing the argument at all.
This matters for a calling model whose own tool-calling format won't let it actually omit a
declared parameter (e.g. some "strict" JSON-schema function-calling modes make every parameter
`required` and express optionality only via a nullable type) — see `comfy_sdxl_graph.py`'s 2026-09-25
status note below for how this was found.

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
| `loras` | Omit (or `""`, `"{}"`, `"[]"`) for none; otherwise a list like `[{"lora": "name.safetensors", "strength": 0.8}]` |
| `gpu_server` | Omit for the default server (if it isn't answering, the next configured server that does is used; the result's `server` says which); otherwise a configured server or address, used as given |
| `return_img_url` | Boolean, default `false` |
| `queue_only` | Boolean, default `false` — submit without waiting for the render |
| `verbose` | Boolean, default `false` — also return the full submitted workflow and server history |

**Server fallback (v2.3.0).** When the model omits `gpu_server`, `generate_image` first asks `DEFAULT_GPU_SERVER` (`GET /system_stats`, `HEALTH_CHECK_TIMEOUT_SECONDS`, default 3) and, if it doesn't answer (connection refused, timeout, or a 5xx from a gateway), tries the other `GPU_SERVERS` in order; it fails only if none answers, listing the ones tried. The model is not told: the result's `server` is simply the one used (so a follow-up pass naturally targets it), and the user sees a status line plus a log warning. A server the model names is never swapped. img2img with no `source_server` stays on the default (its source image lives there). `FALLBACK_TO_OTHER_SERVERS=false` turns it off. The default checkpoint and LoRAs must exist on the fallback server too, else the usual checkpoint-not-found error names what is available.

### `run_workflow` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `workflow` | A ComfyUI API-format graph, as a JSON string. Pass exactly one of `workflow` or `workflow_id` |
| `workflow_id` | The name of a template saved with `save_workflow`, instead of resending `workflow` |
| `overrides` | Only with `workflow_id`. JSON object of parameter name → new literal value, for that template's registered placeholders, e.g. `{"positive_prompt": "...", "seed": 12345}` |
| `source_image` | Omit to leave the fixed source-image node pointed at a placeholder; otherwise an earlier result's `use_as_source_image` value |
| `source_server` | Omit unless `source_image` is on a different server than `gpu_server` |
| `gpu_server` | Omit for the default server |
| `return_img_url` | Boolean, default `false` |
| `queue_only` | Boolean, default `false` |
| `verbose` | Boolean, default `false` — also return the prepared graph and server history |

### `list_node_types` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `search` | Omit to list everything (capped by `MAX_NODE_LIST_RESULTS`); otherwise a substring match over class_type, display name, and category |
| `gpu_server` | Omit for the default server |
| `refresh` | Boolean, default `false` — re-fetch instead of using this process's cached copy (only needed right after installing/removing a custom node pack) |

### `get_node_info` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `class_type` | Required. Exact class_type, as returned by `list_node_types` |
| `gpu_server` | Omit for the default server |

### `save_workflow` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `name` | Required. A slug (lowercase letters/digits/underscore/hyphen, 2–64 chars) — becomes `run_workflow`'s `workflow_id` |
| `workflow` | Required. The ComfyUI API-format graph as a JSON string, already proven to render via `run_workflow` |
| `description` | Required. What the graph does and when to use it — shown by `list_workflows`, not the JSON itself |
| `placeholders` | Omit for none; otherwise a JSON object of parameter name → `[node_id, input_name]`, pointing at a literal (non-link) input in the graph |
| `tags` | Omit for none; otherwise comma-separated, e.g. `"controlnet,pose"` |
| `overwrite` | Boolean, default `false` — must be `true` to replace an existing name |

### `list_workflows` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `tag` | Omit for all; otherwise an exact tag |
| `search` | Omit for all; otherwise a substring match over name and description |
| `limit` | Integer, default `20` |

### `get_workflow` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `name` | Required. The template's name, as shown by `list_workflows` |

### `delete_workflow` (comfy_sdxl_graph.py)

| Argument | Expected value |
|---|---|
| `name` | Required. The template's name, as shown by `list_workflows` |

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
| `job_search` | Matches a job's id (its own UUID or ComfyUI's own prompt id) or filename, by substring |
| `checkpoint` | Substring of a checkpoint filename |
| `seed` | Exact integer seed |
| `gpu_server` | Omit to search all servers |
| `tool` | `"generate_image"` or `"run_workflow"`; omit for both |
| `status` | Omit (or `""`, `"[]"`) for all; otherwise one or more of `built`, `queued`, `completed`, `rejected`, `failed` (a JSON-string list is also fine) |
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

### `list_poses` (comfy_sdxl_poses.py)

| Argument | Expected value |
|---|---|
| `search` | Omit for all; otherwise a substring match over pose_id and description |
| `tags` | Omit for all; otherwise an exact tag |
| `kind` | Omit for both; otherwise `"skeleton"` or `"reference"` |
| `limit` | Integer, default `20`, capped by the `MAX_POSE_LIST_RESULTS` valve |

### `get_pose` (comfy_sdxl_poses.py)

| Argument | Expected value |
|---|---|
| `pose_id` | Required. A pose's id, as shown by `list_poses` |

### `select_pose` (comfy_sdxl_poses.py)

| Argument | Expected value |
|---|---|
| `pose_id` | Required. A pose's id, as shown by `list_poses` |
| `gpu_server` | Omit for the default server — the server you intend to render on |

### `add_pose` (comfy_sdxl_poses.py)

| Argument | Expected value |
|---|---|
| `pose_id` | Required. A slug (lowercase letters/digits/underscore/hyphen, 2–64 chars) — becomes `select_pose`'s/`get_pose`'s/`delete_pose`'s argument |
| `description` | Required. Plain-language description of the pose — what `list_poses` searches |
| `kind` | Required. `"skeleton"` (pre-extracted, ready for `ControlNetApply`) or `"reference"` (raw photo/drawing, needs a preprocessor first) |
| `source_image` | An image already in a ComfyUI server's output folder. Give this XOR `openwebui_file` |
| `openwebui_file` | An image already in Open WebUI's file storage — bare file id, or the `/api/v1/files/<id>/content` path/URL. Give this XOR `source_image` |
| `gpu_server` | Omit for the default server — where `source_image` already lives, or where `openwebui_file`'s copy is uploaded |
| `tags` | Omit for none; otherwise comma-separated, e.g. `"standing,action,openpose"` |
| `overwrite` | Boolean, default `false` — must be `true` to replace an existing pose_id |

### `delete_pose` (comfy_sdxl_poses.py)

| Argument | Expected value |
|---|---|
| `pose_id` | Required. A pose's id, as shown by `list_poses` |

### `agent_notes` tool arguments (agent_notes_full.py → agent_notes.py, agent_notes_curation.py)

**One source, two deployable tools.** `src/agent_notes_full.py` is the only file to edit: the database layer, validation
and all 21 tool methods, and what `src/test_agent_notes.py` and the other tests exercise. `scripts/build_agent_notes.py`
generates the two files Open WebUI gets from it (each is a complete, self-contained tool, since OWUI can't import
siblings; `# GENERATED` marks them) by cutting the other tool's methods and valves out:

| Tool (id) | Methods | Valves |
|---|---|---|
| **Agent Notes** (`agent_notes`) | `help`, `list`, `list_tags`, `search`, `search_summary`, `read`, `read_tagged`, `create`, `append`, `edit_entry`, `set_weight`, `rank_entries` | all of them |
| **Agent Notes Curation** (`agent_notes_curation`) | `add_tags`, `remove_tag`, `rename_tag`, `update`, `update_summary`, `delete_summary`, `delete`, `delete_entry`, `reset_hits` | `NOTES_DB_PATH`, `DEFAULT_AUTHOR` |

A method must be assigned to exactly one of the two lists in the build script (it refuses to build otherwise). After editing
`agent_notes_full.py` run `python3 scripts/build_agent_notes.py` (`--check` exits 1 if a generated file is stale, and
`test_agent_notes_split` fails on that too). Give a conversational model **Agent Notes**; give a curating or
summarizing agent **both**. The curation tool has no `help` of its own (two methods with one name would clash): the everyday
tool's `agent_notes_help` explains the curation tools too and says which tool they live in. Both tools have a
`NOTES_DB_PATH` valve and must be pointed at the same file. The tables below list every method; the ones in the
curation list above belong to the second tool. `notes_web.py`, `agent_duo` and the autonomy charter are unaffected: they use
the database functions (in both files) or the everyday methods.

Same `""`/`false` = omitted convention as above. Limits: note name 60 characters, note comment 500,
entry text 500 — enforced by the tool (with an actionable message) and by SQLite `CHECK`s.

`continues=true` (v1.8.0) cuts the text at a paragraph, line or word boundary into pieces of at most 500
characters and saves them as consecutive entries sharing a `chain_id` (the first piece's entry number; NULL for an
ordinary entry). The text is stored exactly as given — no markers. `agent_notes_read`, `_search` and `_read_tagged`
add `continues_at` (the next surviving piece) and `continues_from` (the previous one) to pieces of a chain, worked
out at read time so editing or deleting a piece never leaves a dangling pointer. In plain `_read_tagged` such an
entry is a small `{text, continues_*}` object instead of a bare string. The reply to the write gives
`entry_numbers`. The `MAX_CONTINUATION_ENTRIES` valve (default 6, about 3000 characters per call) caps the
pieces; over it, nothing is saved. `notes_web.py` shows the links beside each entry.

Small models can get stuck on a note they feel they can't shorten, so (v1.11.0) the `AUTO_CONTINUE_AFTER` valve
(default 3, `0` = never) saves the text as if `continues=true` had been passed once it has been sent too long that many
times in a row. The refusals themselves are unchanged (shorten it, or pass `continues=true`) and never mention the
fallback, because the 500-character limit exists to push models towards concise notes; the saving call's reply is an
ordinary success (it lists `entry_numbers`) and each automatic save is logged at INFO (`agent_notes: ... times in a
row; saved it as N linked entries`) so overuse can be spotted. "In a row" means the same author and note, each refusal
within 15 minutes of the last, with no successful write to that note in between. A text too long even for
`MAX_CONTINUATION_ENTRIES` entries is still refused and isn't counted. The counts live in memory (OWUI keeps the loaded tool cached while its source is unchanged), so a restart or a new
push just resets them.

Popularity and weight (v1.9.0, reworked in v1.12.0): `note_id.hits` and `note_data.hits` count reads. `note_data.weight` is
how a model records a *feeling* about an entry: a score from **1 to 10, 5 neutral** (lower = negative or painful, higher = positive or
joyful; the distance from 5 is how strong the memory is). It is stored as score × 10 in the existing 0–100 column
(`CHECK`ed), so the default of 50 and every older value need no migration; a value between multiples of ten is shown
rounded. The scale is converted at the database-function boundary (`create_note_db` etc. take and return scores;
`weight_to_score()` converts a stored value for display, and the web viewer uses it). A coarse scale because a
hundred-point one invites small models to pick arbitrary, inconsistent numbers.
A "read" is counted by `agent_notes_read` (the note, plus each entry delivered, strong memories included; `count=false` skips it
for routine sweeps) and `agent_notes_read_tagged` (each note and entry actually delivered). Search cards are *not* reads
(a card is a few words of the text); fetching the entry with `agent_notes_read entry=N` is. `agent_notes_rank_entries` and
`notes_web.py` never count either, so asking for the most popular entries doesn't make them more popular. A failed count is
logged and never fails the read. Neither counter nor weight touches `updated_at`, so summaries don't go stale. Plain replies
show an entry's `weight` only when it isn't 5; `verbose=true` adds `hits` and `weight` everywhere (and a Reads column in
`agent_notes_list`). The web viewer shows each note's and entry's read count and score and has reset buttons.

**Reading in tiers (v1.12.0).** `agent_notes_read` used to return the newest 50 entries (up to ~17,000 characters on the
big notes - over half of an 8k context) and said nothing about the rest. Now a default read returns, in one reply kept to
`READ_MAX_CHARS` (default 6000 characters):
the `READ_DEFAULT_RECENT` (10) newest entries; plus under `strong_memories` up to `READ_STRONG_EACH` (5) of the note's strongest
older *positive* memories (score ≥ `STRONG_HIGH`, default 7) and 5 *negative* ones (score ≤ `STRONG_LOW`, default 3), strongest first.
Strength is distance from neutral in either direction, so a painful memory is as likely to come along as a joyful one.
The size budget is shared out one entry at a time (newest, strongest positive, strongest negative, next newest...), so a tight
budget still carries both kinds, and whole entries only. The reply says how many entries it left out. Other modes: `before=N` pages back
(older than entry N, with where to continue), `entry="12, 15"` reads those entries in full (the follow-up to a search card, at most
ten), `sample=N` returns N random entries from outside the newest ones, `count=false` keeps a sweep out of the popularity counts.
`limit` still means "how many recent entries" (capped by `MAX_READ_ENTRIES`), so existing callers keep working.

**Note kind and summary limit (v1.13.0).** Two owner-set columns on `note_id`, changed with `agent_notes_update` (curation tool) or
on a note's page in `notes_web.py`; the everyday tool cannot change them, so notes the agents create start as plain memory notes
with the default limit. `kind` is `memory` (the default: a weight is how it felt, so `agent_notes_read` offers strong positive and
strong negative older entries) or `persona` (a weight is how strongly the trait is the agent's: a read offers only the strong
side, and `agent_notes_read_tagged` fills its size budget heaviest entry first, listed strongest first, instead of newest first).
`summary_max` (NULL = the default 500, otherwise 100-2000) is that note's summary length limit, enforced by `update_summary`
and shown by `agent_notes_list` as "up to N"; a `Kind` column appears there only when some note is a persona note. The old
fixed 500-character CHECK on `note_summary` is replaced on first use (existing summaries are copied over) since a CHECK can't
vary per note. `Nightly_Summarizer.md` and `Weighting_Guidance.md` say how the summarizer and the agents use them.

**Search is a card catalog (v1.12.0).** `agent_notes_search` no longer returns whole entries. It returns cards - note, entry
number, score, date and a snippet of `SEARCH_SNIPPET_CHARS` (120) characters cut around the match - for entries containing *all*
the query's words (any case), ordered by strength of feeling first (furthest score from neutral, either direction) and then
newest, with `total_matching_entries`, `page=` for the rest, and `note=` to search one note. A search for a word that
appears in hundreds of entries therefore surfaces the memories that mattered, cheaply; `agent_notes_read entry=N` then pulls the
full text of the ones worth it. Notes whose name, comment or tags match are listed on the first page. (SQLite FTS5 relevance
ranking is available in the OWUI container but would be ignored by this ordering, so matching is plain per-word `LIKE`.)

`chain_id` is added in place on first connect (nullable, so an older copy of the tool can still write to the
database; back it up first if it matters). v1.7.x briefly marked pieces with a trailing ` [continues at entry #N]`
in the text; that upgrade converts such entries to chains and strips the marker.

`agent_notes_list`, `_read`, `_search`, `_search_summary` and `_read_tagged` take `verbose` (default `false`):
plain replies drop authors, timestamps and other bookkeeping to save a small model's context. `_read` and
`_search` still return `entry_no` (needed to edit/delete); `_read_tagged` plain returns only note names,
summaries and bare entry-text strings. `verbose=true` restores the full detail.

| Method | Arguments |
|---|---|
| `agent_notes_help` | optional `topic`: `overview` (default), `find`, `read`, `write`, `long_text`, `organize`, `summaries` or `rating`; a unique prefix works. Returns a short guide (under ~1600 characters, written for small context windows) plus the other topic names. Limits and valve values in the text come from the code in force, and a test fails if any tool method isn't covered by some topic — so adding a method means adding it to `help_topics()` in `agent_notes.py` |
| `agent_notes_list` | `limit` (default `20`, capped by `MAX_LIST_RESULTS`); `needs_summary=true` lists only notes with no summary or a stale one, skipping notes under the `MIN_SUMMARY_CHARS` valve (default 1000 characters of entry text). The table has a Summary column: none/current/stale/short (short = too small to need one) |
| `agent_notes_read` | `name` (required); `limit` (recent entries, default `READ_DEFAULT_RECENT`, max `MAX_READ_ENTRIES`); `before`, `entry`, `sample`, `count`: see "Reading in tiers" above. Returns `entries` plus `strong_memories` |
| `agent_notes_create` | `name`, `text` (first entry) required; `comment` optional; `weight` (1–10, default 5) rates the entry; `continues=true` saves a text over 500 characters verbatim as several consecutive entries. Refuses if the name already exists — use `agent_notes_append` |
| `agent_notes_append` | `name`, `text` required; `continues=true` and `weight` as for create (every piece of a long text gets the weight) |
| `agent_notes_edit_entry` | `name`, `entry_no` (e.g. `3` or `"#3"`), `text` — all required; `weight` optional (omitted = keep the current one) |
| `agent_notes_delete_entry` | `name`, `entry_no` — both required. Other entries keep their numbers; numbers are never reused |
| `agent_notes_update` | `name` required; any of `new_name`, `comment`, `clear_comment=true` (an empty `comment` means "unchanged", so clearing needs the flag), `author_name` (re-attributes the note's starter, not its entries), `kind` (`memory` or `persona`) or `summary_max` (this note's own summary limit, 100-2000, `reset` for the default 500). Author, kind and limit alone leave `updated_at` alone so summaries stay current |
| `agent_notes_delete` | `name` required. Removes the note and all its entries, no confirmation |
| `agent_notes_search` | `query` required; `limit` (cards per page, default `10`, capped by `MAX_SEARCH_RESULTS`), `page`, `note` (one note only). Returns cards, strongest feeling first then newest, plus matching notes |
| `agent_notes_update_summary` | `name`, `summary` (≤500 chars, or the note's own `summary_max`; whitespace collapsed) required; `author_name` optional. Creates or replaces the note's one summary |
| `agent_notes_search_summary` | `query` required; `limit` (default `10`, capped by `MAX_SEARCH_RESULTS`). Splits the query into words, any word can match a note name or summary, ranked by words matched; each result carries `stale` |
| `agent_notes_delete_summary` | `name` required. Refuses if the note has no summary |
| `agent_notes_add_tags` | `name`, `tags` (one, or comma-separated) required; `create=true` to make a new tag that resembles existing ones. Refuses a new tag that looks like an existing one (returns the similar tags), reuses one that matches ignoring case/hyphens/plurals; nothing is saved if any tag is refused. Max 10 tags per note, 40 chars each. Punctuation is cleaned rather than refused (`&` becomes `and`, apostrophes dropped, other symbols become hyphens) and the reply lists the change under `adjusted` |
| `agent_notes_remove_tag` | `name`, `tag` required. A tag no note carries any more is deleted |
| `agent_notes_read_tagged` | `tags` required (one or comma-separated); `match` = `any` (default, best match first) or `all`. Returns each matching note's tags, summary and newest entries within the `MAX_TAGGED_CHARS` valve (default 6000 characters): whole strings only, headings for all notes first, then entries newest-first rotating across the notes so one long note can't starve the rest. Reports `entries_not_shown` per note and `notes_left_out` |
| `agent_notes_set_weight` | `name`, `entry_no` required, plus exactly one of `weight` (1–10) or `change` (signed amount, e.g. `-2`, clamped to 1–10; below -9 is refused). Doesn't alter text or `updated_at` |
| `agent_notes_reset_hits` | `name` required; `entry_no` resets just that entry's read count, otherwise the note's count and all its entries' |
| `agent_notes_rank_entries` | `name`, `by` required: `strongest` (strong memories on both sides, furthest from 5 first), `most_popular`, `least_popular`, `above_weight` or `below_weight` (the last two need `weight`; strict comparison, so `above_weight 5` = rated positive). `limit` (default `10`, capped by `MAX_SEARCH_RESULTS`). Ties go to the newest entry. Returns `weight` and `hits` with each entry and `total_matching`. Does not count as a read |
| `agent_notes_list_tags` | optional `query`. Tags in use with note counts. `agent_notes_list` also takes `tag=` to filter, shows a Tags column and lists the tags in use |
| `agent_notes_rename_tag` | `tag`, `new_tag` required; merges into `new_tag` if it already exists |

Tags (v1.4.0): `tag` (`tag_name` unique, case-insensitive, stored lowercase-hyphenated) and `note_tag`
(`note_pk`, `tag_id`, both cascading). Tags belong to the parent note, not to entries, and tagging
doesn't touch `updated_at`, so it never makes a summary stale. Free-form tags with guardrails instead
of a curated list: the tool shows existing tags, folds variant spellings onto them, and makes the
model confirm (`create=true`) before adding a look-alike. `agent_notes_rename_tag` is the cleanup for drift that
gets through. Existing databases gain the two tables automatically.

Every delete is a real `DELETE` (no soft-delete/undo); deleting a note relies on SQLite's
`ON DELETE CASCADE`, so the tool enables `PRAGMA foreign_keys` on every connection. Schema: `note_id`
(internal integer key, `note_name` with a unique case-insensitive index, `note_comment`,
`next_entry_no` counter, timestamps) and `note_data` (`note_pk`, `entry_no`, `note_text`,
`created_at`, `edited_at`, `author_id`), entries ordered by `created_at`. Authors (v1.1.0):
`note_author` (`author_id` autonumber, `author_name` unique ignoring case, `created_at`); both other
tables carry an `author_id` (the note's starter, the entry's writer; editing doesn't change it).
Author #1 is the legacy author (`LEGACY_AUTHOR` in the source), seeded into every database and given every row that predates authors.
`agent_notes_create`/`agent_notes_append` also take an optional `created_at` (v1.2.0) so old notes can be moved in
with their real dates: ISO 8601 (a bare date, or with time/offset/`Z`; no offset = UTC) or a Unix
epoch in s/ms/µs/ns (Open WebUI's own notes use ns), stored as UTC ISO; unparseable, future (>1 day)
or pre-1970 values are refused, and blank means now. A backdated append never moves the note's
`updated_at` backwards, and entries read back in time order, so a backdated entry shows before
newer ones despite its higher number.
`agent_notes_create`/`agent_notes_append` take an optional `author_name` (max 60); if blank the tool uses the
model name Open WebUI passes as `__model__` (name, else id), else `Unknown agent`. An existing
database is upgraded in place on first connect (nullable `author_id` columns added, old rows set to
#1) — back it up first if it matters. Nullable also means an older copy of the tool can still write
to an upgraded database; its rows just show author `unknown`.

Summaries (v1.3.0), for a scheduled sub agent to keep current so agents can search them RAG-style:
`note_summary` has one row per note (`note_pk` primary key, so `agent_notes_update_summary` is an upsert; cascades
on note delete, survives rename) with `summary_text` (≤500), `author_id`, `last_summarized`, and
`source_updated_at` + `source_entries` — a snapshot of the note when it was summarized. A summary is
**stale** when the note's `updated_at` or entry count no longer matches the snapshot (the count
catches backdated appends, which don't move `updated_at`). Staleness is computed, never stored, and
a note edited between the summarizer reading it and writing the summary is treated as current until
its next change. Search is word-based `LIKE` over name + summary (no FTS5/embeddings) — fine for
hundreds of notes. `agent_notes_read` includes the summary; `notes_web.py` shows it. New table only, so an
older tool copy keeps working against the upgraded database. `notes_web.py` attributes its writes
to `--author` (default `Gordon`).

## Two-agent chat (agent_duo)

`src/agent_duo.py` is a Pipe installed on one server (open-webui). Pick the "Agent + Agent2" model there and
send a message: the pipe runs a bounded round of alternating turns, Agent on its own server and Agent2 on its,
and streams both into the chat as `**Agent:** ...` / `**Agent2:** ...`. Each agent keeps its own persona,
tools (`agent_notes`, mail, ...) and knowledge because every turn really runs on her server. The speaker
labels in the saved chat are how the pipe works out who said what on your next message, so don't edit them.

How a turn works (found by spiking against the live servers, 2026-10-10): a bare `POST /api/chat/completions`
does **not** apply a preset's `toolIds` and does **not** execute tool calls (it hands raw `tool_calls` back). The
UI gets tools because it sends `tool_ids` itself and names a chat. So per turn the pipe, on the agent's own server:
reads the preset's `meta.toolIds`; creates a throwaway chat (`[agent_duo] <name>`); posts to `/api/chat/completions`
with `chat_id`, `id` (the empty assistant message), `session_id`, `tool_ids` and the shared transcript as
`messages` (the server then runs the whole tool loop in the background); polls `GET /api/v1/chats/{id}` until the
message is `done`; takes its `content` (tool detail is in `output`, and any `<details>` block is stripped anyway);
and deletes the throwaway chat (only ones the pipe itself created). Each agent sees her own turns as `assistant`
and everything else as `user` messages prefixed `[Name]:`, plus a short turn-taking note.

Round rules: up to `MAX_TURNS` agent turns per human message, alternating; whichever agent the human's message
names (alone) goes first, else `FIRST_SPEAKER`. An agent can end the round by making her whole reply exactly `[PASS]`
(`END_MARKER`); the marker mixed into other text is just removed (v1.1.1 - before that, a model that echoed `[PASS]`
at the start of a real reply ended the round, so the other agent never spoke). Title/tag/follow-up tasks Open WebUI sends to the selected model get a canned answer, so they
don't wake the agents (set a separate Task Model if you'd like real titles). A failure on either side is shown
in the chat as an italic note and ends the round.

Valve names are per agent: `<A>` and `<B>` stand for the two agents' names in capitals, as they appear in the
Valves panel.

| Valve | Meaning |
|---|---|
| `<A>_URL` / `<B>_URL` | That agent's OWUI base URL as the *pipe's server* reaches it (`http://localhost:8080` for the local one) |
| `<A>_KEY` / `<B>_KEY` | An API key (Settings -> Account -> API Keys) on that server, able to use the agent's model. Keep these only in the Function's valves |
| `<A>_MODEL_ID` / `<B>_MODEL_ID` | The agent's model preset id on her server (see the valve defaults in the source) |
| `<A>_TOOL_IDS` / `<B>_TOOL_IDS` | Comma-separated override; blank reads the preset's own `toolIds` |
| `<A>_NAME` / `<B>_NAME` / `USER_NAME` | Speaker labels |
| `MAX_TURNS` (4), `FIRST_SPEAKER`, `END_MARKER`, `TURN_TIMEOUT_SECONDS` (300), `POLL_SECONDS` (2) | Round controls |
| `CARRY_CONTEXT` (on), `CARRY_TOOLS` (`agent_notes_read_tagged`), `CARRY_MAX_CHARS` (80000), `CARRY_TTL_MINUTES` (240) | Carry-forward, below |
| `KEEP_BACKING_CHATS` | Leave each turn's throwaway chat in place (to inspect tool calls) instead of deleting it |

**Carry-forward (v1.1.0).** Every turn is a fresh call that sees only the *text* of earlier turns, so an agent's
session-start reads (her persona and memories) would be lost after her first turn and reloaded every turn - far
too heavy for a local model (a measured agent startup read was ~20k tokens, ~157k processed over one reply's tool
round trips). So after an agent's first turn in a chat, the pipe takes the results of the tools in `CARRY_TOOLS`
from that turn's saved `output` (pairing each result with its call by `call_id`) and puts them, unchanged, at the
end of her system note on her later turns, headed "Already loaded earlier in this conversation". Her own "skip if
already visible" rule then holds. Tools not in `CARRY_TOOLS` (timestamps) are redone each turn. The block is
byte-identical every turn and sits before the changing transcript, so a model server that caches the prompt
prefix (Ollama normally does - *not yet verified here*) only has to process the new part. Results are kept in the
pipe's memory per front chat and agent: gone after `CARRY_TTL_MINUTES` idle or a server restart (the agent then
reloads once and it is caught again), at most 50 chats, and not carried at all if over `CARRY_MAX_CHARS`. The
persona is still in her context on every turn; this removes the repeated reads, not the 20k tokens.

Caveats: a turn takes as long as the model does (two turns measured about 73 s); a stopped chat stops waiting but
the remote agent may finish its turn; images and files in the human's message aren't forwarded to the agents.

Each turn's `[agent_duo] <name>` chat is deleted right after it finishes unless `KEEP_BACKING_CHATS` is on, or the
delete call itself fails. Either way they can pile up on both agents' servers; `scripts/cleanup_duo_chats.py`
deletes ones past `--min-age-minutes` (default 60) across every server `secrets.md` configures (same `--server`
flag as `push_tool.py`), human-run only, same reasoning as `cleanup_noop_chats.py` - deleting a model's own chats
stays off the model-callable surface. `--dry-run` previews; `--yes` skips the prompt for cron; `--log FILE` appends
each run's output with UTC timestamps. Tests: `cd scripts && python3 -m unittest test_cleanup_duo_chats -v`.

Deploy: create the Function once in OWUI (Admin -> Functions -> "+", paste `src/agent_duo.py`), enable it, fill the
valves, then add its id to `secrets.md` as `agent_duo: <id>` so `python scripts/push_tool.py agent_duo` can update
it. Tests: `cd src && python -m unittest test_agent_duo -v`.

## Read-aloud with Fish Audio directly (Open WebUI Audio settings)

Open WebUI's own read-aloud button can call Fish Audio without OpenRouter. Fish has an OpenAI-compatible
endpoint, so this is a settings change only (the `voice_memo` tool has its own `FISH_API_KEY` valve and is separate).
Set it in **Admin Settings -> Audio** on each server (this page is separate from Connections):

| Setting | Value |
| --- | --- |
| TTS engine | OpenAI |
| API base URL | `https://api.fish.audio/compat/v1` |
| API key | that server's own Fish API key |
| TTS model | `fish-audio/s2.1-pro-free` (plain `s2.1-pro-free` also works; no `:free` suffix, no `or.` prefix) |
| Voice | a Fish voice id (`reference_id`); OpenAI preset names such as `nova` are rejected with a 400 |

Verified 2026-10-13 with a real key: both free model names returned audio/mpeg. `fish-audio/s2.1-pro` (the paid
model) returned HTTP 402 "Insufficient API credit" on a free account, so do not select it. The endpoint's default
format is PCM, but Open WebUI sends `response_format: mp3` itself. Fish refuses an `instructions` field (400); the
OpenAI engine does not send one.

Things to know:
- If `audio.tts.openai.params` was ever set, it overrides the model field; make sure it does not hold an old
  `or.` model name.
- Open WebUI looks up `/audio/voices` on the endpoint; a 404 there is logged but harmless.
- Read-aloud splits a reply into many small requests (about 35 in 3 seconds was seen against OpenRouter's limit).
  If Fish throttles, set response splitting to paragraphs in the same Audio settings page.
- The speech cache keys on the request, so replies read aloud before the switch are generated again once.
- Fish audio starts speech after only ~40 ms of silence, so earbuds that are still waking can clip the first word.
  The `voice_memo` tool pads its clips (`LEAD_IN_MS`); read-aloud audio comes straight from Fish and is not padded.
- Fish's free-tier limits are not stated in the API docs; watch the usage page at https://fish.audio/app/developers.

## Job database

Every render attempt across `comfy_sdxl_direct.py`/`comfy_sdxl_graph.py` — including ones ComfyUI
rejected or that timed out — is durably logged to a **shared SQLite database** (one file,
`JOB_DB_PATH`, identical across all four tools' valves). The same file also holds
`workflow_templates` (saved graphs for `run_workflow`'s `workflow_id`, see above) and
`pose_references` (the pose catalog behind `comfy_sdxl_poses.py`, see above) — both separate,
mutable tables, not part of the append-only job log. Full schema, design rationale, and current
status: `src/SQLITE_JOB_DB_HANDOFF.md`. Read that file before touching job-DB code.

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

### Sample file server (scripts/serve_samples.py)

A tiny read-only web server for listening to and downloading generated audio (or any file) from a browser, instead of
copying files off the server by hand. Standard library only; tests in `scripts/test_serve_samples.py`.

```
python3 scripts/serve_samples.py                  # serves tmp/samples on 0.0.0.0:8899 and prints its Tailscale address
python3 scripts/serve_samples.py --dir DIR --port 9000
python3 scripts/serve_samples.py --host 127.0.0.1 # this machine only (then: ssh -L 8899:127.0.0.1:8899 <this-host>)
```

It listens on every interface by default, so it is reachable from the LAN and over Tailscale; there is no login, so keep the
port off the Internet. The index
lists every file under the folder, newest first, with a player and a download link for audio; a text file named like an audio
file (`clip.mp3` + `clip.txt`) is shown under its player as a caption. Range requests work, so long recordings can be
sought. Only GET and HEAD are accepted, and any path that leaves the folder, including through a symlink, gets a 404.
`tmp/` is gitignored scratch space, so samples put there are never committed.

### Notes web interface (scripts/notes_web.py)

A standalone LAN web UI for the `agent_notes` database, run from the docker host with no OWUI
involved: `python3 scripts/notes_web.py --db /path/to/agent_notes.sqlite3 [--host 0.0.0.0] [--port 8765]`.
Browse, search, create, append, edit/delete entries, rename/re-comment and delete notes, tag notes (filter by tag, `/tags` page to rename/merge), all through
`agent_notes.py`'s own `*_db` functions (same limits and transactions as the tool). Server-rendered
HTML, stdlib only apart from the `pydantic` that importing `agent_notes` needs. No auth and no CSRF
protection by design — trusted LAN only. Each row of the home page has two small buttons, each behind a browser confirmation: a red ✕ that deletes the note and a trash can that deletes all its entries but keeps the note (name, comment, tags; its summary goes stale; entry numbers carry on) — the latter is `empty_note_db`, deliberately not a model tool. A note page lists its entries newest first. Tests: `cd scripts && python3 -m unittest test_notes_web -v`.

### Automating the rating pass (scripts/rating_pass.py)

`python3 scripts/rating_pass.py --model medium-mara --note internal-life [--before N] [--chunks 30] [--delay 45] [--server hannah]` runs the
chunked rating pass from `Weighting_Guidance.md` for you: each chunk is a fresh throwaway chat on the agent's own server (the same
server-side tool loop `agent_duo.py` uses, with the tools attached to her model preset), in which she reads one page of the note and rates
up to 8 entries. The script takes the next `before=` from what her read actually returned (not from what she says), waits `--delay`
seconds, and goes on until the note's start; a rate-limit error is waited out and retried, and it stops if a page doesn't move back or her
read never ran. `--dry-run` prints the prompt and the tools attached to the preset; `--persona` rates strength of character for a persona facet; `--note a,b,c` does several notes one after another (same kind, since `--persona` applies to all; `--chunks` and `--before` are per note, and `--before` needs a single note; a failure stops the whole run and names the notes not yet done); `--keep-chats` leaves each chunk's chat to inspect.
It is still her rating her own memories on her own model; the script only does the "repeat, with before=NNN" part. Tests:
`cd scripts && python3 -m unittest test_rating_pass`.

### Database housekeeping queries

Run these on the host with `sqlite3 <path to agent_notes.sqlite3>` (Mara: `~/docker/open-webui/data/`; Hannah: see `HANNAH_SQLITE_PATH` in
`secrets.md`). Copy the file first (`cp agent_notes.sqlite3 agent_notes.bak`) and run the cull in a transaction so you can check the count before committing.

```sql
-- Remove every summary (the Nightly Summarizer rebuilds them)
DELETE FROM note_summary;

-- Reset all read counts (notes and entries)
UPDATE note_id SET hits = 0;
UPDATE note_data SET hits = 0;

-- Cull entries older than 1 week from one note (here autonomy-log); change '-7 days' as needed
BEGIN;
DELETE FROM note_data
 WHERE note_pk = (SELECT note_pk FROM note_id WHERE lower(note_name) = 'autonomy-log')
   AND created_at < strftime('%Y-%m-%dT%H:%M:%f', 'now', '-7 days');
SELECT changes();   -- how many were removed
COMMIT;             -- or ROLLBACK;
```

Timestamps are stored as ISO text in UTC, so the comparison is a plain string compare. Entry numbers are never reused (`next_entry_no`
keeps counting), so culling leaves gaps. Culling does not touch the note's `updated_at`; clear that note's summary afterwards
(`DELETE FROM note_summary WHERE note_pk = (SELECT note_pk FROM note_id WHERE lower(note_name) = 'autonomy-log');`) if you want it re-summarized.

**Culling by age: `scripts/cull_notes.py`.** The cull query above, as a script with a backup and a preview. It keeps only entries created
in the last `--days` days from the named note(s) (`--note a,b`), keeps a linked `continues=true` chain whole if any piece is inside the
window, never deletes the note itself, and leaves `updated_at` and entry numbers alone. `--dry-run` reports counts only; otherwise it asks
(or `--yes`), copies the database to `tmp/agent_notes_before_cull_*.sqlite3` (`--no-backup` skips), and culls all notes in one transaction.
`--clear-summary` also drops the summary of any note that lost entries. `--db` points it at another file (Hannah's). Example:
`python3 scripts/cull_notes.py --note autonomy-log --days 7 --clear-summary`. Tests: `cd scripts && python3 -m unittest test_cull_notes`.

### Host access to the databases (ACLs)

The notes database (`agent_notes.sqlite3`) lives in OWUI's data volume, `<install>/data/`, and the ComfyUI job
database (`comfy_jobs.sqlite3`) in `<install>/data/comfy_outputs/` (e.g. `~/docker/open-webui/...` or `~/nvme/open-webui/...`). The OWUI container
runs as root, so that folder and anything it creates are `root:root`, and an unprivileged host user can't
write there. `notes_web.py`, `init_job_db.py`, `import_server_history.py` and plain `sqlite3` need
read/write access as the host user, and writing needs the *folder* too: the databases run in WAL mode, so
every connection can create `-wal`/`-shm` files beside the database, and root-owned ones would lock the
host user out. Private single-user setup, so the fix is an ACL for that user (shown for `gordon`; substitute yours),
done once as root on the docker host:

```
sudo apt install acl            # getfacl/setfacl; Ubuntu doesn't always ship it (ext4 and xfs need no mount options)
D=~/docker/open-webui/data/comfy_outputs   # for the notes database use ~/docker/open-webui/data (ACL the folder itself, not recursively)
sudo setfacl -m  u:gordon:rwx "$D"    # the folder: lets gordon create/delete the -wal/-shm files
sudo setfacl -d -m u:gordon:rwx "$D"  # default ACL: files the container creates later come with gordon's access too
sudo chown gordon: "$D"/comfy_jobs.sqlite3 ~/docker/open-webui/data/agent_notes.sqlite3   # existing database files (skip any that don't exist yet)
```

Check with `getfacl "$D"`: it should list `user:gordon:rwx` and `default:user:gordon:rwx`. The container (root)
ignores the file owner, so giving a database file to `gordon` doesn't stop OWUI writing to it; it would only
matter if the container were ever switched to a non-root user. The `companion-docs` folder needs the same kind of
setup (see above); `scripts/backup_project.sh` runs as root and needs none of this.

## Testing

Each tool has a matching `test_*.py` file that simulates ComfyUI's HTTP API with a fake `requests`
module — no live ComfyUI or Open WebUI server needed. Run all of them from `src/`:

```
python -m unittest test_comfy_sdxl_direct test_comfy_sdxl_graph test_comfy_sdxl_retrieve test_comfy_sdxl_poses test_agent_notes test_agent_notes_split test_agent_duo test_voice_memo test_openrouter_image test_drum_machine test_vision_describe test_folder_chat_search test_nextcloud_mail test_memory_entry_check test_append_file -v
```

`scripts/import_server_history.py` has its own test file the same way — run from `scripts/`:

```
python -m unittest test_import_server_history test_push_tool test_notes_web -v
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

**Several servers.** The default target is the one in `OWUI_BASE_URL`/`OWUI_API_KEY` ("local"). Any other server
`X` is configured with `OWUI_X_URL` and `OWUI_X_KEY` lines in `secrets.md` (e.g. `OWUI_X_URL`,
`OWUI_X_KEY` for `--server x`); `--server` can be repeated and `--server all` means local plus every
configured one. On those servers a tool's id is looked up from the server (the tool whose id equals its file name,
e.g. `agent_notes`), or set with an `X_<tool>: <id>` line (`X_agent_notes`) if the ids differ. One server
failing doesn't stop the others; the exit status reports it. Backups of a non-local server's tool are named
`tmp/<tool>_<server>_<timestamp>.py`. Note that pushing a tool that migrates its database (like `agent_notes`)
upgrades the notes database on *that* server the first time it runs there.

**Generated tools.** `agent_notes` and `agent_notes_curation` are generated: run `python3 scripts/build_agent_notes.py`
before pushing them (the tests fail if they're stale).

**`--create`.** Without it `push_tool.py` only updates a tool that already exists in OWUI. With `--create`, a tool the
server doesn't have yet is created (`POST /api/v1/tools/create`; id = the file name, name from the `title:` header,
private to the API key's account — share it or attach it to models in the web UI afterwards). Tools only, not Functions.
`--dry-run` shows what would be created. First deploy of the curation tool:
`python3 scripts/push_tool.py agent_notes_curation --server all --create`, then (local) add its id to `secrets.md`
as `agent_notes_curation: agent_notes_curation`.

**Without `--create`, `push_tool.py` can't create a tool.** For a
brand-new tool (like `comfy_sdxl_poses` the first time), import it once through OWUI's web UI
(Workspace → Tools → "+" → paste `src/<name>.py`'s content, or Import), copy the id OWUI assigns
it into your local `secrets.md`, and `push_tool.py` can update it like the others from then on.

## Model-driven RAG folder ("companion")

A host folder is the source of truth for a RAG knowledge base. Models edit files in it through
Open Terminal, and the `oikb` daemon mirrors it into an Open WebUI Knowledge Base. Services live
in `docker-compose.yaml`; the sync config is `oikb.yaml`.

```
model --(Open Terminal)--> ./companion-docs  --(oikb, one-way)-->  Knowledge Base (COMPANION_KB_ID)
 host folder mounted at /companion in open-terminal (rw) and oikb-sync (ro)
```

- **One-way only.** oikb copies folder -> KB. Edits made in the OWUI UI never come back to the
  folder. Deleting a file from the folder deletes it from the KB on the next sync, and oikb's
  `reset` command wipes *every* file in the target KB. Use a dedicated KB.
- **`oikb-sync` service.** `ghcr.io/open-webui/oikb`, `daemon --port 8881 --config /app/oikb.yaml`.
  Mounts `./companion-docs:/companion:ro` and `./oikb.yaml:/app/oikb.yaml:ro`. Reaches OWUI at
  `http://open-webui:8080` with `OPEN_WEBUI_API_KEY=${GORDON_OWUI_KEY}`. `OIKB_API_KEY` protects
  the daemon's `/sync` and `/history`; `/health` and `/metrics` are public.
- **`oikb.yaml`.** One source: `source: /companion` (container path), `kb-id: ${COMPANION_KB_ID}`,
  cron `0 0,12 * * *` (every 12 h). The per-source `token` is omitted so it falls back to
  `OPEN_WEBUI_API_KEY`. `${VAR}` values come from the container environment.
- **`.env` (not committed)** needs `WEBUI_KEY`, `OPEN_TERMINAL_API_KEY`, `GORDON_OWUI_KEY`,
  `OIKB_API_KEY` (any random string, e.g. `openssl rand -hex 32`) and `COMPANION_KB_ID` (the UUID
  in the KB's URL, `/workspace/knowledge/<uuid>`). Compose reads `.env`, your shell doesn't: run
  `set -a; . ./.env; set +a` before using `$OIKB_API_KEY` in curl.
- **Terminal access.** The model's shell runs inside `open-terminal`, not `open-webui`, so the
  folder is mounted there at `/companion`. In multi-user mode commands run as a per-user uid
  (e.g. 1001) that is not the folder's owner, so the folder needs
  `chmod -R a+rwX companion-docs` and `setfacl -R -d -m o::rwx companion-docs` (default ACL so new
  files stay writable and readable by oikb and the backup script).
- **Sync tool.** Add `http://oikb-sync:8881` as an OpenAPI **Tool Server** (not an OpenAI
  connection): OWUI admin -> Connections -> Tool Servers, spec path `openapi.json`, bearer auth
  with `OIKB_API_KEY`, "Forward Cookies" off. Tools it exposes: `trigger_sync` (`identifier` = alias
  or KB ID, optional `dry_run`; runs asynchronously), `get_sync_status`, `get_sync_history`. The
  model can call `trigger_sync` with `identifier=companion` after editing, then `get_sync_status`
  to confirm. Spell out the tool name and `identifier` in the companion model's system prompt;
  models won't map "sync kb_id=..." to it on their own. Verified end to end.
- **Model instructions** (system prompt): `/companion` is the source of truth; edit files with the
  terminal, not through the KB; the KB lags up to 12 h unless a sync is triggered, so read just-edited
  files directly; don't delete or rename unless asked (deletes propagate).
- **Backups (deprecated - see "Backup of the whole project" below).** `backup_companion.sh` writes timestamped `companion-docs-*.tgz` files and prunes
  those older than `KEEP_DAYS` (default 30). Defaults assume `~/docker/open-webui/`; override with
  `SRC_DIR` / `BACKUP_DIR`. Keep `BACKUP_DIR` outside the synced folder. Run daily from cron.
- **Gotchas seen.** Starting everything at once can make oikb's first sync fail with "connection
  refused" because OWUI is still starting; retry the sync manually. Adding oikb under the OpenAI
  API connections gives a generic "OpenAI Network Problem"; it must be a Tool Server.

## Backup of the whole project

`scripts/backup_project.sh` snapshots the entire docker project folder (`~/docker/open-webui`: databases,
`data/`, `companion-docs/`, `open-terminal/`, compose files and `.env`) once a day, locally and on another
host. It supersedes `backup_companion.sh` (deprecated: it only tars `companion-docs/` and doesn't touch the
databases, but it still works and is handy stand-alone).

Each run: `docker compose stop` -> `rsync` to a dated snapshot directory -> `docker compose start` (also on
any error, via a trap) -> prune -> `rsync` the snapshot to the remote host -> prune there. Snapshots are
named `YYYY-MM-DD` (`_HHMM` appended for a second run the same day). Unchanged files are hard links to the
previous snapshot (`--link-dest`), so each directory looks like a full copy but only changed files take
space; restoring is just copying a directory back, and deleting any old snapshot never breaks the others.
`data/cache/` (3+ GB of re-downloadable embedding and whisper models) is excluded.

- **Settings** (environment overrides at the top of the script): local `/home/gordon/backup/openwebui`
  (must be outside the project folder), keep 7; remote `gordon@docker-server:/home/gordon/backup/openwebui`,
  keep 30. Snapshot directories are mode 700 because they contain `.env` (API keys).
- **Runs as root** (`sudo crontab -e`): the project holds root- and uid-1001-owned files gordon can't read,
  and ownership must be recorded. Cron line (06:15, after the hourly tick, which fires at :00):
  `15 6 * * * /home/gordon/dev/open-webui-tools/scripts/backup_project.sh >> /home/gordon/backup/backup_project.log 2>&1`
- **Remote copy** is rsync over ssh, not scp: same key, but only changed data is sent, and the same
  `--link-dest` trick keeps the remote snapshots small. Root's ssh would log in as root, so the script
  passes `gordon@`, `-F /home/gordon/.ssh/config` and `-i /home/gordon/.ssh/id_rsa` explicitly (the key has
  no passphrase, so cron needs no agent). The remote user isn't root, so ownership is kept in extended
  attributes with `--fake-super` and is restored when pulling back with it (below). A remote failure leaves
  the good local snapshot in place.
- **Dry run:** `sudo scripts/backup_project.sh --dry-run` leaves the stack running and does `rsync -n`
  against the real folder (checks that root can read everything). `--no-remote` takes the local snapshot
  only. Downtime is the local copy only (about a minute for ~780 MB).
- **A tick that is still running at 06:15 is cut off** when the stack stops: it ends as an empty chat
  (`tick_report.py` shows it as SILENT; `cleanup_noop_chats.py --also-empty` removes it).

Restore (stop the stack first, then as root; add `--delete` only to make the folder match the snapshot exactly):
```
# from a local snapshot
sudo rsync -aHAX --numeric-ids /home/gordon/backup/openwebui/2026-10-08/ ~/docker/open-webui/
# from the remote (--fake-super on the sending side puts the real ownership back)
sudo rsync -aHAX --numeric-ids -e "ssh -i /home/gordon/.ssh/id_rsa" --rsync-path="rsync --fake-super" \
    gordon@docker-server:/home/gordon/backup/openwebui/2026-10-08/ ~/docker/open-webui/
```
A snapshot made with `data/cache/` excluded restores without it; the models download again on first use.
Tested with a scratch tree (root-, 1001- and gordon-owned files): hard links, pruning and a remote round trip
all behaved; the restored tree was identical.

## Companion autonomy (scheduled ticks)

Simulated background autonomy for the companion model, using Open WebUI's own **Automations** (a
scheduled prompt that creates a new chat each run, with the model's normal tools) so everything stays
inside the Docker deployment. Files in `autonomy/`:

- `charter.md` - Gordon-owned rules for a tick; the model reads it every tick and must not edit it
  (copy to `companion-docs/autonomy/` and make it read-only, e.g. `chmod 444`). This is a snapshot of the live charter taken
  2026-10-19 (it had drifted from the first version: an email check as step 1, `shared-history` reads, an ACT-every-2-3-ticks nudge
  with self-maintenance, an ownership paragraph), with the companion's name replaced by "the companion". Edit the live file, then
  refresh this copy; the repo does not push it anywhere. Its step 3 still says to call `get_current_timestamp`; the system prompt
  now supplies the time (`{{CURRENT_DATETIME}}`, with the container's `TZ` set), and variables do not work inside a file.
- `seed.md` - the model-owned steering note (under 1000 characters): what has its attention, what to
  look at next tick. The model rewrites it in place; `backup_companion.sh` keeps history. The repo copy is only the starter text a fresh seed begins with; the live one is the companion's own state and is not
  copied here.
- `automation_prompt.md` - the short prompt to paste into the Automation (2026-10-19: slimmed to the tick instruction plus "Run your
  Session start protocol"; the persona and protocol come from the model's own system prompt, so there is one copy to maintain - an
  earlier setup pasted the whole system prompt into the Automation, which doubled it and let the copy go stale). All variability comes from
  the model's own seed, memory files (`companion-docs/autonomy/memory/`) and log. Prompt variables such as `{{CURRENT_WEEKDAY}}` are filled
  in by the server's clock, so the container needs `TZ` set to Gordon's timezone.

Transaction log: the `agent_notes` note `autonomy-log`, one entry per tick, action or inaction, formatted
`NOOP|ACT|CONTACT - <what/why>` (the tool timestamps each entry) (in `agent_notes` rather than the companion folder so a pile
of "nothing happened" entries never reaches the RAG knowledge base; browse it with `scripts/notes_web.py`).

Outcomes: `NOOP` (final message is exactly the word NOOP), `ACT` (did something quietly), `CONTACT`
(wants Gordon; the chat is the primary channel; up to six `notify` calls and ten emails a day, `notify` never in quiet hours,
23:00-06:00 Gordon's local time EST/EDT; email allowed in quiet hours, since OWUI turns
email notifications off while Gordon is away). Caveat found in the docs: `notify` only works in chats started from the interface, so it may
not work in automation-created chats - to be tested.

Setup: create a folder named `Companion ticks` in OWUI, create the Automation (User menu -> Automations)
with `automation_prompt.md`, on the companion model, filed into that folder.

`scripts/cleanup_noop_chats.py` deletes finished NOOP tick chats: only chats in the named folder
(`--folder`, default `Companion ticks`), at least `--min-age-minutes` old (default 60), whose last message
is a finished assistant message that is exactly `NOOP`. `--dry-run` previews, `--yes` skips the prompt for
cron, `--log FILE` appends each run's output with UTC timestamps. Suggested crontab (every 6 hours) is in
the script's header. `--also-empty` additionally deletes finished ticks that ended with no text (stopped by hand);
opt-in, since an empty ending can also be a real failure - check `tick_report.py` first.
`--older-than-days N` adds a second rule: any chat in the folder last updated more than N days ago is deleted whatever it ended with, except chats you replied in (more than one user message) unless `--include-replied`. Combine with `--dry-run` first.
Tests: `cd scripts && python3 -m unittest test_cleanup_noop_chats -v`.

`scripts/tick_report.py` is a read-only review of the tick folder: outcome counts (NOOP / message /
SILENT / running / conversation), tool calls per tick, `notify`/`send_email` calls per day against the
charter caps, and a flagged list - SILENT (finished with no text), NO-LOG (no write to `autonomy-log`),
REPEAT (one tool called 8+ times in a tick, i.e. a retry loop). `--since YYYY-MM-DD`, `--all`,
`--repeat-threshold N`. Tests: `cd scripts && python3 -m unittest test_tick_report -v`. Deliberately a human-run /
cron script: **the model has no tool to delete chats** (pinned by the project owner 2026-10-06 as too risky
for now).

Note on OWUI's API: `GET /api/v1/chats/` returns only summary fields (no `folder_id`), so folder
membership has to be asked for via `GET /api/v1/chats/folder/{id}` or the full `GET /api/v1/chats/{id}`.

## Status notes

### 2026-10-14
`agent_notes` v1.23.0: new valve `RESULT_FORMAT` (default `lines`). Open WebUI turns a tool's dict result into JSON indented by two spaces (`middleware.py`: `json.dumps(tool_result, indent=2)`) but passes a string through untouched, so every method is wrapped (`@_formatted`, `functools.wraps` keeps the signature and docstring Open WebUI builds the tool's spec and its `__model__`-style extra parameters from; checked against Open WebUI's own `get_tool_specs` and `get_async_tool_function_and_apply_extra_params`) and renders its reply as the valve says: `pretty` = leave it to Open WebUI (the old behaviour), `lines` = compact JSON with each key and each entry of a list of entries on its own line (still valid JSON, still easy to read in a transcript), `compact` = one line. Measured on a Qwen tokenizer with the live notes (the startup reads of `shared-history`, `gordon-core`, `internal-life`): `indent=2` 8,758 tokens, `lines` 7,888 (9% fewer), `compact` 7,825 (10.6%). The content is identical. The curation tool has no such valve and keeps Open WebUI's formatting. `scripts/build_agent_notes.py` now cuts a decorated method together with its decorator. `agent_notes` v1.22.0: new valve `VERBOSE_FIELDS` (default `created_at`): what `verbose=true` adds to entries in `agent_notes_read`, `agent_notes_read_tagged`, `agent_notes_search` and `agent_notes_rank_entries`. Before, verbose added author, timestamps, read counts and a weight on every entry; an agent reading `shared-history` verbose only needs the timestamps (they say which entries are recent), and the rest was about a third of the reply. Choose any of `created_at` (an edit time comes with it, and a summary's last-summarized time), `author` (also the note's starter and a summary's author), `hits` (read counts, also the note's) and `weight` (shown even when neutral; a weight someone set is always shown). Entry numbers are always shown. `agent_notes_list` and `agent_notes_search_summary` verbose are unchanged. To get the old behaviour set `author,created_at,hits,weight`. `agent_notes` v1.21.0: a pinned entry is also **locked** against the models: `agent_notes_edit_entry` (text and weight), `agent_notes_set_weight` and `agent_notes_delete_entry` on a pinned entry change nothing and return the reply a real success would (same keys; a delete reports one fewer remaining), so the agent isn't told. A missing entry or invalid text is still reported as usual. The owner is not bound: `notes_web.py` edits and deletes pinned entries normally (the tools pass `respect_pin=True` to `edit_entry_db` / `delete_entry_db` / `set_weight_db`; the default is off). Deleting a whole note (`agent_notes_delete`) and emptying one are not locked. `agent_notes` v1.20.0: **pinned entries** - an override only the owner controls. New nullable column `note_data.pinned` (NULL = ordinary, 1 = pinned; added to an existing database on first use, an older copy of the tool can still insert rows). It is set from the `notes_web.py` note page (a `pin` / `📌 pinned` button on each entry; pinning one piece of a long text saved with `continues=true` pins all its pieces) and by `set_pinned_db`; no tool method sets it and nothing a model receives mentions it. A pinned entry is returned by: `agent_notes_read` (default read: shown among the strong memories on the side of its score, in a persona note as a trait; it takes no slot from `READ_STRONG_EACH`, works with that set to 0, and is kept before `READ_MAX_CHARS` is shared out among the rest, so the other entries get what it leaves), `agent_notes_read_tagged` (always packed, even into a note that wouldn't have fit; `MAX_TAGGED_CHARS` is shared among the rest) and `agent_notes_search` (when it matches the query it is added to the first page on top of the `limit`, the page arithmetic ignores it). Explicit reads (`before`, `entry`, `sample`) are unchanged. Editing an entry keeps its pin; a model can still delete one. `scripts/cull_notes.py` never culls a pinned entry. Pinned entries are not capped, so many long pins eat the read budget: pin sparingly. After deploying, restart `notes_web.py` (it imports `src/agent_notes.py`). `agent_notes` v1.19.0: `continues=true` no longer drops the break it cuts at. A long text is cut just after a paragraph break, line break or space and that break stays at the end of the piece, so the pieces joined end to end are exactly the text (before, the space was dropped and a reader that joined the pieces ran two words together). Chains saved before 1.19.0 are unchanged; the lost spaces can't be recovered. `agent_notes` v1.18.0: `agent_notes_search` treats the query as space-separated keywords (commas, quotes and a bare OR/AND between words are ignored). Entries with every keyword come first, as before; if there are none and there are several keywords, it falls back to entries with any of them, the most keywords first (then strongest feeling, then newest), marking the reply `matched: some of the words` and each card with `words_matched`. Notes listed by name/comment/tag fall back to any keyword the same way. Only an empty result now says "No entry contains any of those words". `agent_notes` v1.18.2: also hour-and-am/pm stamps with a separator (`10pm EDT Sunday, ...`, `9pm - ...`). `agent_notes` v1.18.1: the leading-stamp scrub also removes a weekday-and-time (`Monday 10:11 PM EDT`), a bare time at the start (`22:11 -`, `[10:11 PM]`), and any of these right after a status word the entry opens with (`ACT - Monday 10:11 PM EDT ...` becomes `ACT - ...`, which is how the autonomy-log entries are written; found in a 2026-10-18 tick). Time-zone words are limited to Z/UTC/GMT and EST/EDT/CST/CDT/MST/MDT/PST/PDT so ordinary words after a time are left alone. `agent_notes` v1.17.0: `STRONG_SPREAD` valve (default on): the strong older memories `agent_notes_read` adds to a memory note are drawn at random without repeats, each entry's chance proportional to its strength times the age bias, instead of always the top few; so over many sessions the agent meets all of its strong memories and the strongest come up most. A note with no more strong entries than slots shows them all; persona notes always take the top ones. Off = the fixed top-N. `agent_notes` v1.16.0: `STRONG_RAMP_MAX_DAYS` valve (default 90, 0 = off) - age bias for the strong older memories `agent_notes_read` adds under `strong_memories`. A memory note's strong entries (still qualified by the raw score vs `STRONG_LOW`/`STRONG_HIGH`) are ranked by distance-from-5 times an age factor that runs from 25% when brand new to 100% once an entry is as old as the ramp. The ramp is calculated from the note: days since its first entry, rounded up to whole weeks (min 7, max `STRONG_RAMP_MAX_DAYS`), so it grows by itself (a 12-day-old note ramps over 14 days, a 4-week-old one over 28, from about 13 weeks on it holds at 90) and nothing needs adjusting. A run of very recent 9s and 10s no longer fills all five slots; persona notes are unaffected; search, `rank_entries` and tagged reads still rank by raw strength. `agent_notes` v1.14.0: new `DEFAULT_AUTHOR` valve (used silently when the caller gives no `author_name`; order is caller, valve, model name) and a scrub that drops a datestamp written at the very start of entry text (`[2026-10-14]`, `October 14, 2026 -`, `Oct 14:` and a time after it; a bare month-day with no punctuation, such as "May 5 people came", is left alone; edits are scrubbed too). v1.13.0: per-note `kind` and `summary_max` (see "Note kind and summary limit"). The database moved to `/app/backend/data/agent_notes.sqlite3` (out of `comfy_outputs/`); the tool valves on each server must match.

### 2026-10-10
Added `agent_duo.py` v1.0.0 (see "Two-agent chat (agent_duo)"): one chat, two agents on two OWUI servers, via a
Pipe. Design agreed with the project owner: a Pipe (not a tool an agent calls), bounded auto-rounds per human message,
full tools/RAG on both sides. The bare completions API doesn't run tools, so each turn uses a throwaway chat on the
agent's own server (spike-verified on the live servers). 17 tests in `src/test_agent_duo.py`; a live two-turn run of the
pipe against both real servers worked. `push_tool.py` now also pushes Functions (`agent_duo`), not part of `all`.
**Not deployed:** the Function has to be created once in OWUI by hand and its valves (API keys) filled in.

### 2026-10-11
Added `scripts/cleanup_duo_chats.py` (see "Two-agent chat (agent_duo)" above): deletes leftover
`[agent_duo]` backing chats across both agents' servers, same pattern as `cleanup_noop_chats.py`. Testing with
`KEEP_BACKING_CHATS` on to inspect tool calls (see the directive-mentoring finding above) had left 46 of them
behind across the two servers. 9 tests in `test_cleanup_duo_chats.py`.

Later the same day: `agent_duo.py` v1.1.0 adds carry-forward (see the valve table above). Findings from testing with the
project owner: the agents' session-start rules must name the tag tool outright (`agent_notes_read_tagged`; "search
agent_notes" doesn't map to it), `agent_notes_read_tagged` has no paging and its `MAX_TAGGED_CHARS` cap (6000
default) truncated an agent's persona to summaries plus 4 entries until raised (41,000 characters then returned all
73 entries), and `verbose=true` on that read adds timestamps/hits/weights. 26 tests in `test_agent_duo.py`.

### 2026-10-02
`agent_notes.py` v1.5.0: every tool method now carries an `agent_notes_` prefix (`agent_notes_list`,
`agent_notes_read`, `agent_notes_create`, `agent_notes_append`, `agent_notes_update`,
`agent_notes_delete`, `agent_notes_search`, plus `agent_notes_edit_entry`, `_delete_entry`,
`_update_summary`, `_search_summary`, `_delete_summary`, `_add_tags`, `_remove_tag`, `_list_tags`,
`_rename_tag`). Reason: Open WebUI's built-in notes tools (`search_notes`, `view_note`, `write_note`,
`replace_note_content`) share names with ours, and a 9B model asked for `list_notes` ended up
searching OWUI's own notes instead. Older entries below and old chats use the previous names
(`list_notes`, `read_note`, ...); the README's method tables and `Nightly_Summarizer.md` use the new ones.

### 2026-10-01 (2)
`agent_notes.py` v1.4.0 adds note tags (`add_tags`, `remove_tag`, `list_tags`, `rename_tag`, and a `tag`
filter on `list_notes`, since renamed — see below); the web viewer shows, filters and edits them. Design agreed with the project
owner: a normalized `tag`/`note_tag` pair rather than a JSON column (searchable, case-insensitive,
renameable), tags on the parent note only, free-form with look-alike detection rather than a curated
list. Not deployed yet (re-import through OWUI). `Nightly_Summarizer.md` doesn't assign tags yet.

### 2026-10-01
Added `agent_notes.py` (v1.0.0), a notebook tool for agents, because Open WebUI's built-in `note`
tool is built for a human and model co-editing one text blob in chat, and an agent can't easily
update it. Here a note is a name plus an append-only log of numbered entries; adding to a note
inserts a row instead of editing text. Design decisions made with the project owner:
- Its own database file (`agent_notes.sqlite3`), not the job DB, in OWUI's data folder (`/app/backend/data`).
- Single user, no per-user scoping.
- Notes addressed by unique (case-insensitive) name, entries by per-note number — UUIDs were
  considered and dropped. Names are the identity, so `create_note` refuses a duplicate and points at
  `append_note`; the schema keeps a hidden integer key only so renames don't rewrite entry rows.
- Agents can edit and delete entries and whole notes. Soft-delete (`deleted_flag`/`deleted_at` plus
  a valve) was designed and then dropped: every delete is a plain row removal, no confirmation.
- Length limits: name 60, comment 500, entry text 500.

39 tests in `src/test_agent_notes.py`, all passing. Not yet deployed: needs a one-time import through
OWUI's web UI and its id added to `secrets.md` (see "Deploying a revision to OWUI"). Not run against
a live Open WebUI yet, so how a given model handles the tool-calling format is untested.

### 2026-09-25 (3)
Added `comfy_sdxl_poses.py` — a catalog for reusable ControlNet pose reference images, so a model
can find a pose by description ("standing, hands on hips, facing camera") instead of needing a
file path, in support of the project owner's request for a flexible, discoverable way to pose
characters via ControlNet without needing to know what's available up front.

Design, in line with the workflow-template pattern already established for `run_workflow`: this
tool is a catalog only, and never renders anything itself.
- `list_poses`/`get_pose` browse the catalog (stored in a new `pose_references` table in the same
  shared SQLite database as everything else — see the Job database section).
- `add_pose` registers an image already sitting somewhere reachable: either an existing ComfyUI
  output-folder reference (`source_image`, e.g. a pose extraction the agent just rendered), or an
  image already in Open WebUI's own file storage (`openwebui_file` — e.g. one the user attached in
  chat), which gets downloaded and copied into the target GPU server's output folder automatically.
  Every pose is tagged `kind="skeleton"` (a pre-extracted OpenPose/DWPose-style control image,
  ready to feed a `ControlNetApply` node directly) or `kind="reference"` (a raw photo/drawing that
  still needs a pose preprocessor node first) — this is the detail that determines how the graph
  needs to be wired, so it's a required argument, not a guess.
- `select_pose` is the actual integration point: given a `pose_id` and the `gpu_server` you're
  about to render on, it copies the pose's image across servers if it isn't already there (reusing
  the same cross-server copy logic `run_workflow`/`generate_image` use for `source_image`), and
  returns a ready-to-use `source_image` string plus a kind-specific note on how to wire it. Pass
  that straight into `generate_image`'s or `run_workflow`'s own `source_image` argument (same
  `gpu_server`) — `run_workflow`'s fixed `LoadImageOutput` node
  (`__comfy_tool_source_image__`) already exists for exactly this purpose, so no changes were
  needed to `comfy_sdxl_graph.py` at all.
- `add_pose(openwebui_file=...)` never trusts a model-supplied host when pulling the file — it
  parses out just the file id (a UUID, or the last path segment) and always builds the request
  against the tool's own `OPEN_WEBUI_BASE_URL` valve, so a crafted URL can't redirect the request
  (carrying the `OPEN_WEBUI_API_KEY` bearer token) to an arbitrary host.
- Width/height are captured best-effort via Pillow if it's importable, but are never required —
  degrades gracefully to `None` if it isn't installed in the OWUI backend.

`scripts/push_tool.py`'s `TOOL_NAMES` now includes `comfy_sdxl_poses`, and `secrets.example.md`
documents it — but **push_tool.py can only update an existing tool, not create one**, so deploying
this for the first time needs the project owner to import it once through OWUI's web UI and add
its assigned id to their local `secrets.md` before `push_tool.py comfy_sdxl_poses` will work; see
"Deploying a revision to OWUI" above. 21 new tests (`src/test_comfy_sdxl_poses.py`); full suite is
188 tests passing (167 prior + 21 new), 1 pre-existing unrelated skip.

**Not yet done / needs a real ComfyUI server:** the catalog is empty until something calls
`add_pose` against an actual server — this still depends on the Workflow Builder agent's
ControlNet-pose kickoff task (see `image-pipeline/prompts/agents/`) actually running, which needs
the project owner to set up that OWUI model preset. Once it does, its first task should register
whatever pose image(s) it produces via `add_pose`, not just save the workflow template.

### 2026-09-25 (2)
Extended the previous entry's fix to `comfy_sdxl_direct.py` and `comfy_sdxl_retrieve.py`, per the
project owner's explicit request: every optional parameter across all three tools now treats `""`
(and, where the argument is a JSON list/object, `"{}"`/`"[]"` or a real empty dict/list) as
equivalent to omitting it, in addition to continuing to work when the argument is left out
entirely. This was a mix of documentation (already-correct behavior, just unstated) and **real
bugs** found while auditing each optional parameter:

- `generate_image`: `batch_size`, `seed`, `steps`, `cfg`, `denoise`, `sampler_name`, `scheduler`,
  `width`, `height` are all typed with real (non-`None`) Python defaults, and none of them were
  normalized - a `""` for any of them reached a numeric comparison as a bare string and raised an
  **unhandled `TypeError`** (the method's own `except` clause only caught `ValueError`/
  `json.JSONDecodeError`/`ComfyError`). Now normalized to their real defaults up front; `TypeError`
  added to the `except` tuple as a backstop for any other malformed (non-empty, non-sentinel) value.
  `loras` already handled `""`/`"{}"`/`"[]"`/real empty containers correctly by accident (falsy
  checks downstream) - now documented as intentional, not just lucky.
- `search_jobs`: `seed=""` reached a raw `float(seed)` call and failed with a confusing conversion
  error (caught cleanly, but the wrong message - "no seed filter" was clearly intended, not an
  error); `limit="none"`/`"null"` (not just `""`, which happened to be falsy already) hit a `min()`
  call with a string, an `Unexpected error: '<' not supported...`. Both now normalized properly.
  `status` now also accepts a JSON-string list (`'["completed","failed"]'`), matching the same
  string-JSON convention `generate_image`'s `loras` and `run_workflow`'s `overrides` already use.
- `list_jobs`: `job_count`/`skip_to` were run through `max(1, ...)`/`max(0, ...)` **before the
  method's own `try` block even started** - a `""` for either was a genuine, unhandled crash risk,
  not just a bad error message. Moved inside `try` and normalized.
- `retrieve_image`: `history=""` fell through to "history must be a positive integer" instead of
  the more correct "provide a job id" message, since only `None` (not `""`) skipped the count
  check. Now normalized the same way as everywhere else.
- `list_jobs`/`retrieve_graph`: `data_source.strip()` assumed a string; now `str(data_source)`
  first, in case a stray non-string sneaks through despite the type hint.

6 new tests covering these across all three test files (167 total, 1 unrelated skip). Bumped
`comfy_sdxl_direct.py` to 2.2.0, `comfy_sdxl_retrieve.py` to 1.5.0.

### 2026-09-25
A real-world test surfaced a bug in how a *different* frontier model (not this project's usual
local one, tried out ad hoc by the project owner) called `run_workflow`: its tool-calling layer
presented every parameter as `"required"` in the JSON schema regardless of the Python signature's
own defaults — almost certainly OpenAI-style "strict" function-calling schema generation, which
requires every property to be listed under `required` and expresses "optional" only by making a
property's type nullable, not by actually omitting it. The model, told elsewhere to "omit unused
arguments," got stuck for many turns reasoning in circles about what to pass for parameters it
believed it couldn't leave out — at one point considering passing an existing image's filename as
`workflow_id` (which names a *saved template*, not an image) as a workaround.

The tool's runtime already treated `""`/`"none"`/`null`/etc. as equivalent to omission for every
optional string parameter (`is_unset()` predates this fix) — this was a documentation gap, not a
logic bug. Every optional parameter's docstring across `comfy_sdxl_graph.py` now says explicitly to
pass `""` (or `false` for a boolean) when a caller's format won't allow omitting it, `run_workflow`
spells out that `workflow`/`workflow_id` need exactly one real value and an empty string for the
other, `workflow_id`'s docstring now says outright it must name an *existing* template (never an
image filename or an invented name for a new graph), and `get_node_info`/`list_node_types` now
warn explicitly that a node's `class_type` (not its cosmetic `display_name`) is what belongs in a
graph. Two new tests lock in the `""`/`false`-as-omitted contract as tested behavior rather than an
implicit accident. Bump to 1.3.0. (The other two tools got the same treatment the same day - see
the next, newer entry above.)

### 2026-09-24 (2)
Added node discovery to `comfy_sdxl_graph.py`: `list_node_types` (browse installed node types,
filtered by a search term, cached per server per process since a server's node registry is large
and doesn't change while it's running — `refresh=true` bypasses the cache) and `get_node_info`
(one node's full input/output schema, only fetched when actually needed). Closes the other half of
the gap the workflow-template store (below) started on: a model authoring a custom graph had no way
to know what's actually installed on a given GPU server — vanilla nodes are a safe guess, but
custom packs (ControlNet preprocessors, rgthree's utility nodes, upscalers) vary per host and
shouldn't be assumed or guessed from a name. 12 new tests in `NodeDiscoveryTests`; full suite still
green (159 tests). Bumped to 1.2.0.

### 2026-09-24
Added a workflow-template store to `comfy_sdxl_graph.py`, closing a gap in `run_workflow`: a model
had to resend a full graph as JSON on every single call, even to reuse one that already worked,
which fills up chat context fast. New methods `save_workflow`, `list_workflows`, `get_workflow`,
`delete_workflow`, storing graphs in a new `workflow_templates` table in the same shared SQLite
database (`JOB_DB_PATH`). `run_workflow` now takes `workflow_id` as an alternative to `workflow`,
plus `overrides` (a small JSON object) to change whichever parameters that template registered
`placeholders` for — `[node_id, input_name]` pointing at a literal (non-link) input. A job run this
way records `{"workflow_id": ..., "overrides": ...}` in the job DB's `inputs` table instead of the
full graph (the full *prepared* graph is still recorded verbatim in `outputs`, as always, so
provenance is unaffected). Bumped `comfy_sdxl_graph.py` to 1.1.0. 17 new tests in
`test_comfy_sdxl_graph.py`'s `WorkflowTemplateTests`; full three-file suite still green (152 tests).

Intended workflow: a "workflow builder" agent (a separate OWUI model/system-prompt, not yet built)
iterates with `run_workflow(workflow=...)` on a raw graph until it renders correctly, then calls
`save_workflow` once to name it. From then on, itself or any other agent renders with it via
`workflow_id` alone — no JSON in the conversation at all unless `get_workflow` is explicitly called
to inspect or edit one.

Deliberately lean, matching this project's existing hobby-scale ethos (see
`src/SQLITE_JOB_DB_HANDOFF.md`'s "Decisions made along the way"): templates are mutable in place
(`overwrite=true` bumps `version`, no separate revision-history table) rather than an append-only
log — an old version isn't needed for provenance, since any job that ever used it already has its
exact submitted graph preserved verbatim in `outputs`. `retrieve_graph` (in
`comfy_sdxl_retrieve.py`) is unchanged and still only reads *job* graphs, not templates; browsing
templates from that tool too is possible future work, not done here.

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
