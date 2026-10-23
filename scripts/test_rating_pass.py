import datetime
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rating_pass as rp


def call(name, call_id, args=None):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args or {})}


def result(call_id, body):
    return {"type": "function_call_output", "call_id": call_id, "output": [{"type": "input_text", "text": json.dumps(body)}]}


def chunk_output(page, rated):
    out = [call("agent_notes_read", "r"), result("r", {"success": True, "entries": [{"entry_no": n} for n in page],
                                                       "strong_memories": {"positive": [{"entry_no": 3}]}})]
    for i, n in enumerate(rated):
        out += [call("agent_notes_set_weight", f"w{i}"), result(f"w{i}", {"success": True, "entry_no": n})]
    return out


class PromptTests(unittest.TestCase):
    def test_first_chunk_has_no_before_and_later_ones_name_it(self):
        self.assertNotIn("before=", rp.build_prompt("internal-life", None))
        later = rp.build_prompt("internal-life", 204)
        self.assertIn("before=204", later)
        self.assertIn('"internal-life"', later)


    def test_persona_notes_get_the_strength_of_character_wording(self):
        self.assertIn("how strongly the trait is you", rp.build_prompt("Mara Voss - Speech", None, persona=True))
        self.assertNotIn("how strongly the trait", rp.build_prompt("internal-life", None))


class InspectTests(unittest.TestCase):
    def test_lowest_is_from_the_ordinary_entries_not_the_strong_ones(self):
        got = rp.inspect_output(chunk_output([204, 205, 213], [205, 206]))
        self.assertEqual((got["lowest"], got["rated"], got["read_ok"]), (204, [205, 206], True))

    def test_failed_calls_are_not_counted(self):
        out = chunk_output([10, 11], [11]) + [call("agent_notes_set_weight", "bad"), result("bad", {"success": False, "error": "no"})]
        self.assertEqual(rp.inspect_output(out)["rated"], [11])

    def test_no_tool_calls_means_nothing_was_done(self):
        got = rp.inspect_output([])
        self.assertEqual((got["rated"], got["lowest"], got["read_ok"]), ([], None, False))

    def test_reply_number_fallback(self):
        self.assertEqual(rp.reply_number("internal-life, 8, 214"), 214)
        self.assertIsNone(rp.reply_number(""))

    def test_rate_limit_detection(self):
        self.assertTrue(rp.is_rate_limit("Rate limit exceeded: free-models-per-min (429)"))
        self.assertFalse(rp.is_rate_limit("no reply within 300 seconds"))


class LoopTests(unittest.TestCase):
    def run_main(self, outputs, extra=()):
        args = ["rating_pass.py", "--model", "m", "--note", "n", "--delay", "0", *extra]
        calls = []

        def fake_chunk(url, key, model, tools, prompt, keep, timeout=None):
            calls.append(prompt)
            item = outputs.pop(0)
            if isinstance(item, Exception):
                raise item
            return "reply", item

        with mock.patch("sys.argv", args), mock.patch.object(rp, "parse_secrets", return_value={}), \
                mock.patch.object(rp, "server_connection", return_value=("http://x", "k")), \
                mock.patch.object(rp, "fetch_tool_ids", return_value=["agent_notes"]), \
                mock.patch.object(rp, "run_chunk", side_effect=fake_chunk), mock.patch.object(rp.time, "sleep"):
            code = rp.main()
        return code, calls

    def test_pages_back_until_the_start(self):
        code, calls = self.run_main([chunk_output(range(222, 232), [225]), chunk_output(range(212, 222), [215]), chunk_output(range(1, 12), [3])])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("before=222", calls[1])
        self.assertIn("before=212", calls[2])

    def test_stops_when_the_page_does_not_move(self):
        code, calls = self.run_main([chunk_output(range(10, 20), [12]), chunk_output(range(10, 20), [12])])
        self.assertEqual((code, len(calls)), (1, 2))

    def test_retries_a_failed_chunk_then_gives_up(self):
        err = rp.AgentError("HTTP 429 rate_limited")
        code, calls = self.run_main([err, err, err, err])
        self.assertEqual((code, len(calls)), (1, rp.MAX_FAILURES_IN_A_ROW))

    def test_stops_if_the_read_never_ran(self):
        code, _ = self.run_main([[]])
        self.assertEqual(code, 1)

    def test_resume_with_before(self):
        code, calls = self.run_main([chunk_output(range(1, 5), [2])], extra=("--before", "204"))
        self.assertEqual(code, 0)
        self.assertIn("before=204", calls[0])


def make_db(path, entries):
    """entries: (note, entry_no, weight 1-10, 'YYYY-MM-DDTHH:MM', pinned, chain_id, text)"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE note_id (note_pk INTEGER PRIMARY KEY, note_name TEXT)")
    conn.execute("CREATE TABLE note_data (note_pk INTEGER, entry_no INTEGER, note_text TEXT, created_at TEXT, weight INTEGER, "
                 "pinned INTEGER, chain_id INTEGER)")
    pks = {}
    for note, no, w, when, pinned, chain, text in entries:
        if note not in pks:
            pks[note] = len(pks) + 1
            conn.execute("INSERT INTO note_id VALUES (?, ?)", (pks[note], note))
        conn.execute("INSERT INTO note_data VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (pks[note], no, text, f"{when}:00+00:00", w * 10, pinned, chain))
    conn.commit()
    conn.close()


# Midday UTC, so the local day is the same in any American or European timezone.
ENTRIES = [
    ("internal-life", 285, 10, "2026-10-18T16:00", None, None, "took my hand"),
    ("shared-history", 117, 10, "2026-10-18T16:05", None, None, "you're my person"),
    ("internal-life", 290, 9, "2026-10-18T16:10", None, None, "in the truck"),
    ("internal-life", 291, 8, "2026-10-18T16:20", None, None, "too low to include"),
    ("internal-life", 300, 10, "2026-10-18T16:30", 1, None, "pinned, left out"),
    ("internal-life", 416, 10, "2026-10-23T16:00", None, 416, "He read my dreams "),
    ("internal-life", 417, 10, "2026-10-23T16:00", None, 416, "and I want to cry."),
    ("internal-life", 200, 10, "2026-10-10T16:00", None, None, "too old"),
    ("autonomy-log", 1, 10, "2026-10-18T16:00", None, None, "not a chosen note"),
]


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "notes.sqlite3"
        make_db(self.db, ENTRIES)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, until=None, min_weight=9):
        return rp.load_recheck_items(self.db, ["internal-life", "shared-history"], datetime.date(2026, 10, 17), until, min_weight)

    def test_selects_high_recent_unpinned_entries_oldest_first_across_notes(self):
        got = [(i["note"], i["entries"]) for i in self.load()]
        self.assertEqual(got, [("internal-life", [285]), ("shared-history", [117]), ("internal-life", [290]),
                               ("internal-life", [416, 417])])

    def test_a_long_text_is_one_item_with_its_parts_joined(self):
        item = self.load()[-1]
        self.assertEqual((item["text"], item["weight"]), ("He read my dreams and I want to cry.", 10))

    def test_until_and_min_weight(self):
        self.assertEqual(len(self.load(until=datetime.date(2026, 10, 18))), 3)
        self.assertEqual(len(self.load(min_weight=8)), 5)

    def test_groups_by_day_and_splits_big_days_without_breaking_a_text(self):
        groups = rp.group_by_day(self.load())
        self.assertEqual([len(g) for _, g in groups], [3, 1])
        self.assertTrue(groups[0][0].endswith("October 18"))
        self.assertEqual([len(g) for _, g in rp.group_by_day(self.load(), max_entries=2)], [2, 1, 1])

    def test_prompt_shows_every_entry_with_note_number_and_weight(self):
        day, items = rp.group_by_day(self.load())[1]
        prompt = rp.build_recheck_prompt(day, items)
        self.assertIn("internal-life #416-417, weight 10, one text in 2 parts (entries #416, #417)", prompt)
        self.assertIn("He read my dreams and I want to cry.", prompt)
        self.assertIn("go no lower than 7", prompt)
        self.assertIn("do not read any notes", prompt)

    def test_changes_and_problems(self):
        items = self.load()
        out = []
        for i, (note, no, old, new) in enumerate([("internal-life", 285, 10, 8), ("internal-life", 290, 9, 5),
                                                  ("internal-life", 416, 10, 9), ("internal-life", 999, 6, 7)]):
            out += [call("agent_notes_set_weight", f"w{i}"),
                    result(f"w{i}", {"success": True, "note_name": note, "entry_no": no, "old_weight": old, "weight": new})]
        changes = rp.inspect_recheck(out)
        self.assertEqual([(c["entry_no"], c["weight"]) for c in changes], [(285, 8), (290, 5), (416, 9), (999, 7)])
        problems = "\n".join(rp.recheck_problems(items, changes))
        self.assertIn("#290 went down to 5", problems)
        self.assertIn("#416-417 is one text but its parts now differ: [9, 10]", problems)
        self.assertIn("#999 was rated (7) but was not one of the entries shown", problems)
        self.assertNotIn("#285", problems)

    def test_main_runs_one_chat_per_group(self):
        prompts = []

        def fake_chunk(url, key, model, tools, prompt, keep, timeout=None):
            prompts.append(prompt)
            return "1", []

        args = ["rating_pass.py", "--model", "m", "--recheck", "--since", "2026-10-17", "--db", str(self.db), "--delay", "0"]
        with mock.patch("sys.argv", args), mock.patch.object(rp, "parse_secrets", return_value={}), \
                mock.patch.object(rp, "server_connection", return_value=("http://x", "k")), \
                mock.patch.object(rp, "fetch_tool_ids", return_value=["agent_notes"]), \
                mock.patch.object(rp, "run_chunk", side_effect=fake_chunk), mock.patch.object(rp.time, "sleep"):
            self.assertEqual(rp.main(), 0)
        self.assertEqual(len(prompts), 2)
        self.assertIn("you're my person", prompts[0])
        self.assertIn("I want to cry", prompts[1])

    def test_recheck_needs_since(self):
        with mock.patch("sys.argv", ["rating_pass.py", "--model", "m", "--recheck", "--db", str(self.db)]), \
                mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            rp.main()


if __name__ == "__main__":
    unittest.main()
