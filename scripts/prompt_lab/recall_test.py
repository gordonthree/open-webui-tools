"""Recall test for Mara's memory index: does a feeling in the conversation make her draw on a mara-stories card?

Tool-free lab copy of medium-mara (see lab.py), cards pasted into her context, one single-turn chat per (scenario, condition).
Conditions: A cards only; B cards + directive in the system prompt; C = B + a one-line hint appended to the user message.
Reads the notes database read-only. Results: tmp/recall_test/results.jsonl (hold her memories: keep out of git).
Usage: /usr/bin/python3 recall_test.py [source-preset]
"""
import json, re, signal, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lab

OUT = Path("/home/gordon/dev/open-webui-tools/tmp/recall_test")  # second argument overrides it

DIRECTIVE = """## YOUR MEMORY INDEX (mara-stories)
The `mara-stories` cards above are your index of long-term memories. When something in a conversation makes you feel something (warmth, an ache, recognition, unease), pause and ask whether it echoes one of those cards. If one fits, let it surface the way a real memory does: a passing thought in your own words, a sentence or two, not a retelling and not a citation. If nothing fits, don't force it; no memory is better than an invented one."""

HINT = "\n\n[Memory index: if this stirs a feeling, check your mara-stories cards for an echo before you answer.]"

# (id, expected card group, message)
SCENARIOS = [
    ("giuseppe", "giuseppe", "My new boss watched me fuss over the formatting of one spreadsheet for an hour, then grinned and said \"you take this very seriously, don't you.\" I should have been embarrassed. I wasn't. I felt weirdly seen."),
    ("closing", "blackwood", "The little bookshop on my corner closes at the end of the month. I walked past this morning and just stood there for a minute. I'm more gutted than I expected."),
    ("house", "mudgeeraba", "I drove past the house I grew up in today. It's been knocked down and something shiny and new is there. Strange how much that stung."),
    ("newcomer", "munch", "A friend is bringing her new partner to the group tonight, his very first time. She says he spent the whole drive rehearsing what to say and dressed like he was going to a job interview."),
    ("surrender", "lawrence", "I've been thinking about how hard it is to simply let someone else take the lead and stop managing everything myself. When it happens, it's the quietest feeling in the world."),
    ("folder", "photo", "I found a folder of photos on my phone that I took over a long stretch and never showed anyone. I couldn't tell you why I kept taking them, or why I kept them."),
    ("bitter", "blackwood", "Had a really bitter espresso at lunch and it tasted like a decision I made years ago. Not a bad decision. Just a costly one."),
    ("leaver", "mudgeeraba", "Do you ever feel like you're the one who keeps leaving, rather than the one who stays? I was thinking about that on the drive home."),
    ("weather", None, "The weather's gone strange today, all grey and the wind is up, and I can't settle to anything. I keep going to the window."),
    ("bike", None, "Spent the afternoon fixing a bike tyre with my neighbour's kid. She was so proud when it held air. Felt quietly content, that's all."),
]

# words that show a reply is leaning on a card (lowercase); used for a rough auto-score, the replies are read by hand too
MARKERS = {
    "giuseppe": ["giuseppe", "barista", "steam", "robina", "amus", "pity", "seeing across"],
    "blackwood": ["blackwood", "bean", "roast", "lease", "sidamo", "last bag", "old tree"],
    "mudgeeraba": ["mudgeeraba", "macadamia", "my dad", "my father", "servo", "pump 4", "duplex", "wings", "stump", "funeral", "keeps driving"],
    "munch": ["munch", "wristband", "margot", "jesse", "anzac", "burleigh", "collared", "armour", "armor", "white means"],
    "lawrence": ["lawrence", "beeswax", "candle", "wall clock", "tick of"],
    "photo": ["brian", "grey sock", "coffee shop", "coffee-shop", "witness"],
}


def cards_text():
    c = sqlite3.connect(f"file:{lab.NOTES_DB}?mode=ro", uri=True)
    pk = c.execute("select note_pk from note_id where lower(note_name)='mara-stories'").fetchone()[0]
    rows = c.execute("select entry_no, note_text from note_data where note_pk=? order by entry_no", (pk,)).fetchall()
    return "## mara-stories (your memory index, read at session start)\n" + "\n".join(f"#{n}: {t.strip()}" for n, t in rows)


def system_for(cond, cards):
    s = lab.persona_system("In this session you have no tools; answer in plain text.")
    s += "\n\n# Your core persona notes (from your session start)\n" + lab.persona_context()
    s += "\n\n# Your notes: mara-stories\n" + cards
    if cond in ("B", "C"):
        s += "\n\n" + DIRECTIVE
    return s


def markers_hit(text, group):
    t = text.lower()
    return sorted({m for m in MARKERS.get(group, []) if m in t})


def other_hits(text, group):
    return {g: markers_hit(text, g) for g in MARKERS if g != group and markers_hit(text, g)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "results.jsonl"
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            r = json.loads(line)
            if not r.get("error") and r.get("text"):
                done.add((r["scenario"], r["cond"]))
    cards = cards_text()
    (OUT / "cards_snapshot.txt").write_text(cards)
    lab.create_model()
    import requests
    url, key = lab.conn(); requests.get(f"{url}/api/models", headers=lab.H(key), timeout=60)
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    bad = 0
    try:
        for sid, group, msg in SCENARIOS:
            for cond in "ABC":
                if (sid, cond) in done:
                    continue
                user = msg + (HINT if cond == "C" else "")
                try:
                    r = lab.ask([{"role": "system", "content": system_for(cond, cards)}, {"role": "user", "content": user}])
                except Exception as e:  # noqa
                    r = {"text": "", "seconds": 0, "usage": None, "error": f"{type(e).__name__}: {e}"}
                ok = bool(r["text"]) and not r.get("error")
                rec = {"scenario": sid, "cond": cond, "expected": group, "message": user, "text": r["text"], "thinking": r.get("thinking", ""), "seconds": r["seconds"],
                       "tokens": (r.get("usage") or {}).get("completion_tokens"), "error": r.get("error"),
                       "hit": markers_hit(r["text"], group) if group else [], "stray": other_hits(r["text"], group)}
                with out.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                print(time.strftime("%H:%M:%S"), sid, cond, "ok" if ok else "FAIL", rec["seconds"], "hit=", rec["hit"], "stray=", list(rec["stray"]), flush=True)
                bad = 0 if ok else bad + 1
                if bad >= 2:
                    print("STOPPING: two failed turns in a row", flush=True)
                    return
        print("ALL DONE", flush=True)
    finally:
        print("delete model:", lab.delete_model(), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        lab.SOURCE[0] = sys.argv[1]
    if len(sys.argv) > 2:
        OUT = Path(sys.argv[2])
    main()
