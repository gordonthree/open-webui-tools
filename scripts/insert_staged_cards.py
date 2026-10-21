"""Insert approved index cards from tmp/staged_cards/candidates.md into the mara-stories note.

Takes a backup of the notes database first, then appends each card through the notes tool's own append_entry_db. A card is skipped if
the note already has an entry for the same story file (so a second run is harmless) and refused if it fails check_entry.
Usage: /usr/bin/python3 scripts/insert_staged_cards.py [--db PATH] [--candidates PATH] [--author Gordon] [--dry-run] [--skip file.md ...]
"""
import argparse, re, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from agent_notes_full import append_entry_db  # noqa: E402
from memory_entry_check import check_entry, Tools  # noqa: E402

NOTE = "mara-stories"


def parse_candidates(text):
    """{file name: card text}: the first paragraph under each '## file.md' heading."""
    cards = {}
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        head, _, rest = block.partition("\n")
        paragraph = rest.strip().split("\n\n")[0].strip()
        cards[head.strip()] = paragraph
    return cards


def existing_text(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return "\n".join(r[0] for r in c.execute(
            "SELECT d.note_text FROM note_data d JOIN note_id n ON n.note_pk = d.note_pk WHERE lower(n.note_name) = ?", (NOTE,)))
    finally:
        c.close()


def backup(db, dest):
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close(); src.close()
    if ok != "ok":
        raise SystemExit(f"backup integrity check failed: {ok}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / "docker/open-webui/data/agent_notes.sqlite3"))
    ap.add_argument("--candidates", default=str(ROOT / "tmp/staged_cards/candidates.md"))
    ap.add_argument("--author", default="Gordon")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip", nargs="*", default=[])
    args = ap.parse_args()

    cards = parse_candidates(Path(args.candidates).read_text())
    have = existing_text(args.db)
    banned = [p.strip() for p in Tools.Valves().BANNED_PHRASES.split(",") if p.strip()]
    todo = []
    for fname, card in cards.items():
        if fname in args.skip:
            print(f"skip   {fname}: asked to skip")
        elif f"({fname})" in have:
            print(f"skip   {fname}: the note already has a card for it")
        else:
            verdict = check_entry(card, banned=banned)
            if verdict["problems"]:
                print(f"REFUSE {fname}: {'; '.join(verdict['problems'])}")
            else:
                print(f"insert {fname}: {verdict['characters']} characters")
                todo.append((fname, card))
    if args.dry_run or not todo:
        print("dry run, nothing written" if args.dry_run else "nothing to insert")
        return
    dest = ROOT / "tmp" / f"agent_notes_before_cards_{time.strftime('%Y%m%dT%H%M%S')}.sqlite3"
    backup(args.db, str(dest))
    print("backup:", dest)
    for fname, card in todo:
        r = append_entry_db(args.db, NOTE, card, args.author)
        print("added ", fname, "->", r)


if __name__ == "__main__":
    main()
