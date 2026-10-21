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
