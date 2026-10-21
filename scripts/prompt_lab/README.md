# Prompt lab (Mara memory entries)

Run a prompt against a TOOL-FREE copy of `medium-mara` (or `mara-duo`) and score the result, without touching her notes, mail or files.

- `lab.py` creates a temporary preset `zz-memory-lab` (same base model and parameters as the source preset, no tools, no system prompt of its own),
  runs turns on throwaway chats the way `scripts/rating_pass.py` does, and deletes both. It reads the local server's URL and key from `secrets.md`.
  Always call `lab.delete_model()` in a `finally`; an interrupted run can leave the preset and a `[prompt lab]` chat behind (delete them in the UI or via the API).
- `run_lab.py <variants> <files> <reps> [source-preset]`, e.g. `python3 run_lab.py D sensory,weather,photo 1 medium-mara`. Variants: A current guidance,
  B meaning-first template, C two-step (notes, then a draft with the source hidden), D = B + rules against numbers, stock images and labels
  (`guidance_d.md`, now the live guidance). Results go to `results_<preset>.jsonl` (they hold Mara's entries: keep them out of git).
- The scoring in `run_lab.py` (length, digits, longest run of words copied from the source) misses spelled-out numbers and style; `src/memory_entry_check.py` is the better scorer.
- Needs `/usr/bin/python3` with `requests`; the system Python, not the venv without pydantic.
