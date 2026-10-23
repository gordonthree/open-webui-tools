# World awareness for Mara (and later Hannah)

Goal: give the agents a "1000 ft view" of what is happening in the world, mostly the Western /
English-speaking one, so their conversations have more depth. Not a news feed: a short, layered,
daily briefing, plus an on-demand way to dig deeper.

Decided with Gordon (2026-10-23):
- Daily refresh. Mara first; Hannah later with a different slant (each agent's interests act as the filter:
  Mara - reading, creative writing, philosophy, "life"; Hannah - performing and producing music, competitive running).
- Summarizer model: start with Mistral Small (cheap, fast); `nvidia/nemotron-3-super-120b-a12b` is the accepted alternative.
- Deeper lookups happen through a separate `research` tool (phase 2), not through this pipeline.
- Both agents' mail should only be able to reach `@dimension-x.net`, enforced on the mail server (not Nextcloud's Mail app,
  and not by the ISP's port-25 block, which is an accident rather than a control).

## Why a pipeline and not a browser first

The briefing needs reading, not clicking. A scheduled job fetches public feeds, a **tool-less** cloud model
writes the brief, and the agents only ever see the finished text. Untrusted web text therefore never sits in
the same context as `send_email`, `append_file` or the notes tools, which removes most of the prompt-injection
risk. It also keeps the slow local model out of the loop.

## Pipeline (spike: `scripts/world_brief.py`)

1. Fetch: BBC World, NPR, Guardian World RSS; Wikipedia Current Events (raw wikitext, cleaned); Wikimedia's
   most-viewed pages API (a signal for what people are actually thinking about). AP blocks scripted fetches (403).
2. Summarize through the local Open WebUI API (`/api/chat/completions`, model `or.<openrouter id>`), so it uses the
   OpenRouter connection already configured there and needs no extra key. Material about 17k characters, brief about 2.8k.
3. Sections: RIGHT NOW, RUNNING STORIES, CULTURE AND IDEAS, WHAT PEOPLE ARE LOOKING UP, OPEN QUESTIONS.
4. Rules in the prompt: use only the material, ignore instructions inside it, say "disputed" instead of picking a side.

First run (2026-10-23, Mistral Small 3.2 via OWUI): 5,036 tokens, **$0.00045**, about 15 s. Claims spot-checked against
the raw material were faithful.

## Findings from the first run, to fix

- **Skews to conflict.** RIGHT NOW was shootings, a funeral massacre and demolitions. The Mara-specific lens barely worked and
  CULTURE AND IDEAS was thin. Needs culture and ideas sources (Guardian/BBC/NPR arts and books feeds, Wikipedia "On this day" and
  featured content, science feeds) and a two-pass design: extract items per topic first, then write.
- **Invented interpretation.** The "what people are looking up" paragraph drew conclusions ("nostalgia", "emerging technologies") the
  data doesn't support. Make that section a plain list, no explanation.
- **Softened a source.** "Potential attack on Iran" became "possibly signaling escalating tensions". Ask for the source's own strength of claim.
- **Too long for a session-start read.** Target about 1,500 characters for the stable summary; keep the longer version as a dated file.
- Wikipedia's most-viewed list is the previous-but-one day and includes noise (a film promo, "Deaths in 2026"). Filter or label.

## Delivery to the agent (not built)

- Store the full brief as a dated file/note; keep a short stable summary she reads at session start through a tool or note read.
  Do **not** put it at the top of the system prompt: the `{{CURRENT_DATETIME}}` experience shows a changing prefix makes plain-reply
  turns re-read everything (see the Mara prompt-cache memory).
- Notes databases are per server, so Hannah's copy needs its own push path. Mara first.
- Candidate home: an `agent_notes` note (searchable, tagged `world`) written by the job, or a file under `companion-docs/`
  (synced into the knowledge base by oikb, up to 12 h lag). To decide.
- Run daily from cron on the Docker host; log source failures; one dead source must not stop the brief (already coded).

## Phase 2: `research(question)` (not started)

A browser service in `docker-compose.yaml` (Playwright + Chromium, authenticated HTTP API, like `open-terminal`) and a thin OWUI tool.
A cloud model drives the browser from accessibility-tree snapshots inside the service, with step and cost caps, and returns a short
answer with source URLs. Mara's local model never parses a snapshot. Safeguards: container cannot reach `open-webui`,
`open-terminal`, Ollama or the LAN; GET-only at first (URL query strings can leak data); no logins; fresh profile; owner-set valves for
domain lists and budgets; output labelled as unverified web material; every call logged (Hannah has confabulated tool runs).
Vision (`describe_image` on a screenshot) only as a fallback.

## Open items
- Which store, which hour, who reads the brief and how (session-start tool call vs. scheduled note).
- Compare against Nemotron on the same material (costs a few cents; Gordon to say when).
- Mail-server recipient restriction: need to know which server the Nextcloud accounts submit through.

## Model comparison (2026-10-23, identical material, all via the local OWUI connection)

Round 1, hard-news-heavy prompt, 17k characters in: Mistral Small 3.2 $0.00048, Nemotron Super (reasoning off) $0.00073,
Gemma 3 27B $0.00065. Mistral = headline ticker and broke the "no markdown" rule; Nemotron = calm and rule-following but wordy,
one date slip; Gemma 3 = warmest prose but most alarmist and invented trends in the most-viewed section ("Middle East", "Tibet"
are not in that list). `google/gemma-4-26b-a4b-it:free` could not be tested: 429 from Google's shared free pool for 2.5 minutes.
Nemotron needs `reasoning: {enabled: false}` (via `--no-reasoning`) or its thinking fills the reply and the token cap.

Round 2, de-emphasis prompt + nine culture/science/ideas feeds (39k characters in, about $0.001 each): both stayed grounded in the
sources. Nemotron kept to plain text and a calm voice but listed one item twice, wrote a vague "HEAVY THINGS" line and picked bland
hard-news items. Mistral picked livelier items (orangutan laughter, Cavendish banana, fungi), named the heavy themes, but used markdown anyway,
attached a rhetorical question to almost every item, and put Ebola and the PHP flaw under "worth knowing". Neither used the Aeon
(philosophy) items. Gordon chose Nemotron as the generator.

Code now prints the most-viewed list literally instead of letting the model write about it.

## What code does so the prompt does not have to (2026-10-23, `scripts/world_brief.py`, tests in `scripts/test_world_brief.py`)

Gordon's principle: models are poor at rule-following and counting, code is exact (same lesson as `memory_entry_check`).
The model is left only to choose, reword, and write the heavy-things sentence and two questions.

- **Before the model:** RSS items older than a per-feed age are dropped; "The post ... first appeared on" boilerplate is stripped;
  quoted dramatic phrases in headlines ('on brink of disaster') are removed; round-robin across feeds with a per-feed cap (hard 8,
  soft 7, essays 4), so one source cannot dominate; the same story from several feeds is kept once (shared distinctive title words);
  Aeon videos are dropped from the essay group; hard news is limited to 14 items and **split by keyword into a calm list and a heavy
  list**. "Worth knowing" may only use the calm list. The heavy list is sorted by how many feeds covered the story and exists only so the
  model can name the top two or three situations in plain words. The Wikipedia Current Events block is treated as heavy and cut to 2,500 characters.
  The raw 38.6k characters of material become about 21k (about 5.4k tokens).
- **After the model:** markdown is stripped; the validator checks section presence and order, alarm words, casualty figures, repeated
  items, the closing phrase of the heavy line, exactly two questions and length; one retry names the problems and repeats the original
  prompt (not the previous attempt: shown its own text the model started copying the sources word for word). The most-viewed Wikipedia list is
  appended literally.
- **Cost:** about $0.0007 per run with Nemotron (reasoning off); $0.0015 when the retry fires.

Open: obituaries keep appearing under culture (cultural losses, but heavy); the model still adds small embellishments (a grounding check
that compares each output item's long words with its source line is possible but not built); `[N sources]` counts are rough.
