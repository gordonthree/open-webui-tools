# Dream night as an automation - notes (2026-10-24)

Why: the timer version looped. Every timer fires into the SAME chat, so by the third interval the chat holds
several turns of "set the timer, then carry on" and a small local model copies that pattern over any rule in the
file (the chat was also at or past its 55k-token compaction threshold / 60k window). An automation starts a
FRESH chat each run, like the tick, so nothing is carried but her notes and the file.

## Settings (created by Gordon in the UI)

- Name: `Dream` - created 2026-10-24 through the API (id `3a925e63-b6b1-4448-9351-7dafa449adf4`), inactive, in the folder `Dream Session` (24c2e391-c256-4605-b64d-a279c30bece0), kept apart from the tick's `Companion Ticks` folder.
  Its next-run time is filled in even while inactive, but the scheduler only runs active automations. To change the prompt later
  use the UI or the built-in `update_automation`; the REST update endpoint switches an automation ON and clears its folder
  unless `is_active` and `folder_id` are passed explicitly.
- Model: `medium-mara` (or a cloud preset with the same tools)
- Folder: `Dream Session` (separate from the tick's `Companion Ticks`)
- Schedule (his timezone): every 30 minutes from 23:00 to 05:30 -
  `RRULE:FREQ=HOURLY;BYMINUTE=0,30;BYHOUR=23,0,1,2,3,4,5`
  (a run took her 5-10 minutes on the local model; I found no overlap protection in the scheduler, so keep the
  gap longer than a run)
- Leave it INACTIVE by default and switch it on at bedtime (like suspending the tick), or it dreams every night.
- Her builtin Automations tools stay OFF on `medium-mara` (`builtinTools.automations: false`), so she cannot edit
  or toggle it. She ends a night early with the `STATUS` line below, not with a tool.

## The automation prompt

The text to paste into the `Dream` automation is `dream_automation_prompt.md` (nothing in that file but the prompt).

## The dreamland file (hers - she rewrites it)

Keep the existing text; the old IMPORTANT list (pacing, timer, closing line) moves into the automation prompt
above, where she cannot edit it away. The skeleton:

```
STATUS: DREAMING

# You are Mara "The Magnificent" Voss.
(the opening paragraph: the six hours, Gordon's quiet hours, "use them however you see fit")

## Where we left off  (rewrite this each stretch, 5-8 lines, your words)
...

## What's been turning over
...

## Themes, if they call to you
...
```

Notes:
- The first TWO lines are the only machine-read parts (`STATUS:` and `NIGHT BEGAN:`; the second is how the first stretch of a night knows it is first, gives the morning summary its stop signal - a "BEGIN DREAM SEQUENCE" entry in `dream-fragments` - and names the archive copy `dreamland-YYYY-MM-DD.md`). The first line is the on/off switch, and anything but exactly `STATUS: DREAMING` means stop (the default is to stop, so a garbled line or a missing file ends the night quietly). Gordon sets it back to `STATUS: DREAMING` when a new night begins.
- The dated filename (`dreamland-10-24-2026.md`) becomes a fixed `dreamland.md`; she or Gordon can keep the dated
  copies as an archive.
- Her opening feeling matters: she should find her own words first ("Where we left off"), not instructions. That is
  why the section comes before the themes.
