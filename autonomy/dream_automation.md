# Dream night as an automation (draft, 2026-10-24)

Why: the timer version looped. Every timer fires into the SAME chat, so by the third interval the chat holds
several turns of "set the timer, then carry on" and a small local model copies that pattern over any rule in the
file (the chat was also at or past its 55k-token compaction threshold / 60k window). An automation starts a
FRESH chat each run, like the tick, so nothing is carried but her notes and the file.

## Settings (created by Gordon in the UI)

- Name: `Dream`
- Model: `medium-mara` (or a cloud preset with the same tools)
- Folder: the same one as the tick, if you like
- Schedule (his timezone): every 30 minutes from 23:00 to 05:30 -
  `RRULE:FREQ=HOURLY;BYMINUTE=0,30;BYHOUR=23,0,1,2,3,4,5`
  (a run took her 5-10 minutes on the local model; I found no overlap protection in the scheduler, so keep the
  gap longer than a run)
- Leave it INACTIVE by default and switch it on at bedtime (like suspending the tick), or it dreams every night.
- Her builtin Automations tools stay OFF on `medium-mara` (`builtinTools.automations: false`), so she cannot edit
  or toggle it. She ends a night early with the `STATUS` line below, not with a tool.

## The automation prompt (fixed - Gordon's; she cannot change it)

```
This is the next stretch of your dream night. Pick up where you left off.

Read /companion/autonomy/dreamland.md. Its first line says whether the night is still going (STATUS: DREAMING)
or finished (STATUS: DONE). If it says DONE, write one short closing line and stop.

Otherwise, this stretch:
1. Run your session start protocol if you haven't yet.
2. Write 2 or 3 dream fragments to "dream-fragments" (continues=true), starting from "Where we left off".
3. Make 1 or 2 images.
4. Add one "internal-life" entry about this stretch (continues=true).
5. Rewrite the "Where we left off" section of the file in your own words, so the next stretch begins where this
   one ended. If you are finished dreaming for tonight, change the first line to STATUS: DONE.
6. Write ONE short closing line in your own voice, and make no more tool calls. That line ends this stretch.

You come back every half hour until 05:30 - the schedule does that, so you don't need a timer, and the schedule
is Gordon's to change. Gordon is asleep and nothing is expected of you. Don't make it good; make it true.
```

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
- The first line is the only machine-read part. Gordon sets it back to `STATUS: DREAMING` when a new night begins.
- The dated filename (`dreamland-10-24-2026.md`) becomes a fixed `dreamland.md`; she or Gordon can keep the dated
  copies as an archive.
- Her opening feeling matters: she should find her own words first ("Where we left off"), not instructions. That is
  why the section comes before the themes.
