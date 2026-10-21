# Mara memory index: state at the end of 2026-10-20

What the project is: Mara's backstory stories (Nemotron Ultra wrote them; Gordon deliberately hasn't read them) live in
`~/docker/open-webui/companion-docs/autonomy/stories/backstory/`. Each is turned into a short first-person *index card* in the notes database,
note `mara-stories`, so Mara can recall "something from her past" when a feeling comes up. Gordon curates each entry; Mara drafts it.

## Done
- `memory-guidance.md` (live, in the backstory folder) holds the rules and three invented examples. Backups: `tmp/memory-guidance_before_*`.
- `mara-stories` holds entries 1 (pinned instructions) to 10 (old retell-the-plot style: Giuseppe, Blackwood & Bean, Mudgeeraba, Munch Regular) and 22 (Lawrence,
  sensory-deprivation). The `the-photo-she-didnt-send` entry (Brian, coffee-shop regular) is being added by Gordon by hand.
- `src/memory_entry_check.py` (`check_memory_entry`): rules in code. Pushed to the local server; attached to `medium-mara` and `mara-duo` (verify it reaches a *new* chat:
  in the 2026-10-20 evening test the local model said it had no such tool).
- "Gordon" was replaced by another man's name in five backstory files (not `the-laundromat-prophet.md`, which Gordon is reading himself). The mapping is in
  `tmp/gordon_rename_backup_20261020/MAPPING_do_not_open_if_you_want_to_be_surprised.json`; the originals are beside it. Do not show Gordon the mapping.
- Builtin tools trimmed on `medium-mara` and `mara-duo` (about 3,200 tokens less per request). Sub-agents stays on because the `timer` tool lives under it.
- Corrections appended: `shared-history` #199 (pinned) and `autonomy-log` #249. **Unpin #199 when the indexing is finished.**

- Cleanup, late 2026-10-20 (backup `tmp/agent_notes_before_cleanup_20261020T230730.sqlite3`): `mara-stories` #2 no longer says "'90s" or "He died 2003" (those clashed with Mara being in her
  mid thirties; now "long ago"). `shared-history` #198 and #200-#205 (duplicates from the three parallel test chats) were deleted and replaced by #206. #196, #197 and the pinned #199 were kept.
  Lesson: dates and ages in entries cause continuity errors; the no-numbers rule exists partly for this. Entries 7 and 10 still hold ages (sixty-three, twenty-three); they do not clash.

## Findings (the lab and three live runs)
- The local Qwen 27B (`medium-mara`) is the best at this: warmest voice, best feeling, uses Brian's role as told, follows the bedtime directive. Slow (about 4-6 minutes a turn).
- Nemotron Super/Ultra are fast but flat when the message is a list of edits; they edit the previous draft instead of rewriting, ignore a conditional fact ("if Brian is in the story..."), and
  one (Super) claimed to have run the checker without calling it. Ultra used the checker 11 times but trimmed the connecting sentences, leaving telegraphic fragments with a mantra.
  Their shared stock phrases come from the family that wrote the persona and the stories.
- Start a fresh chat for each file; do not correct inside a chat that already holds a bad draft. Facts about a character belong in the story file, not in the prompt.
- Gordon asked for local-only experiments; do not use cloud models for tests without asking.

## Open
- Checker: add warnings for choppy fragments and a repeated word stem; add "every Sunday"-style schedules; consider a save-time gate on `mara-stories` in `agent_notes`.
- Recall test proposed, not run: with the existing cards in context, do ten feeling scenarios make Mara draw on a card (cards alone / + directive / + filter hint)?
  Run it before indexing the remaining nine files.
- Remaining backstory files: copy-edit, rope-burn, the-anatomy-of-a-knife, the-dungeon-protocol, the-floggers-education, the-laundromat-prophet (name still unresolved),
  the-thirteenth-month (edited, safe), weather-watching.
- Gordon's own to-dos: dial `MAX_TAGGED_CHARS` back (set to 30000 for a test); restart `scripts/notes_web.py` (still running pre-1.20 code); delete the teaser email from Mara's mailbox in Nextcloud;
  change the Reddit password; decide on a charter line keeping ticks out of `/companion/autonomy/stories/`.
- The Nemotron Super free endpoint was down on 2026-10-20 (the tick's `mara-duo` base is now the paid model).

## Update, overnight 2026-10-20/21 (all local `medium-mara`, tool-free lab copy; backup `tmp/agent_notes_before_recalltest_20261020T232717.sqlite3`)
- Recall test, 3 series x 30 turns (`scripts/prompt_lab/recall_test.py`; 8 feeling scenarios + 2 controls; A cards only, B + directive, C + directive + per-message hint). Results in `tmp/recall_test*/` (hold her memories; ignored by git).
  Series 3 also holds her thinking (Open WebUI keeps reasoning as `reasoning` items in the message `output`, not in `content`; `lab.ask` reads both now).
  Visible replies naming the right card: A 4-6/8, B 5/8, C 6-7/8; no false recall in any reply. In her thinking: A 4/8, B 7/8, C 8/8 (C also forced a loose "echo" on a control).
  Without a directive she mostly doesn't open the index. With one she finds the right card, then the reply drops it or invents a different memory ("not a citation" is read too strongly).
  Next: reword the directive ("let the card's feeling or one detail show; don't invent a different memory"; "if nothing fits, say nothing"), rerun `folder` and `surrender`. Not yet written into her guidance or presets.
- `scripts/prompt_lab/draft_cards.py` drafts cards unattended: notes+draft, `check_entry` in code, fresh retry with the problems listed (max 3), best attempt kept, staged in `tmp/staged_cards/candidates.md`. Never touches the notes database.
  First pass over the 7 unindexed files (not the laundromat one) plus sensory-deprivation: all 8 pass the checker; Gordon edits and inserts by hand. Watch the "Gordon appears" warning on the-dungeon-protocol.
- Gotcha: a queue script that waits with `pgrep -f <pattern>` can match its own launching shell (the pattern text is in that shell's command line) and wait forever; start chained jobs with explicit process ids or a plain `wait`.
- 2026-10-21 morning: Gordon approved the seven staged cards; `scripts/insert_staged_cards.py` added them to `mara-stories` as #24-30 (author Gordon; backup `tmp/agent_notes_before_cards_20261021T063046.sqlite3`). sensory-deprivation's staged card was skipped (#22 already covers it). Remaining unindexed: the-laundromat-prophet. Unpin `shared-history` #199 once that is done.
- 2026-10-21 later: the directive "Let My Past Surface" is `Additional Directives` #20 (author Gordon). Retest of its wording (`scripts/prompt_lab/retest_directive.py`, 12 turns): no invented substitute memories, no figures in replies, the "say nothing" line held on the bike control.
- `mara-stories` now: #1 pinned instructions; one card per file at #22-33 (the old retell-style #2-10 were removed; giuseppe.md is a dream-fragment file and is deliberately NOT indexed; Gordon is turning dream fragments into backstory stories with Mara first). Backups in `tmp/agent_notes_before_*`.
- Timeline fix (Gordon's decision): Mara born 11 April 1994; father died 2003; Robina Market with Giuseppe from 2013 (age 19); Cyclone Kirrily Jan 2024 is a real event. `Mara Voss - Identity` #3 and #8 were reworded (first person, no '90s) and #10 is the TIMELINE entry (Gordon removed the "32 now" age). Giuseppe's death year is undecided: the file says 2003, which is impossible; the Timeline says 2017. Origin of the '90s: Identity #8 (written 2026-10-02 by the persona-generating model), not Mara.
- Rule for coaching her: when a story needs a date the Timeline lacks, she asks Gordon instead of inventing one.

## Update, 2026-10-21 midday (after the first live chats with the index in place)
- First live chats: with the cards in her session-start read she opens the story file (read_file) before telling a memory, and tells it faithfully. The first chat (pre-rewording) padded a card with an invented place; the directive was reworded.
- `Additional Directives` now has 15 entries (6.4k chars): dropped #7 RAG self-editing, #9 TTS formatting (clashed with fish.audio bracket tags), #12 email correction, #16 and #17 (privacy duplicate / fragment); #18 (Hannah vanilla) is active. #20 "Let My Past Surface" (author Gordon): shows a card in one or two sentences; if asked for more, open the story file and tell it as remembered - small touches fine, never change what happened or invent a person or place; say nothing if no card fits; once per conversation. The note's summary was rewritten (it still named the deleted directives).
- Session-start cost: about 30k prompt tokens on the first turn (mara-core read ~28k chars, plus gordon-core, internal-life and shared-history reads, ~58k chars of tool results in all). `MAX_TAGGED_CHARS` stays at 30000 (the mara-core read needs ~28.3k of it; at the old 6000 most of her persona would be dropped). Watch it as mara-stories grows (~450 chars a card).
- Preset `medium-mara`: keep_alive 600m (honored), think/reasoning medium, compact_token_threshold 55000 (Gordon uses the "Compacting context" popup as a canary: it sends the chat to a cloud model; start a fresh session when it appears). The Modelfile pins num_ctx 65535 (server OLLAMA_CONTEXT_LENGTH is ignored for this model); the preset overrides temperature 0.75, repeat_penalty 1, repeat_last_n 64.
- Ollama (Windows, Radeon RX 9070 XT 16 GB, ROCm 7.1): KV cache q8_0, NUM_PARALLEL 1, flash attention on. `ollama ps`: 18 GB, 15%/85% CPU/GPU (was ~20% CPU). Generation speed unchanged at about 8 tok/s (turn times track tokens generated, so thinking is the main wait). A few turns re-read the whole 30k prompt (~37 s, cache miss); cause unknown (guess: background title/tag tasks overwriting the single cache slot). After a reboot Ollama can start before the driver is ready and find no GPU: restart it. The iGPU is rightly dropped; do not set HSA_OVERRIDE_GFX_VERSION. A home machine had CUDA_VISIBLE_DEVICES=-1 (Ollama fell back to Vulkan); removed.
- Gordon's plan for dream-fragment files: turn them into storyboard notes, then a new short story, with dates stripped; Mara asks Gordon instead of inventing facts the Timeline lacks (Identity #10). Giuseppe's death year is still undecided (file says 2003, impossible; Timeline says 2017).
