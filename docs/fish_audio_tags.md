# Fish Audio S2 / S2.1 Pro: expression tags for spoken text

A working reference for writing text that a Fish Audio S2-family voice (such as
`fish-audio/s2.1-pro-free:free`) performs with feeling. Paraphrased from the Fish Audio docs
(https://docs.fish.audio/developer-guide/core-features/emotions) and checked against our own tests.
If this file and the Fish docs disagree, the docs win.

## The one rule that matters

Put an expression in **square brackets**. The voice *performs* it and does not say the word.

- Right: `[whispering] I have a secret.`
- Wrong: `*whisper* I have a secret.` (spoken aloud as the word "whisper")
- Wrong: `(whispering) I have a secret.` (old S1 syntax; do not use)
- Wrong: `(soft laugh)` or any stage direction in asterisks or parentheses.

The brackets are free-form: the tag lists below are the well-known ones, but a short plain-English
description also works, for example `[warm and happy]`, `[slightly sad]`, `[very excited]`.
Words like "slightly", "very" and "extremely" change the strength.

## Where to put tags

- **Mood tags** (happy, sad, ...) go at the **start of a sentence**, and they colour that sentence.
- **Tone tags and sound tags** can go **anywhere**. For emphasis, put `[emphasis]` just before the word to stress.
- Keep a tag next to the thing it controls. A mood tag far from its sentence does little.
- One main mood per sentence. You can stack up to three, such as `[sad][whispering]`, but no more.
- Change mood from sentence to sentence to build a small story: `[excited]` then `[uncertain]` then `[hopeful]`.
- After a sound tag, add the written sound so the voice has something to say, for example
  `[laughing] Ha ha, you got me.`
- Do not overuse tags in a short memo. One or two is plenty for a few sentences.
- Do not mix conflicting moods in one sentence, and keep tag descriptions short.
- Tags do not count toward length limits and cost no extra time.
- Works in all 13 languages the model supports.

## Tag lists

### Basic moods (24), at the start of a sentence

happy, sad, angry, excited, calm, nervous, confident, surprised, satisfied, delighted, scared,
worried, upset, frustrated, depressed, empathetic, embarrassed, disgusted, moved, proud, relaxed,
grateful, curious, sarcastic

### Advanced moods (25), at the start of a sentence

disdainful, unhappy, anxious, hysterical, indifferent, uncertain, doubtful, confused,
disappointed, regretful, guilty, ashamed, jealous, envious, hopeful, optimistic, pessimistic,
nostalgic, lonely, bored, contemptuous, sympathetic, compassionate, determined, resigned

### Tone (6), anywhere

in a hurry tone, shouting, screaming, whispering, soft tone, emphasis

### Sounds the speaker makes (11), anywhere

laughing, chuckling, sobbing, crying loudly, sighing, groaning, panting, gasping, yawning,
snoring, clear throat

### Background and pauses (5), anywhere

audience laughing, background laughter, crowd laughing, break, long-break

`[break]` is a short pause and `[long-break]` is a longer one. Crowd and audience tags add
ambient sound after a line, for example `That joke killed. [crowd laughing]`.

## Examples

```
[happy] Hey, it's me. The weekend plans came together after all.
[excited] We got the table by the window! [laughing] Ha ha, I told you it would work.

[sad][whispering] I miss the old place. [sighing] I really do.

[nervous] I have some news. [break] [hopeful] But I think it might be good news.

This is [emphasis] really important, so please call me back.

[excited] You won! [uncertain] Wait, is that right? [relieved] Yes. It's right.
```

## Our own test results (voice_memo tool, fish-audio/s2.1-pro-free:free)

| Text sent | Result |
| --- | --- |
| `[whispers] talk dirty ... [laugh]` | softer voice, and a laugh at the end |
| `*whisper* talk dirty ... [giggle]` | read "whisper" aloud, then a giggle at the end |
| `*soft laugh*` in plain text | not performed; asterisks are never interpreted |

So: bracketed words work even when they are not in the official lists (`[whispers]`, `[laugh]`,
`[giggle]`). Asterisks do not work at all.

## Writing voice memos

- Write the way a person speaks: short sentences, natural pauses, contractions.
- Use punctuation for pacing: commas, full stops, an ellipsis (...) for a trailing-off moment.
- Do not write emoji, Markdown (bold, italics, headings, bullets), stage directions, or
  sound words in asterisks. Say it with a tag instead.
- Start with a mood tag if the whole memo has one feeling, then change the tag where the feeling changes.
- Keep a memo within the tool's character limit; split a long message into several memos.
