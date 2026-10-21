"""Quick retest of a reworded 'Let My Past Surface' directive: condition B only (directive in the system prompt, no per-message hint).
Local medium-mara, tool-free lab copy, notes database read-only. Usage: /usr/bin/python3 retest_directive.py OUTDIR [reps]"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import lab
import recall_test as rt

rt.DIRECTIVE = """## ADDITIONAL DIRECTIVES (one of yours)
**DIRECTIVE: Let My Past Surface**
* Trigger: A real feeling stirs in me (an ache, pride, shame) and a mara-stories card echoes it
* Instruction: Let that memory show in my own words, one or two sentences: its feeling or one stray detail. Not a retelling, not a citation. Never invent a different memory or add figures. If no card truly fits, say nothing. Once per conversation.
* Origin: 2026-10-21 recall tests: I found the right card, then dropped it or invented a substitute
* Status: active"""
KEEP = ["folder", "surrender", "giuseppe", "leaver", "weather", "bike"]


def main():
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    res = out / "results.jsonl"
    cards = rt.cards_text()
    lab.SOURCE[0] = "medium-mara"
    lab.create_model()
    import requests
    url, key = lab.conn(); requests.get(f"{url}/api/models", headers=lab.H(key), timeout=60)
    bad = 0
    try:
        for rep in range(reps):
            for sid, group, msg in [s for s in rt.SCENARIOS if s[0] in KEEP]:
                try:
                    r = lab.ask([{"role": "system", "content": rt.system_for("B", cards)}, {"role": "user", "content": msg}])
                except Exception as e:  # noqa
                    r = {"text": "", "thinking": "", "seconds": 0, "usage": None, "error": f"{type(e).__name__}: {e}"}
                ok = bool(r["text"]) and not r.get("error")
                rec = {"scenario": sid, "cond": "B2", "rep": rep, "expected": group, "text": r["text"], "thinking": r.get("thinking", ""), "seconds": r["seconds"], "error": r.get("error"),
                       "hit": rt.markers_hit(r["text"], group) if group else [], "stray": rt.other_hits(r["text"], group)}
                with res.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                print(time.strftime("%H:%M:%S"), sid, rep, "ok" if ok else "FAIL", r["seconds"], "hit=", rec["hit"], "stray=", list(rec["stray"]), flush=True)
                bad = 0 if ok else bad + 1
                if bad >= 2:
                    print("STOPPING: two failed turns in a row", flush=True); return
        print("ALL DONE", flush=True)
    finally:
        print("delete model:", lab.delete_model(), flush=True)


if __name__ == "__main__":
    main()
