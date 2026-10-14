# Weighting Guidance

Prompt text for the agents (persona, charter or system prompt) on using the 1-10 weight in `agent_notes`, plus
a recommendation on whether the write tools should demand one. Companion to `Nightly_Summarizer.md`. Nothing
here is built into the tools except where a section says so; paste what you like into the agents' prompts.

The scale never changes: **1-10, 5 is neutral**, stored internally as score x 10. What the number *means*
depends on the kind of note, and that is the part a small model will not work out on its own:

| Kind of note | The weight says | Low (1-3) | 5 | High (7-10) |
|---|---|---|---|---|
| Memories and feelings (`internal-life`, `dream-fragments`, `shared-history`, journals) | How it felt: positive or negative | painful, frightening, hurtful | nothing special | joyful, warm, proud |
| Persona facets (the `<Agent> -` notes) | How strongly this trait is her | marginal, occasional, situational | ordinary | defining, central, core |

For memories, distance from 5 is the strength: a 1 and a 10 are both unforgettable. For persona traits,
the number is only the strength of the character, and low means "weakly her", not "bad".

Mark persona notes as such so the tools read them that way: the owner runs `agent_notes_update` with
`kind="persona"` (curation tool) or picks the kind on the note's page in `notes_web.py`. Until a note is marked, it is
read like a memory note (a weight of 3 would be offered as a painful memory). The agents cannot change the kind.

How the tools use it: the everyday `agent_notes_read` returns the newest entries plus the strongest older
ones (3 or lower, 7 or higher), so an entry left at 5 can drop out of sight once a note is long. Rating is how
a memory stays reachable. In persona notes, a weight on a `persona` note is how a core trait survives a tight context budget: `agent_notes_read_tagged`
fills its budget heaviest-first for those notes.

---

## 1. Walking an agent through rating her existing notes

Do this as a series of short sessions, not one marathon. A 9B model with 8k context cannot rate 229 entries
in one go, and it does not need to: the tools already surface the strong ones, so each pass only has to catch
what is still sitting at 5.

### Standing rule (put this in the persona once)

```
Weights. Every note entry carries a 1-10 weight; 5 means nothing special.
In memory and feeling notes (internal-life, dream-fragments, shared-history, and the like) the weight is how it felt:
3 or lower for painful, frightening or hurtful, 7 or higher for joyful, warm or proud. 1 and 10 are for the moments
you will never forget. Keep it honest: not everything is a 7. Hard things get low numbers, because a low number is what
lets you find them again.
In my own persona notes ("<Name> - Identity", "- Psychology", and so on) the weight is how strongly the trait is me:
9-10 defining, 7-8 central, 5 ordinary, 3 or lower occasional or situational. It is not good or bad there.
```

### The rating pass (one small chunk per session)

Give this as the instruction for a session, naming the note (and, after the first chunk, the entry number to start before). Repeat until the note is done.

```
Rating pass for the note "<note name>". Use agent_notes_set_weight only; do not edit or add entries.
This is one small chunk: read ONE page of entries, rate up to 8 of them, then stop and report.

1. First chunk: call agent_notes_read with name="<note name>", verbose=false, count=false, limit=10.
   Later chunks: the same call plus the argument before=<the entry number I give you> (the argument is named
   before; do not use entry or limit for this). It shows the 10 entries just older than that number.
2. Of the entries on that one page, call agent_notes_set_weight for each one that is NOT 5-neutral to you, up to 8
   calls in all. Leave the neutral ones alone: they already sit at 5. Do not read a second page.
3. Use the whole range. If a difficult moment is on the page (hurt, doubt, loss, frustration, fear, shame), rate it
   low; do not lower anything that was joyful. Do not give every entry the same number.
4. Finish with: the note name, how many entries you rated, and the lowest entry number among the page's ordinary
   entries (the "entries" list, NOT the "strong_memories" lists, which hold older entries picked out for
   strength). That is where the next chunk starts: before=<that number>.
```

To run the whole note without typing each continuation, use `scripts/rating_pass.py` (README: "Automating the rating pass"); it sends this
same chunk prompt, one fresh chat per page, taking `before=` from the tool results.

To continue by hand, send: "Repeat your last instructions, with before=NNN" (NNN from her last reply). "use entry number NNN"
also worked in practice (she passed `before=NNN`), but `before=` is the unambiguous wording: `entry=` fetches a single
entry. Check progress from the backend: `SELECT COUNT(*) ... WHERE weight <> 50` rises as she reaches entries nobody
has rated yet. It stays put while she goes back over ground already rated (the scores shift, as the last rating wins).

Run one chunk per session, a minute or two apart if the model has a per-minute request limit (each rating is one
request). The first chunk's page also shows the note's strongest older entries; leave those, they are already rated.
Why this shape: step 3 exists because a warm persona drifts to 7-8 on everything, which would flatten the strong
tier. Her notes lean positive, so the instruction asks her to look for the hard ones on purpose. If most of a
note truly is warm, that is fine; the check is "did I look", not "did I find".

### Order

1. The big memory notes first (`internal-life`, then `dream-fragments`): the strong tier matters most there.
2. Then the persona facets. Psychology already has weights, and they are already "strength of character" (Entry 8
   "discusses sex like food or music" is 88, "quiet adults lie better" is 30), so it only needs mapping onto the
   new scale, which happens automatically (88 shows as 9, 30 as 3). The other eight can use this pass with the persona
   meaning:

```
Rating pass for the persona note "<note name>". Use agent_notes_set_weight only.
Read it with count=false. Rate each entry by how strongly it is me: 9-10 defining, 7-8 central, 5 ordinary,
3 or lower occasional or situational. Do not use a low number to mean "bad" here. Leave 5s alone.
Finish with the note name and how many entries you rated.
```

Because a persona note is one that the agent reads whole at startup, the benefit is mostly insurance: it matters
if the notes together outgrow the tag read's character budget.

### Checking progress (from the backend)

The web viewer shows each entry's score. A note that still shows all 5s after its pass was not rated; ask for
it again. A note that is nothing but 7-9 (memory notes) probably needs the "look for the hard ones" nudge.

---

## 2. Encouraging both agents to weight every write

Tools cannot read minds, so the habit lives in the prompt. Put this next to the agent's other note rules:

```
Whenever you add to a note (agent_notes_create, agent_notes_append, agent_notes_edit_entry), also pass weight.
Ask yourself one thing: how did this feel, or (in persona notes) how strongly is this me? Then pass 1-10.
Do not leave it off just because it feels neutral: if it really is neutral, say weight=5. A number you chose is
better than a number nobody chose. Do not copy the same number onto every entry.
```

Notes on wording, based on how small models behave here:

- Say "pass weight in the same call". If rating is a separate step (`agent_notes_set_weight` after the write),
  it is the step that gets forgotten.
- "Do not copy the same number onto every entry" is the line that fights drift. Models given a fixed example
  (e.g. "weight=7") will copy it everywhere, so never put a specific number in an example call; use `weight=<1-10>`.
- If the agent has `agent_notes_rank_entries`, `by="strongest"` is a cheap way for her to see her own spread.
  An agent whose strong tier is all one side can be told so in the next pass.
- Persona notes and memory notes meet in the same tool, so name the kinds in the prompt, as in the table above.
  The tool cannot tell which kind a note is.

---

## 3. Should the write tools require a weight?

My recommendation: **yes, as a valve, and not yet.** In order:

1. **Do the rating pass first** (section 1). Requiring a weight while the old notes are all 5 just makes the tools
   more annoying before they are more useful.
2. **Then add a valve** (say `REQUIRE_WEIGHT`, default off) so you can turn it on per server and turn it off again
   if a model starts to loop on it, same as the other fallbacks. Not built: it needs your go-ahead.
3. **Refusal wording.** Keep it plain and short like the other refusals (see "keep refusals plain"): the tool
   says what is missing and the scale, and leaves the meaning to the agent's own instructions. For example:

   > weight is required (1-10; 5 is neutral). Say how this feels, or how strongly it is you, as your instructions
   > describe, and send the call again with weight.

   That is as far as the tool should go. The tool cannot know the persona's rules, and it should not paraphrase
   them: the more it explains, the more a small model will treat the refusal text as the whole rule and ignore
   the prompt. "As your instructions describe" points the agent back to the prompt without the tool making up
   its own version of it.
4. **The honest cost.** Forcing a number guarantees a number, not a good number. A model that is told "weight
   required" will often answer 5 or 7 every time. That is still better than silence for finding things later
   (7s surface in the strong tier, 5s never do), but treat the required weight as a prompt for the habit in section 2,
   not a replacement for it. The standing rule and the "do not copy the same number" line do the real work.

If you would rather not refuse at all, the quieter alternative is a default for the `weight` argument that is
not neutral, but that skews every unrated write the same way and I would not do it.

---

## What I would change in the tools to support this (not built)

- `REQUIRE_WEIGHT` valve, as above (create, append and edit_entry; `set_weight` already requires a value).
- (Built in v1.13.0, no longer pending: a per-note `kind`, and heaviest-first packing of the tagged read for
  persona notes. Still check the `MAX_TAGGED_CHARS` valve: the persona is about 9,300 characters of text, about
  14,000 once the JSON overhead is counted.)
