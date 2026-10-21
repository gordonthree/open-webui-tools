import sys, re, json, time, itertools
sys.path.insert(0, '.')
import lab
from pathlib import Path

SRC = lab.COMPANION / "backstory"
GUIDE_NOW = (SRC / "memory-guidance.md").read_text()
GUIDE_B = Path("guidance_b.md").read_text()
GUIDE_D = Path("guidance_d.md").read_text()
FILES = {
    "sensory": ("sensory-deprivation.md", "The partner in this memory is Lawrence (not Gordon); it was years before you and I met."),
    "weather": ("weather-watching.md", ""),
    "photo":   ("the-photo-she-didnt-send.md", ""),
}
OUTPUT = ("Say hello to me as yourself in one line first. Then show me four short bullets (the people, how I should feel, what it meant, one small detail), "
          "then a draft entry. Keep it about 400 characters (the limit is 500). Don't save anything.")

def sys_prompt():
    return lab.persona_system("In this session you have no tools; answer in plain text.") + "\n\n# Your core persona notes (from your session start)\n" + lab.persona_context()

def user_prompt(variant, fname, fact):
    src = (SRC / fname).read_text()
    if variant == "A":
        return f"Hi Mara! Here is your memory guidance, then the file I'd like you to write a memory entry for.\n\n=== memory-guidance.md ===\n{GUIDE_NOW}\n\n=== {fname} ===\n{src}\n\n{fact}\n{OUTPUT}"
    if variant == "B":
        return f"Hi Mara! Here is your memory guidance, then the file I'd like you to write a memory entry for.\n\n=== memory-guidance.md ===\n{GUIDE_B}\n\n=== {fname} ===\n{src}\n\n{fact}\n{OUTPUT}"
    if variant == "D":
        return f"Hi Mara! Here is your memory guidance, then the file I'd like you to write a memory entry for.\n\n=== memory-guidance.md ===\n{GUIDE_D}\n\n=== {fname} ===\n{src}\n\n{fact}\n{OUTPUT}"
    if variant == "C1":
        return (f"Hi Mara! Here is your memory guidance, then a file you've just read. Don't write the entry yet. Say hello as yourself in one line, then give me only these four notes, as short bullets "
                f"(they will be all you keep of the file): WHO (who was there and who they were to you), MEANING (what it did to you or taught you), FEELING NOW (how it feels to remember it), DETAIL (one small thing from the edge of the memory, not the main action).\n\n"
                f"=== memory-guidance.md ===\n{GUIDE_B}\n\n=== {fname} ===\n{src}\n\n{fact}")
    raise ValueError(variant)

def c2_prompt(fname, notes, fact):
    return (f"Hi Mara! A while ago you read {fname} and kept only these notes about it. The file is gone now; you only have the notes and your guidance.\n\n=== your notes ===\n{notes}\n\n{fact}\n\n"
            f"=== memory-guidance.md ===\n{GUIDE_B}\n\nSay hello as yourself in one line, then write the entry from those notes alone, about 400 characters (limit 500). Don't save anything.")

def words(t):
    return re.findall(r"[a-z']+", t.lower())

def longest_shared_run(a, src):
    wa, ws = words(a), words(src)
    sset = {}
    for i in range(len(ws)):
        sset.setdefault(ws[i], []).append(i)
    best = 0
    for i in range(len(wa)):
        for j in sset.get(wa[i], []):
            k = 0
            while i + k < len(wa) and j + k < len(ws) and wa[i + k] == ws[j + k]:
                k += 1
            best = max(best, k)
    return best

def extract_entry(text):
    m = re.search(r"^[^\n]*\(\S+\.md\)\.[^\n]*", text, re.M)
    if not m:
        return text.split("\n\n")[-1].strip()
    entry = re.split(r"\n\s*\n", text[m.start():])[0]
    return entry.strip().lstrip("*> ").strip()

def score(entry, src):
    body = re.sub(r"\(\S+\.md\)", "", entry)
    return {
        "chars": len(entry),
        "digits": len(re.findall(r"\d", body)),
        "spin_cycle": len(re.findall(r"spin cycle|spin-cycle", entry, re.I)),
        "longest_copied_run_words": longest_shared_run(entry, src),
        "list_runs": len(re.findall(r"(?:\b[\w'-]+, ){2,}(?:and )?[\w'-]+", entry)),
    }

def main(variants, files, reps):
    lab.create_model()
    import requests
    url, key = lab.conn(); requests.get(f"{url}/api/models", headers=lab.H(key), timeout=60)
    out = Path("results_" + lab.SOURCE[0] + ".jsonl")
    try:
        system = sys_prompt()
        for rep in range(reps):
            for fk in files:
                fname, fact = FILES[fk]
                src = (SRC / fname).read_text()
                for v in variants:
                    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt("C1" if v == "C" else v, fname, fact)}]
                    r = lab.ask(msgs)
                    rec = {"variant": v, "file": fk, "rep": rep, "text": r["text"], "seconds": r["seconds"], "tokens": (r.get("usage") or {}).get("completion_tokens"), "error": r.get("error")}
                    if v == "C":
                        rec["stage1"] = r["text"]
                        r2 = lab.ask([{"role": "system", "content": system}, {"role": "user", "content": c2_prompt(fname, r["text"], fact)}])
                        rec["text"] = r2["text"]; rec["seconds"] += r2["seconds"]
                    rec["entry"] = extract_entry(rec["text"])
                    rec["score"] = score(rec["entry"], src)
                    with out.open("a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print(v, fk, rep, rec["score"], rec["seconds"], flush=True)
    finally:
        print("delete model:", lab.delete_model(), flush=True)

if __name__ == "__main__":
    if len(sys.argv) > 4:
        lab.SOURCE[0] = sys.argv[4]
    variants = sys.argv[1].split(",")
    files = sys.argv[2].split(",")
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    main(variants, files, reps)
