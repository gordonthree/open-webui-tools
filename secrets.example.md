# Secrets template

Copy this file to `secrets.md` (already in `.gitignore` — it will never be committed) and fill
in your real values. Used by `scripts/push_tool.py` to push local tool source into a running
Open WebUI (OWUI) instance without going through the web UI's manual import each time.

Format is simple `KEY: value` lines — one per line, comments start with `#`.

## OWUI connection

OWUI_BASE_URL: https://your-owui-host.example.com
OWUI_API_KEY: paste-your-personal-api-key-here

Generate the API key in OWUI: Settings -> Account -> API Keys. The account needs to own (or be
admin over) each tool below, since updating tool content requires write access.

## Tool IDs

Each tool's id in your OWUI instance. Find it in the URL when editing the tool under
Workspace -> Tools (look for `?id=<tool-id>`), or via `GET /api/v1/tools/`.

comfy_sdxl_direct: <tool-id>
comfy_sdxl_graph: <tool-id>
comfy_sdxl_retrieve: <tool-id>
comfy_sdxl_poses: <tool-id>

Note: push_tool.py only UPDATES a tool that already exists in OWUI - it can't create one. For a
brand-new tool (e.g. comfy_sdxl_poses the first time), import it once through OWUI's web UI
(Workspace -> Tools -> "+" -> paste src/<name>.py's content, or Import), then copy the id it's
assigned here. After that, push_tool.py can update it like the others.
