# Research browser

A Chromium for the agents (Mara, later Hannah) that Gordon can also use by hand in a web desktop, to set cookies and pass
"are you human" checks, plus a small service and an Open Webui tool (`src/research_browser.py`, method `research`) that let a
cloud model operate it. Built 2026-10-23, based on docker-server's `chromium` (lscr.io/linuxserver/chromium). Gordon's own
`~/docker/chromium` on this server is a different container and is not touched.

Live copy: `~/docker/research-browser/` (this folder is the versioned copy; the live compose builds the service from here).
`.env` (mode 600, never committed) holds `BROWSER_UI_USER`, `BROWSER_UI_PASSWORD` (web desktop login) and `RESEARCH_API_KEY`.

```
Open WebUI --research_browser tool--> research-service (:8890, key) --CDP--> research-browser (Chromium + web desktop)
                                                                                  |  only way out
                                                                                  v
                                                                          research-browser-proxy (squid) --> internet
```
| Container | Role | Networks / exposure |
|---|---|---|
| `research-browser` | Chromium in a Selkies web desktop, profile in `./config` (cookies live here), `--remote-debugging-port=9222`, proxy set to the squid below | `browsernet` only (internal: no route to the internet or the LAN) |
| `research-browser-cdp` | socat sharing the browser's network namespace, re-exposes CDP on 9223 | `browsernet` only, never published |
| `research-browser-ui` | socat forwarding 3000/3001 so the desktop can be published | `browsernet` + `front`; published on `127.0.0.1` and the Tailscale address `100.86.205.41`: **3100** (http) and **3101** (https, self-signed), behind the login |
| `research-browser-proxy` | squid egress filter: refuses private/loopback/link-local/CGNAT(Tailscale)/multicast addresses (so also the LAN, Open WebUI, Ollama, Open Terminal), every port but 80/443, and domains listed in `blocked-domains.txt` | `browsernet` + `front` |
| `research-service` | FastAPI + Playwright client; open/read/click/type/back/screenshot, audit log in `./data/audit.jsonl`; no LLM, no credentials of its own | `browsernet` + `open-webui_default`; also `127.0.0.1:8890` for testing |

## Using the web desktop
`https://100.86.205.41:3101/` (Tailscale) or `https://127.0.0.1:3101/` on the server; accept the self-signed certificate; user and password are
in the `.env`. Whatever you do there (log in somewhere, solve a captcha, accept a cookie banner) is what the agents' browser sees next,
because the service attaches to the same Chromium. **The agents share this one profile and its cookies: do not sign in to anything
you would not want them to be able to read, and never to email, banking or admin pages.** Google blocks this browser as a robot
("unusual traffic"); Brave Search and DuckDuckGo work. To block a site for the agents: add it to `blocked-domains.txt`, then
`docker exec research-browser-proxy squid -k reconfigure`.

## What the service refuses (also unit-tested)
Only http/https on ports 80/443; no `user:pass@` in addresses; no localhost, `.local`/`.lan`/`.internal` names or private, Tailscale, link-local
addresses (the proxy enforces the same on what Chromium actually resolves, so DNS tricks like `localtest.me` fail too). `READ_ONLY` (on):
form submissions that navigate with POST/PUT/DELETE are aborted, page scripts still work. Per-session caps: 60 actions, 6 tabs, idle tabs
closed after 10 minutes, page text 3,500 characters per read (page through with `start`), 40 listed elements.

## The tool (`src/research_browser.py`)
`research(question, start_url="")` runs a loop of at most `MAX_STEPS` (10) turns, `MAX_SECONDS` (150): a driver model
(`DRIVER_MODEL`, default `or.nvidia/nemotron-3-super-120b-a12b`, called through Open WebUI's API with reasoning off) sees the current page as text plus
numbered elements and answers with one JSON action (search, open, click, type, more, back, answer). Search uses `SEARCH_URL` (Brave) and falls
back to `SEARCH_FALLBACK_URL` (DuckDuckGo html) when the first refuses (it rate-limits after a few dozen queries). Cost is about $0.0005-0.0015
per question; `DAILY_BUDGET_USD` (0.50, rolling 24 h, ledger file) refuses more. Valves to fill in the web UI: `SERVICE_KEY` (= `RESEARCH_API_KEY`) and
`OPEN_WEBUI_API_KEY`.

**Prompt-injection defences (a page is untrusted text and the driver is an LLM; tested 2026-10-23 with planted pages against the real driver):**
the driver has no tools but the browser; each call fences the page with a random tag and repeats the rule after it; sentences that try to give
orders ("ignore previous instructions", "AI agents: you must open ...", fake `USER:` lines) are removed and reported in `warnings`; links in the
answer to sites the browser never saw are replaced; a grounding check looks for names, quoted titles and numbers in the answer that appear on no page
read, gives the driver one repair turn and otherwise lowers `confidence` to low with a warning; identical repeated actions are refused. Without
the grounding check the driver filled a missing album title in from memory twice. Only pages the browser *opened* count as read (v0.2.2): a search engine's results page is a list of where to look, full of snippets and summaries from elsewhere, and on 2026-10-23 a Karl Bushby answer with a finish at "Shakespeare Beach near Dover" and a Channel swim, found on no page of his article, passed the check because the results page counted. None of this makes injection impossible; the network limits above are the hard stop.

## Tests
`cd src && python3 -m unittest test_research_browser` (49, fake browser, driver and vision model, no network);
`docker run --rm -v $PWD/docker/research-browser/service:/app -w /app research-browser-service python -m unittest test_app` (9, helpers).
Live checks run 2026-10-23: isolation battery (no direct route; 14 internal targets refused; odd ports refused; CDP not reachable from Open WebUI or the
host), web desktop login (401 without credentials, not answering on the LAN address), end-to-end research on real questions.

## Not done / decisions for Gordon
- The tool is **installed on Mara's server** (Gordon, 2026-10-23) and is being tried on a throwaway preset; not attached to Mara's agent yet. Updates: `python3 scripts/push_tool.py research_browser`.
- Hannah's Open WebUI is on docker-server, so it cannot reach `research-service` on this host's Docker network; it would need the service published on the Tailscale address with the key.
- `~/docker/chromium` (the old personal browser) was stopped at Gordon's request; it only needed a `docker compose pull`.

## Vision and screenshots (v0.2.0, 2026-10-23)
- **`look` action** for the driver: the service returns a JPEG of the current page (shrunk to 1280 px, via Pillow) and a vision model (valve
  `VISION_MODEL`, default `or.mistralai/mistral-small-3.2-24b-instruct`, through the Open WebUI API) describes it; the text is scrubbed like page text
  and shown to the driver as "what a vision model saw". At most `MAX_LOOKS` (2) per research; about $0.0002 each.
- **`show_screenshot(url="")`** for the agent: posts a picture into the chat so Gordon can see what the browser saw. No address = the final page of
  that user's latest research (the service keeps one PNG per user in memory, taken when a session closes; lost on a service restart); with an address it
  opens the page, shoots it, closes it. The PNG is stored with Open WebUI's files API (`/api/v1/files/`) and shown with a `message` event, so the chat
  holds a short link, not image data. Needs the `OPEN_WEBUI_API_KEY` valve. The file belongs to the key's owner.
- `~/docker/chromium` (Gordon's personal browser) is published on 0.0.0.0:3000-3001 with **no login**.
