"""Draft mara-stories index cards from backstory files, unattended, on the local model, into a staging folder.

For each file: ask a tool-free copy of medium-mara for the four notes and a draft (the prompt that worked in the lab), run
src/memory_entry_check.check_entry on it in code, and if there are problems start a FRESH attempt with those problems added to the
prompt (the lab found that correcting inside a chat that already holds a bad draft doesn't work). Up to ATTEMPTS tries; the best one wins.
Nothing is saved to the notes database: candidates land in tmp/staged_cards/ for Gordon to edit and insert by hand.

Usage: /usr/bin/python3 draft_cards.py [--facts facts.json] [--attempts 3] file-stem [file-stem ...]
  facts.json maps a file name to a sentence the model must be told (who a person really is), e.g. {"the-photo-she-didnt-send.md": "Brian was ..."}.
  Facts about a character are better written into the story file itself.
"""
import argparse, json, sys, time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "src"))
import lab
import run_lab
from memory_entry_check import check_entry, Tools

OUT = HERE.parent.parent / "tmp" / "staged_cards"


def banned_phrases():
    try:
        return [p.strip() for p in Tools.Valves().BANNED_PHRASES.split(",") if p.strip()]
    except Exception:  # noqa
        return ["spin cycle", "low-voltage", "quiet strength"]


def prompt_for(fname, fact, earlier_problems):
    base = run_lab.user_prompt("D", fname, fact)
    if not earlier_problems:
        return base
    notes = "\n".join(f"- {p}" for p in earlier_problems)
    return base + ("\n\nA checker read earlier drafts of this entry and found these problems. Write a fresh entry, not a patch of an old one, "
                   "and make sure none of these happen:\n" + notes)


def rank(check):
    # fewest problems first, then fewest warnings, then closest to the target length
    return (len(check["problems"]), len(check["warnings"]), abs(check["characters"] - check["target"]))


def draft_file(fname, fact, attempts, system, banned):
    src = (run_lab.SRC / fname).read_text()
    tried, carried = [], []
    for n in range(1, attempts + 1):
        r = lab.ask([{"role": "system", "content": system}, {"role": "user", "content": prompt_for(fname, fact, carried)}])
        entry = run_lab.extract_entry(r["text"])
        check = check_entry(entry, banned=banned)
        copied = run_lab.score(entry, src)["longest_copied_run_words"]
        rec = {"attempt": n, "entry": entry, "check": check, "longest_copied_run_words": copied, "seconds": r["seconds"],
               "error": r.get("error"), "notes_and_reply": r["text"], "thinking": r.get("thinking", "")}
        tried.append(rec)
        print(time.strftime("%H:%M:%S"), fname, f"attempt {n}", f"{check['characters']} chars", f"problems={len(check['problems'])}",
              f"warnings={len(check['warnings'])}", f"copied_run={copied}", flush=True)
        if not check["problems"] and copied < 6:
            break
        carried = list(dict.fromkeys(carried + check["problems"] + ([f"A run of {copied} words was copied from the story; use your own words."] if copied >= 6 else [])))
    best = min(tried, key=lambda t: (rank(t["check"]), t["longest_copied_run_words"]))
    return best, tried


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stems", nargs="+")
    ap.add_argument("--facts")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--source", default="medium-mara")
    args = ap.parse_args()
    lab.SOURCE[0] = args.source
    facts = json.loads(Path(args.facts).read_text()) if args.facts else {}
    OUT.mkdir(parents=True, exist_ok=True)
    banned = banned_phrases()
    system = run_lab.sys_prompt()
    lab.create_model()
    import requests
    url, key = lab.conn(); requests.get(f"{url}/api/models", headers=lab.H(key), timeout=60)
    failures = 0
    try:
        for stem in args.stems:
            fname = stem if stem.endswith(".md") else stem + ".md"
            fact = facts.get(fname) or dict((v[0], v[1]) for v in run_lab.FILES.values()).get(fname, "")
            try:
                best, tried = draft_file(fname, fact, args.attempts, system, banned)
            except Exception as e:  # noqa
                failures += 1
                print("FAIL", fname, type(e).__name__, e, flush=True)
                if failures >= 2:
                    print("STOPPING: two failed files in a row", flush=True)
                    return
                continue
            failures = 0
            (OUT / (fname[:-3] + ".json")).write_text(json.dumps({"file": fname, "fact_given": fact, "best": best, "attempts": tried}, indent=1))
            ok = "PASSES the checker" if not best["check"]["problems"] else "STILL HAS PROBLEMS: " + "; ".join(best["check"]["problems"])
            with (OUT / "candidates.md").open("a") as f:
                f.write(f"## {fname}\n{best['entry']}\n\n- {best['check']['characters']} characters, {ok}\n"
                        f"- warnings: {'; '.join(best['check']['warnings']) or 'none'}\n- longest copied run: {best['longest_copied_run_words']} words; attempts used: {len(tried)}\n\n")
        print("ALL DONE", flush=True)
    finally:
        print("delete model:", lab.delete_model(), flush=True)


if __name__ == "__main__":
    main()
