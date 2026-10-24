"""
Tests for agent_notes.py. Run with: python -m unittest test_agent_notes -v
(no Open WebUI server needed - it's just SQLite in a temp directory).
"""

import asyncio
import json
from collections import Counter as collections_counter
import shutil
import sqlite3
import tempfile
import typing
import unittest
from typing import Optional
from pathlib import Path

import agent_notes_full as mod  # the single source; the two deployable tools are generated from it (see test_agent_notes_split)


class NotesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "nested" / "agent_notes.sqlite3")  # parent dir doesn't exist yet
        self.tool = mod.Tools()
        self.tool.valves.NOTES_DB_PATH = self.db
        self.tool.valves.MIN_SUMMARY_CHARS = 0  # tests use tiny notes; the short-note rule has its own tests
        self.tool.valves.STRONG_SPREAD = False  # strong memories are a fixed top-N unless a test asks for the random draw
        self.tool.valves.VERBOSE_FIELDS = "author,created_at,hits,weight"  # verbose = everything here; VerboseFieldsTests covers the default
        self.tool.valves.RESULT_FORMAT = "pretty"  # replies stay dicts here; ResultFormatTests covers the other formats
        mod._overlong.clear()  # too-long retry counts are kept module-wide

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_async(self, coro):
        return asyncio.run(coro)

    def sql(self, query, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def create(self, name="ideas", text="first", comment=""):
        return self.run_async(self.tool.agent_notes_create(name, text, comment))


class CreateAndReadTests(NotesTestCase):
    def test_create_then_read_round_trips(self):
        res = self.create("Character Bios", "Ada is a cartographer.", "who's who")
        self.assertTrue(res["success"])
        self.assertEqual(res["entry_no"], 1)
        read = self.run_async(self.tool.agent_notes_read("character bios"))  # case-insensitive lookup
        self.assertTrue(read["success"])
        self.assertEqual(read["note_name"], "Character Bios")  # stored spelling is preserved
        self.assertEqual(read["note_comment"], "who's who")
        self.assertEqual([e["text"] for e in read["entries"]], ["Ada is a cartographer."])

    def test_creates_database_file_and_parent_folder(self):
        self.create()
        self.assertTrue(Path(self.db).exists())

    def test_duplicate_name_is_refused_ignoring_case_and_points_at_append(self):
        self.create("ideas")
        res = self.create("IDEAS", "again")
        self.assertFalse(res["success"])
        self.assertIn("agent_notes_append", res["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_id"), [(1,)])

    def test_name_whitespace_is_normalized(self):
        self.create("  my   note \n")
        read = self.run_async(self.tool.agent_notes_read("my note"))
        self.assertEqual(read["note_name"], "my note")

    def test_blank_comment_sentinels_mean_no_comment(self):
        for sentinel in ("", None, "none"):
            with self.subTest(sentinel=sentinel):
                self.create(f"n-{sentinel}", "x", sentinel)
        self.assertEqual({r[0] for r in self.sql("SELECT note_comment FROM note_id")}, {""})

    def test_read_missing_note_suggests_close_names(self):
        self.create("character-bios")
        res = self.run_async(self.tool.agent_notes_read("character-bio"))
        self.assertFalse(res["success"])
        self.assertIn("character-bios", res["error"])

    def test_read_missing_note_when_none_exist(self):
        res = self.run_async(self.tool.agent_notes_read("anything"))
        self.assertFalse(res["success"])
        self.assertIn("agent_notes_create", res["error"])


class AppendTests(NotesTestCase):
    def test_append_adds_numbered_entries_in_order(self):
        self.create("log", "one")
        r2 = self.run_async(self.tool.agent_notes_append("log", "two"))
        r3 = self.run_async(self.tool.agent_notes_append("LOG", "three"))
        self.assertEqual((r2["entry_no"], r3["entry_no"]), (2, 3))
        read = self.run_async(self.tool.agent_notes_read("log"))
        self.assertEqual([(e["entry_no"], e["text"]) for e in read["entries"]], [(1, "one"), (2, "two"), (3, "three")])
        self.assertEqual(read["total_entries"], 3)

    def test_append_to_missing_note_fails_without_creating_it(self):
        res = self.run_async(self.tool.agent_notes_append("ghost", "boo"))
        self.assertFalse(res["success"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_id"), [(0,)])

    def test_read_limit_returns_most_recent_in_chronological_order(self):
        self.create("log", "e1")
        for i in range(2, 6):
            self.run_async(self.tool.agent_notes_append("log", f"e{i}"))
        read = self.run_async(self.tool.agent_notes_read("log", 2))
        self.assertEqual([e["text"] for e in read["entries"]], ["e4", "e5"])
        self.assertEqual(read["total_entries"], 5)
        self.assertIn("Showing 2 recent of 5", read["note"])

    def test_read_limit_is_capped_by_valve(self):
        self.tool.valves.MAX_READ_ENTRIES = 2
        self.create("log", "e1")
        for i in range(2, 5):
            self.run_async(self.tool.agent_notes_append("log", f"e{i}"))
        read = self.run_async(self.tool.agent_notes_read("log", 99))
        self.assertEqual(len(read["entries"]), 2)


class EditAndDeleteEntryTests(NotesTestCase):
    def setUp(self):
        super().setUp()
        self.create("log", "one")
        self.run_async(self.tool.agent_notes_append("log", "two"))
        self.run_async(self.tool.agent_notes_append("log", "three"))

    def test_edit_replaces_text_keeps_number_and_marks_edited(self):
        res = self.run_async(self.tool.agent_notes_edit_entry("log", 2, "TWO"))
        self.assertTrue(res["success"])
        read = self.run_async(self.tool.agent_notes_read("log", verbose=True))
        by_no = {e["entry_no"]: e for e in read["entries"]}
        self.assertEqual(by_no[2]["text"], "TWO")
        self.assertIn("edited_at", by_no[2])
        self.assertNotIn("edited_at", by_no[1])
        self.assertEqual([e["entry_no"] for e in read["entries"]], [1, 2, 3])

    def test_entry_no_accepts_hash_and_string_forms(self):
        self.assertTrue(self.run_async(self.tool.agent_notes_edit_entry("log", "#2", "a"))["success"])
        self.assertTrue(self.run_async(self.tool.agent_notes_edit_entry("log", "3", "b"))["success"])

    def test_edit_missing_entry_lists_what_exists(self):
        res = self.run_async(self.tool.agent_notes_edit_entry("log", 9, "x"))
        self.assertFalse(res["success"])
        self.assertIn("1-3", res["error"])

    def test_entry_no_required_and_numeric(self):
        self.assertFalse(self.run_async(self.tool.agent_notes_edit_entry("log", "", "x"))["success"])
        self.assertFalse(self.run_async(self.tool.agent_notes_delete_entry("log", "abc"))["success"])

    def test_delete_entry_removes_only_that_row_and_numbers_are_not_reused(self):
        res = self.run_async(self.tool.agent_notes_delete_entry("log", 3))
        self.assertEqual(res["remaining"], 2)
        appended = self.run_async(self.tool.agent_notes_append("log", "four"))
        self.assertEqual(appended["entry_no"], 4)  # not 3 again
        read = self.run_async(self.tool.agent_notes_read("log"))
        self.assertEqual([e["entry_no"] for e in read["entries"]], [1, 2, 4])

    def test_editing_bumps_note_to_top_of_list(self):
        self.create("other", "x")
        self.run_async(self.tool.agent_notes_edit_entry("log", 1, "touched"))
        table = self.run_async(self.tool.agent_notes_list())["table"].splitlines()
        self.assertTrue(table[2].startswith("| log |"))


class UpdateNoteTests(NotesTestCase):
    def test_rename_and_comment(self):
        self.create("old", "x", "c1")
        res = self.run_async(self.tool.agent_notes_update("old", "new", "c2"))
        self.assertTrue(res["success"])
        read = self.run_async(self.tool.agent_notes_read("new"))
        self.assertEqual(read["note_comment"], "c2")
        self.assertFalse(self.run_async(self.tool.agent_notes_read("old"))["success"])
        self.assertEqual(len(read["entries"]), 1)  # entries came along

    def test_rename_onto_another_note_is_refused(self):
        self.create("a", "x")
        self.create("b", "y")
        res = self.run_async(self.tool.agent_notes_update("a", "B"))
        self.assertFalse(res["success"])

    def test_case_only_rename_of_own_note_is_allowed(self):
        self.create("notes", "x")
        self.assertTrue(self.run_async(self.tool.agent_notes_update("notes", "Notes"))["success"])
        self.assertEqual(self.run_async(self.tool.agent_notes_read("notes"))["note_name"], "Notes")

    def test_comment_only_update_keeps_name(self):
        self.create("a", "x", "old")
        self.run_async(self.tool.agent_notes_update("a", "", "new"))
        read = self.run_async(self.tool.agent_notes_read("a"))
        self.assertEqual((read["note_name"], read["note_comment"]), ("a", "new"))

    def test_clear_comment(self):
        self.create("a", "x", "old")
        self.run_async(self.tool.agent_notes_update("a", "", "", True))
        self.assertEqual(self.run_async(self.tool.agent_notes_read("a"))["note_comment"], "")

    def test_nothing_to_update_and_conflicting_args(self):
        self.create("a", "x")
        self.assertFalse(self.run_async(self.tool.agent_notes_update("a", "", "", False))["success"])
        self.assertFalse(self.run_async(self.tool.agent_notes_update("a", "", "c", True))["success"])


class DeleteNoteTests(NotesTestCase):
    def test_delete_note_removes_note_and_all_entries(self):
        self.create("doomed", "one")
        self.run_async(self.tool.agent_notes_append("doomed", "two"))
        self.create("keeper", "safe")
        res = self.run_async(self.tool.agent_notes_delete("DOOMED"))
        self.assertEqual(res["entries_removed"], 2)
        self.assertEqual(self.sql("SELECT note_name FROM note_id"), [("keeper",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data"), [(1,)])  # no orphaned rows

    def test_name_is_reusable_after_delete(self):
        self.create("again", "one")
        self.run_async(self.tool.agent_notes_delete("again"))
        self.assertTrue(self.create("again", "fresh")["success"])
        read = self.run_async(self.tool.agent_notes_read("again"))
        self.assertEqual([(e["entry_no"], e["text"]) for e in read["entries"]], [(1, "fresh")])

    def test_delete_missing_note_fails(self):
        self.assertFalse(self.run_async(self.tool.agent_notes_delete("nope"))["success"])


class LimitTests(NotesTestCase):
    def test_name_limit_is_60(self):
        self.assertTrue(self.create("n" * 60)["success"])
        res = self.create("n" * 61)
        self.assertFalse(res["success"])
        self.assertIn("61", res["error"])

    def test_comment_limit_is_500(self):
        self.assertTrue(self.create("a", "x", "c" * 500)["success"])
        self.assertFalse(self.create("b", "x", "c" * 501)["success"])

    def test_text_limit_is_500_for_create_append_and_edit(self):
        self.assertTrue(self.create("a", "t" * 500)["success"])
        self.assertFalse(self.create("b", "t" * 501)["success"])
        self.assertFalse(self.run_async(self.tool.agent_notes_append("a", "t" * 501))["success"])
        self.assertFalse(self.run_async(self.tool.agent_notes_edit_entry("a", 1, "t" * 501))["success"])
        self.assertIn("continues", self.run_async(self.tool.agent_notes_append("a", "t" * 501))["error"])

    def test_empty_text_is_refused(self):
        self.assertFalse(self.create("a", "   ")["success"])

    def test_limits_are_also_enforced_by_the_database_itself(self):
        self.create("a", "x")
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO note_data (note_pk, entry_no, note_text, created_at) VALUES (1, 99, ?, 'now')", ("t" * 501,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO note_id (note_name, created_at, updated_at) VALUES (?, 'now', 'now')", ("n" * 61,))
        finally:
            conn.close()

    def test_length_counts_characters_not_bytes(self):
        self.assertTrue(self.create("a", "é" * 500)["success"])  # 1000 bytes, 500 characters


class ListAndSearchTests(NotesTestCase):
    def test_list_empty(self):
        res = self.run_async(self.tool.agent_notes_list())
        self.assertEqual(res["count"], 0)
        self.assertIn("agent_notes_create", res["table"])

    def test_list_shows_entry_counts_and_comments_most_recent_first(self):
        self.create("first", "x", "the first")
        self.create("second", "y")
        self.run_async(self.tool.agent_notes_append("first", "z"))
        res = self.run_async(self.tool.agent_notes_list(""))  # "" means default
        rows = res["table"].splitlines()[2:]
        self.assertTrue(rows[0].startswith("| first | 2 |"))
        self.assertIn("the first", rows[0])
        self.assertTrue(rows[1].startswith("| second | 1 |"))

    def test_list_limit_reports_truncation(self):
        for i in range(3):
            self.create(f"n{i}", "x")
        res = self.run_async(self.tool.agent_notes_list(2))
        self.assertEqual((res["count"], res["total_notes"]), (2, 3))
        self.assertIn("note", res)

    def test_pipe_in_name_does_not_break_table(self):
        self.create("a|b", "x")
        row = self.run_async(self.tool.agent_notes_list())["table"].splitlines()[2]
        self.assertIn("a\\|b", row)

    def test_search_finds_entries_names_and_comments_case_insensitively(self):
        self.create("dragons", "Wyrmling hatched at dawn", "lore")
        self.create("misc", "buy milk", "dragon-adjacent")
        res = self.run_async(self.tool.agent_notes_search("DRAGON"))
        self.assertEqual({n["note_name"] for n in res["matching_notes"]}, {"dragons", "misc"})
        res = self.run_async(self.tool.agent_notes_search("wyrmling"))
        self.assertEqual([(e["note_name"], e["entry_no"]) for e in res["cards"]], [("dragons", 1)])

    def test_search_cards_put_the_strongest_feelings_first_then_the_newest(self):
        self.create("a", "gordon one")
        for text in ("gordon two", "gordon three", "gordon four"):
            self.run_async(self.tool.agent_notes_append("a", text))
        for no, w in (("2", "9"), ("3", "1")):
            self.run_async(self.tool.agent_notes_set_weight("a", no, weight=w))
        out = self.run_async(self.tool.agent_notes_search("gordon"))
        self.assertEqual([c["entry_no"] for c in out["cards"]], [3, 2, 4, 1])  # 1 and 9 are equally strong, the newer first; then neutral, newest first
        self.assertEqual([c["weight"] for c in out["cards"]], [1, 9, 5, 5])
        self.assertEqual(set(out["cards"][0]), {"note_name", "entry_no", "weight", "date", "snippet"})
        self.assertEqual(out["total_matching_entries"], 4)

    def test_continues_splits_long_text_verbatim_into_a_linked_chain(self):
        text = " ".join(f"word{i}" for i in range(300))  # ~2000 characters
        made = self.run_async(self.tool.agent_notes_create("long", text, continues=True))
        self.assertTrue(made["success"], made)
        self.assertEqual(made["entry_numbers"], "1-5")
        more = self.run_async(self.tool.agent_notes_append("long", "tail " * 150, continues=True))
        self.assertEqual(more["entry_numbers"], "6-7")
        entries = self.run_async(self.tool.agent_notes_read("long"))["entries"]
        self.assertTrue(all(len(e["text"]) <= mod.NOTE_TEXT_MAX for e in entries))
        self.assertEqual("".join(e["text"] for e in entries[:5]), text)  # the pieces joined end to end are exactly what was given
        self.assertEqual([e.get("continues_at") for e in entries], [2, 3, 4, 5, None, 7, None])
        self.assertEqual([e.get("continues_from") for e in entries], [None, 1, 2, 3, 4, None, 6])
        self.assertEqual(self.sql("SELECT DISTINCT chain_id FROM note_data WHERE entry_no > 5"), [(6,)])

    def test_ordinary_entries_carry_no_chain_fields_and_entry_reads_show_links(self):
        self.create("n", "plain")
        self.run_async(self.tool.agent_notes_append("n", "needle " * 100, continues=True))
        read = self.run_async(self.tool.agent_notes_read("n"))["entries"]
        self.assertEqual(set(read[0]), {"entry_no", "text"})
        fetched = self.run_async(self.tool.agent_notes_read("n", entry="2, 3"))["entries"]  # a card's follow-up read shows the links
        self.assertEqual({h["entry_no"]: (h.get("continues_from"), h.get("continues_at")) for h in fetched}, {2: (None, 3), 3: (2, None)})

    def test_read_tagged_plain_keeps_chain_links_on_pieces_only(self):
        self.create("n", "plain")
        self.run_async(self.tool.agent_notes_append("n", "word " * 150, continues=True))
        self.run_async(self.tool.agent_notes_add_tags("n", "t", create=True))
        out = self.run_async(self.tool.agent_notes_read_tagged("t"))
        entries = out["notes"][0]["entries"]
        self.assertEqual(entries[0], "plain")
        self.assertEqual([e["continues_at"] if isinstance(e, dict) and "continues_at" in e else None for e in entries[1:]], [3, None])
        self.assertEqual(entries[2]["continues_from"], 2)

    # ---- popularity counters and weights ----

    def hits(self, name="n"):
        return self.sql("SELECT hits FROM note_id WHERE note_name = ?", (name,))[0][0], [r[0] for r in self.sql("SELECT d.hits FROM note_data d JOIN note_id n USING (note_pk) WHERE n.note_name = ? ORDER BY entry_no", (name,))]

    def test_read_counts_the_note_and_only_the_entries_returned(self):
        self.create("n", "one")
        for t in ("two", "three"):
            self.run_async(self.tool.agent_notes_append("n", t))
        self.run_async(self.tool.agent_notes_read("n", limit="2"))  # entries 2 and 3
        self.run_async(self.tool.agent_notes_read("n", limit="1"))  # entry 3
        self.assertEqual(self.hits(), (2, [0, 1, 2]))

    def test_search_cards_are_not_reads_but_fetching_an_entry_is(self):
        self.create("n", "needle")
        before = self.sql("SELECT updated_at FROM note_id")
        self.run_async(self.tool.agent_notes_search("needle"))
        self.assertEqual(self.hits(), (0, [0]))
        self.run_async(self.tool.agent_notes_read("n", entry="1"))
        self.assertEqual(self.hits(), (1, [1]))
        self.assertEqual(self.sql("SELECT updated_at FROM note_id"), before)

    def test_read_tagged_counts_what_it_delivered(self):
        self.create("a", "a1")
        self.run_async(self.tool.agent_notes_append("a", "a2"))
        self.run_async(self.tool.agent_notes_add_tags("a", "t", create=True))
        self.run_async(self.tool.agent_notes_read_tagged("t"))
        self.assertEqual(self.hits("a"), (1, [1, 1]))
        self.tool.valves.MAX_TAGGED_CHARS = 500  # budget minus 300 leaves room for the heading but not for long entries
        self.run_async(self.tool.agent_notes_append("a", "x" * 400))
        self.run_async(self.tool.agent_notes_read_tagged("t"))
        note, entries = self.hits("a")
        self.assertEqual(note, 2)
        self.assertEqual(entries[2], 0)  # the 400-character entry did not fit, so it was not delivered

    def test_rank_and_web_style_reads_do_not_count(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_rank_entries("n", "most_popular"))
        import agent_notes_full as m
        m.read_note_db(self.db, "n", 50)  # what notes_web uses
        self.assertEqual(self.hits(), (0, [0]))

    def test_a_failed_count_does_not_spoil_the_read(self):
        self.create("n", "one")
        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        original, mod.record_hits_db = mod.record_hits_db, boom
        try:
            self.assertTrue(self.run_async(self.tool.agent_notes_read("n"))["success"])
        finally:
            mod.record_hits_db = original

    def test_reset_hits_for_an_entry_or_a_whole_note(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_append("n", "two"))
        for _ in range(2):
            self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual(self.run_async(self.tool.agent_notes_reset_hits("n", "#2"))["reset"], "entry")
        self.assertEqual(self.hits(), (2, [2, 0]))
        out = self.run_async(self.tool.agent_notes_reset_hits("N"))
        self.assertEqual((out["reset"], out["entries_reset"]), ("note", 2))
        self.assertEqual(self.hits(), (0, [0, 0]))
        self.assertFalse(self.run_async(self.tool.agent_notes_reset_hits("n", "9"))["success"])
        self.assertFalse(self.run_async(self.tool.agent_notes_reset_hits("missing"))["success"])

    def test_weight_is_a_1_to_10_score_stored_times_ten_and_can_be_set_or_nudged(self):
        self.create("n", "one")
        self.assertEqual(self.sql("SELECT weight FROM note_data")[0][0], 50)  # neutral, as before the 1-10 scale
        self.assertEqual(self.run_async(self.tool.agent_notes_set_weight("n", "1", weight="8"))["weight"], 8)
        self.assertEqual(self.sql("SELECT weight FROM note_data")[0][0], 80)
        out = self.run_async(self.tool.agent_notes_set_weight("n", "#1", change="-3"))
        self.assertEqual((out["old_weight"], out["weight"]), (8, 5))
        out = self.run_async(self.tool.agent_notes_set_weight("n", "1", change="+9"))
        self.assertEqual(out["weight"], 10)
        self.assertIn("note", out)  # said it clamped
        out = self.run_async(self.tool.agent_notes_set_weight("n", "1", change="-9"))
        self.assertEqual(out["weight"], 1)
        out = self.run_async(self.tool.agent_notes_set_weight("n", "1", change="-3"))  # already at the bottom: stays there
        self.assertEqual(out["weight"], 1)
        for bad in ({"weight": "11"}, {"weight": "0"}, {"weight": "-1"}, {}, {"weight": "5", "change": "1"}, {"weight": "high"}, {"change": "-20"}):
            self.assertFalse(self.run_async(self.tool.agent_notes_set_weight("n", "1", **bad))["success"], bad)  # -20 on a 1-10 scale is a misunderstanding, so it is refused
        self.assertFalse(self.run_async(self.tool.agent_notes_set_weight("n", "7", weight="5"))["success"])

    def test_scores_in_between_old_values_are_shown_rounded(self):
        self.create("n", "one")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE note_data SET weight = 65")  # a 0-100 value from before the 1-10 scale
        conn.commit(); conn.close()
        self.assertEqual(self.run_async(self.tool.agent_notes_read("n"))["entries"][0]["weight"], 7)

    def test_weight_is_shown_plain_only_when_not_neutral_and_does_not_make_summaries_stale(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_append("n", "two"))
        self.run_async(self.tool.agent_notes_update_summary("n", "about stuff"))
        self.run_async(self.tool.agent_notes_set_weight("n", "2", weight="2"))
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual([e.get("weight") for e in out["entries"]], [None, 2])
        self.assertFalse(out["summary"]["stale"])
        self.assertEqual(self.run_async(self.tool.agent_notes_read("n", verbose=True))["entries"][0]["weight"], 5)

    def test_edit_keeps_weight_and_hits(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_set_weight("n", "1", weight="2"))
        self.run_async(self.tool.agent_notes_read("n"))
        self.run_async(self.tool.agent_notes_edit_entry("n", "1", "changed"))
        self.assertEqual(self.sql("SELECT weight, hits FROM note_data"), [(20, 1)])

    def make_ranked(self):
        self.create("n", "e1")
        for t in ("e2", "e3", "e4"):
            self.run_async(self.tool.agent_notes_append("n", t))
        for no, w in (("1", 9), ("2", 2), ("3", 6)):  # e4 stays 5
            self.run_async(self.tool.agent_notes_set_weight("n", no, weight=str(w)))
        conn = sqlite3.connect(self.db)
        for no, count in ((1, 3), (3, 1), (4, 1)):  # e2 never read
            conn.execute("UPDATE note_data SET hits = ? WHERE entry_no = ?", (count, no))
        conn.commit(); conn.close()

    def rank(self, **kw):
        out = self.run_async(self.tool.agent_notes_rank_entries("n", **kw))
        self.assertTrue(out["success"], out)
        return [e["entry_no"] for e in out["entries"]], out

    def test_rank_by_popularity_ties_go_to_the_newest(self):
        self.make_ranked()
        self.assertEqual(self.rank(by="most_popular", limit="3")[0], [1, 4, 3])
        self.assertEqual(self.rank(by="least_popular", limit="2")[0], [2, 4])

    def test_rank_by_weight_is_strict_and_ordered(self):
        self.make_ranked()
        self.assertEqual(self.rank(by="above_weight", weight="5")[0], [1, 3])
        self.assertEqual(self.rank(by="below_weight", weight="5")[0], [2])
        self.assertEqual(self.rank(by="above_weight", weight="5", limit="1")[0], [1])
        ids, out = self.rank(by="above_weight", weight="5", limit="1")
        self.assertEqual(out["total_matching"], 2)
        self.assertIn("note", out)
        self.assertEqual(self.rank(by="Below Weight", weight="10")[0], [2, 4, 3, 1])
        self.assertEqual(self.rank(by="above_weight", weight="9")[0], [])
        entry = self.run_async(self.tool.agent_notes_rank_entries("n", "most_popular", limit="1"))["entries"][0]
        self.assertEqual((entry["weight"], entry["hits"]), (9, 3))

    def test_rank_arguments_are_checked(self):
        self.make_ranked()
        for kw in ({"by": "newest"}, {"by": "above_weight"}, {"by": "below_weight", "weight": "11"}, {"by": "most_popular", "limit": "0"}):
            self.assertFalse(self.run_async(self.tool.agent_notes_rank_entries("n", **kw))["success"], kw)
        self.assertFalse(self.run_async(self.tool.agent_notes_rank_entries("nope", "most_popular"))["success"])
        self.assertTrue(self.run_async(self.tool.agent_notes_rank_entries("n", "most_popular", weight="999"))["success"])  # weight ignored

    def test_rank_strongest_takes_both_ends_furthest_from_neutral_first(self):
        self.make_ranked()  # scores: e1 9, e2 2, e3 6, e4 5
        ids, out = self.rank(by="strongest")
        self.assertEqual(ids, [1, 2])  # 9 is 4 away, 2 is 3 away; 6 and 5 are not strong
        self.assertEqual(out["total_matching"], 2)
        self.tool.valves.STRONG_HIGH = 6  # lowering the cutoff lets the 6 in
        self.assertEqual(self.rank(by="strongest")[0], [1, 2, 3])

    def test_upgrade_adds_the_score_columns_and_old_rows_get_defaults(self):
        self.create("old", "plain")
        conn = sqlite3.connect(self.db)
        for table, col in (("note_id", "hits"), ("note_data", "hits"), ("note_data", "weight")):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        conn.commit(); conn.close()
        mod._schema_ready.discard(self.db)
        mod.notes_db_connect(self.db).close()  # connecting is what upgrades
        self.assertEqual(self.hits("old"), (0, [0]))
        self.assertEqual(self.sql("SELECT weight FROM note_data"), [(50,)])
        self.assertTrue(self.run_async(self.tool.agent_notes_set_weight("old", "1", weight="7"))["success"])

    def test_database_rejects_out_of_range_weight(self):
        self.create("n", "one")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE note_data SET weight = 101")

    def test_list_verbose_shows_note_reads(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_read("n"))
        self.assertIn("| Reads |", self.run_async(self.tool.agent_notes_list(verbose=True))["table"])

    def test_create_append_and_edit_can_set_the_weight(self):
        made = self.run_async(self.tool.agent_notes_create("n", "one", weight="8"))
        self.assertEqual(made["weight"], 8)
        self.run_async(self.tool.agent_notes_append("n", "two"))  # no weight: neutral
        self.assertEqual(self.run_async(self.tool.agent_notes_append("n", "three", weight=" 1 "))["weight"], 1)
        self.run_async(self.tool.agent_notes_append("n", "word " * 300, continues=True, weight="3"))  # 3 pieces
        self.assertEqual([r[0] for r in self.sql("SELECT weight FROM note_data ORDER BY entry_no")], [80, 50, 10, 30, 30, 30])
        out = self.run_async(self.tool.agent_notes_edit_entry("n", "2", "two, revised", weight="9"))
        self.assertEqual(out["weight"], 9)
        self.run_async(self.tool.agent_notes_edit_entry("n", "2", "two, again"))  # no weight: kept
        self.run_async(self.tool.agent_notes_edit_entry("n", "2", "two, again", weight=""))
        self.assertEqual(self.sql("SELECT weight, note_text FROM note_data WHERE entry_no = 2"), [(90, "two, again")])

    def test_bad_weight_on_write_saves_nothing(self):
        self.create("n", "one")
        for call in (
            lambda: self.tool.agent_notes_append("n", "x", weight="11"),
            lambda: self.tool.agent_notes_append("n", "x", weight="-5"),
            lambda: self.tool.agent_notes_append("n", "x", weight="heavy"),
            lambda: self.tool.agent_notes_create("m", "x", weight="500"),
            lambda: self.tool.agent_notes_edit_entry("n", "1", "changed", weight="11"),
        ):
            self.assertFalse(self.run_async(call())["success"])
        self.assertEqual(self.sql("SELECT note_text, weight FROM note_data"), [("one", 50)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_id")[0][0], 1)

    def test_empty_note_keeps_the_note_makes_the_summary_stale_and_is_not_a_model_tool(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_append("n", "two"))
        self.run_async(self.tool.agent_notes_update_summary("n", "about stuff"))
        self.assertEqual(mod.empty_note_db(self.db, "N")["entries_removed"], 2)
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual((out["total_entries"], out["entries"], out["summary"]["stale"]), (0, [], True))
        self.assertEqual(self.run_async(self.tool.agent_notes_append("n", "three"))["entry_no"], 3)
        self.assertFalse([m for m in dir(self.tool) if "empty" in m])
        with self.assertRaises(mod.NoteError):
            mod.empty_note_db(self.db, "missing")

    # ---- agent_notes_help ----

    def help_guides(self):
        return {t: self.run_async(self.tool.agent_notes_help(t)) for t in mod.HELP_ORDER}

    def test_help_overview_is_the_default_and_every_topic_works(self):
        default = self.run_async(self.tool.agent_notes_help())
        self.assertEqual((default["success"], default["topic"]), (True, "overview"))
        self.assertEqual(self.run_async(self.tool.agent_notes_help("")), default)
        for topic, out in self.help_guides().items():
            self.assertTrue(out["success"] and out["guide"], topic)
            self.assertNotIn(topic, out["other_topics"].split(", "))
            self.assertLess(len(out["guide"]), 1600, f"{topic} is too long for a small model")

    def test_every_tool_method_is_explained_by_some_topic(self):
        text = " ".join(g["guide"] for g in self.help_guides().values())
        methods = [m for m in dir(self.tool) if m.startswith("agent_notes_") and m != "agent_notes_help"]
        self.assertGreaterEqual(len(methods), 20)  # a floor, so the check can't pass by finding nothing
        self.assertEqual([m for m in methods if m not in text], [])

    def test_help_topic_matching_and_unknown_topics(self):
        self.assertEqual(self.run_async(self.tool.agent_notes_help("long"))["topic"], "long_text")
        self.assertEqual(self.run_async(self.tool.agent_notes_help(" Long-Text "))["topic"], "long_text")
        self.assertEqual(self.run_async(self.tool.agent_notes_help("sum"))["topic"], "summaries")
        for bad in ("nonsense", "o"):  # "o" is ambiguous: overview, organize
            out = self.run_async(self.tool.agent_notes_help(bad))
            self.assertFalse(out["success"])
            self.assertIn("long_text", out["error"])

    def test_help_quotes_the_limits_and_valves_actually_in_force(self):
        self.tool.valves.MAX_CONTINUATION_ENTRIES = 3
        self.tool.valves.MAX_READ_ENTRIES = 17
        self.assertIn("At most 3 entries (about 1500 characters)", self.run_async(self.tool.agent_notes_help("long_text"))["guide"])
        self.tool.valves.READ_DEFAULT_RECENT, self.tool.valves.READ_STRONG_EACH = 8, 4
        self.tool.valves.STRONG_LOW, self.tool.valves.STRONG_HIGH, self.tool.valves.READ_MAX_CHARS = 2, 8, 4000
        guide = self.run_async(self.tool.agent_notes_help("read"))["guide"]
        for fact in ("at most 17", "the 8 newest entries", "up to 4 of the note's strongest", "score 8 or more", "score 2 or less", "about 4000 characters"):
            self.assertIn(fact, guide)
        self.assertIn(str(mod.NOTE_TEXT_MAX), self.run_async(self.tool.agent_notes_help("write"))["guide"])

    def test_help_does_not_touch_the_database(self):
        self.run_async(self.tool.agent_notes_help("write"))
        self.assertFalse(Path(self.db).exists())

    # ---- too-long retries become continues=true ----

    LONG = "word " * 150  # 750 characters: two entries

    def append(self, text=None, name="n", **kw):
        return self.run_async(self.tool.agent_notes_append(name, text or self.LONG, **kw))

    def test_third_too_long_call_in_a_row_is_quietly_saved_as_linked_entries(self):
        self.create("n", "start")
        first, second = self.append(), self.append()
        self.assertFalse(first["success"] or second["success"])
        self.assertIn("over by 249", first["error"])
        self.assertTrue(first["error"].startswith("Entry text is 749 characters; the limit is 500"))
        for refusal in (first, second):  # the refusal is the usual one: it doesn't mention any fallback or say how many retries are left
            self.assertNotIn("automatic", refusal["error"])
            self.assertNotIn("more time", refusal["error"])
            self.assertNotIn("linked", refusal["error"])
        third = self.append()
        self.assertTrue(third["success"], third)
        self.assertEqual(third["entry_numbers"], "2-3")
        self.assertNotIn("note", third)  # no scolding or advertising in the reply
        entries = self.run_async(self.tool.agent_notes_read("n"))["entries"]
        self.assertEqual("".join(e["text"] for e in entries[1:]), self.LONG.strip())
        self.assertEqual(entries[1]["continues_at"], 3)
        self.assertFalse(self.append()["success"])  # the count starts again after an automatic save

    def test_each_automatic_save_is_logged_for_the_owner(self):
        self.create("n", "start")
        self.append(), self.append()
        with self.assertLogs("agent_notes", level="INFO") as logged:
            self.append()
        self.assertIn("3 times in a row", logged.output[0])

    def test_valve_zero_never_switches_continues_on(self):
        self.tool.valves.AUTO_CONTINUE_AFTER = 0
        self.create("n", "start")
        for _ in range(6):
            out = self.append()
            self.assertFalse(out["success"])
            self.assertNotIn("automatically", out["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data")[0][0], 1)

    def test_valve_sets_how_many_it_takes(self):
        self.tool.valves.AUTO_CONTINUE_AFTER = 1
        self.create("n", "start")
        self.assertTrue(self.append()["success"])  # the very first too-long call is saved
        self.tool.valves.AUTO_CONTINUE_AFTER = 2
        self.assertFalse(self.append()["success"])
        self.assertTrue(self.append()["success"])

    def test_anything_else_in_between_breaks_the_run(self):
        self.create("n", "start")
        self.append()
        self.append()
        self.assertTrue(self.append("short and fine")["success"])  # a good write ends the run
        self.append()
        self.append()
        self.assertTrue(self.append()["success"])  # third since the reset: saved
        self.append()
        self.append()
        self.assertTrue(self.append(continues=True)["success"])  # asking properly also ends it
        self.assertFalse(self.append()["success"])

    def test_counts_are_per_author_and_per_note(self):
        self.create("n", "start")
        self.create("m", "start")
        self.append(), self.append()
        self.assertFalse(self.append(name="m")["success"])  # another note: its own count
        self.assertFalse(self.append(author_name="Someone else")["success"])  # another author too
        self.assertTrue(self.append()["success"])  # the original pair's third

    def test_refusals_stop_counting_after_the_window(self):
        self.create("n", "start")
        clock = [1000.0]
        original, mod._clock = mod._clock, lambda: clock[0]
        try:
            self.append(), self.append()
            clock[0] += mod.OVERLONG_WINDOW_S + 1
            self.assertFalse(self.append()["success"])  # the old two no longer count
            clock[0] += 10
            self.assertFalse(self.append()["success"])
            self.assertTrue(self.append()["success"])
        finally:
            mod._clock = original

    def test_create_works_the_same_way_and_too_long_even_for_the_cap_is_never_counted(self):
        for _ in range(2):
            self.assertFalse(self.run_async(self.tool.agent_notes_create("big", self.LONG))["success"])
        out = self.run_async(self.tool.agent_notes_create("big", self.LONG))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["entry_numbers"], "1-2")
        self.assertEqual(out["note"], "Created. Use agent_notes_append to add more entries.")
        self.tool.valves.MAX_CONTINUATION_ENTRIES = 2
        for _ in range(5):
            refused = self.append("word " * 400)
            self.assertFalse(refused["success"])
            self.assertIn("the limit is 500", refused["error"])
        self.assertEqual(mod._overlong, {})

    # ---- the tiered read, paging, entry fetch, sampling, the size budget and card search ----

    def fill(self, count, name="n", text=lambda i: f"e{i}", scores=None):
        """A note of `count` entries 'e1'..; scores maps entry number -> 1-10 score (stored times ten, as the tool does)."""
        self.create(name, text(1))
        for i in range(2, count + 1):
            self.run_async(self.tool.agent_notes_append(name, text(i)))
        if scores:
            conn = sqlite3.connect(self.db)
            for no, score in scores.items():
                conn.execute("UPDATE note_data SET weight = ? WHERE entry_no = ?", (score * 10, no))
            conn.commit(); conn.close()

    def read(self, **kw):
        out = self.run_async(self.tool.agent_notes_read("n", **kw))
        self.assertTrue(out["success"], out)
        return out

    def test_read_gives_recent_entries_plus_the_strongest_older_memories_each_side(self):
        self.fill(30, scores={3: 9, 5: 1, 7: 8, 9: 2, 11: 6, 29: 10})  # 11 is only mildly positive; 29 is strong but already recent
        out = self.read()
        self.assertEqual([e["text"] for e in out["entries"]], [f"e{i}" for i in range(21, 31)])
        self.assertEqual([e["entry_no"] for e in out["strong_memories"]["positive"]], [3, 7])  # strongest first
        self.assertEqual([e["entry_no"] for e in out["strong_memories"]["negative"]], [5, 9])
        self.assertEqual([e["weight"] for e in out["strong_memories"]["positive"]], [9, 8])
        self.assertEqual(out["total_entries"], 30)
        self.assertIn("16 not shown", out["note"])
        self.assertIn("before=21", out["note"])
        self.assertEqual(out["entries"][8]["weight"], 10)  # entry 29: shown once, among the recent

    def test_strong_memories_are_capped_per_side_and_can_be_switched_off(self):
        self.fill(30, scores={3: 9, 7: 8, 5: 1, 9: 2})
        self.tool.valves.READ_STRONG_EACH = 1
        out = self.read()
        self.assertEqual(([e["entry_no"] for e in out["strong_memories"]["positive"]], [e["entry_no"] for e in out["strong_memories"]["negative"]]), ([3], [5]))
        self.tool.valves.READ_STRONG_EACH = 0
        self.assertNotIn("strong_memories", self.read())

    def age(self, days_ago):
        import datetime
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)).isoformat()

    def set_ages(self, ages):
        conn = sqlite3.connect(self.db)
        for no, days in ages.items():
            conn.execute("UPDATE note_data SET created_at = ? WHERE entry_no = ?", (self.age(days), no))
        conn.commit(); conn.close()

    def test_age_bias_lets_an_older_strong_memory_outrank_a_fresh_peak(self):
        self.fill(30, scores={3: 7, 7: 10, 9: 10})
        self.set_ages({3: 40, 7: 1, 9: 0.1})  # a 7 from 40 days ago; a 10 from yesterday; a 10 from hours ago
        self.tool.valves.STRONG_RAMP_MAX_DAYS = 0
        self.assertEqual([e["entry_no"] for e in self.read()["strong_memories"]["positive"]], [9, 7, 3])  # raw: strongest, newest first on ties
        self.tool.valves.STRONG_RAMP_MAX_DAYS = 90
        self.assertEqual([e["entry_no"] for e in self.read()["strong_memories"]["positive"]], [3, 7, 9])  # 3: 20 x 1.0; 7: 50 x 0.29; 9: 50 x 0.26

    def test_age_bias_applies_to_negative_memories_and_not_to_who_qualifies(self):
        self.fill(30, scores={3: 3, 7: 1, 9: 4})
        self.set_ages({3: 40, 7: 0.1})
        self.tool.valves.STRONG_RAMP_MAX_DAYS = 90
        out = self.read()["strong_memories"]["negative"]
        self.assertEqual([e["entry_no"] for e in out], [3, 7])  # 9 (a 4) never qualified

    def test_age_bias_is_off_for_persona_notes(self):
        self.fill(30, scores={3: 7, 7: 10})
        self.set_ages({3: 40, 7: 0.1})
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE note_id SET kind = 'persona'")
        conn.commit(); conn.close()
        self.tool.valves.STRONG_RAMP_MAX_DAYS = 90
        self.assertEqual([e["entry_no"] for e in self.read()["strong_memories"]["positive"]], [7, 3])

    def test_auto_ramp_days_is_the_notes_age_in_whole_weeks_within_a_cap(self):
        import datetime
        now = datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc)
        ago = lambda d: (now - datetime.timedelta(days=d)).isoformat()  # noqa: E731
        self.assertEqual(mod.auto_ramp_days(ago(0.2), 90, now), 7)
        self.assertEqual(mod.auto_ramp_days(ago(7), 90, now), 7)
        self.assertEqual(mod.auto_ramp_days(ago(12), 90, now), 14)
        self.assertEqual(mod.auto_ramp_days(ago(21), 90, now), 21)
        self.assertEqual(mod.auto_ramp_days(ago(22), 90, now), 28)
        self.assertEqual(mod.auto_ramp_days(ago(400), 90, now), 90)
        self.assertEqual(mod.auto_ramp_days(ago(400), 60, now), 60)
        self.assertEqual(mod.auto_ramp_days(ago(12), 0, now), 0)
        self.assertEqual(mod.auto_ramp_days("garbage", 90, now), 0)

    def test_spread_draws_strong_memories_at_random_in_proportion_to_strength(self):
        self.fill(40, scores={**{n: 8 for n in range(1, 21)}, 25: 10})
        self.tool.valves.STRONG_SPREAD = True
        self.tool.valves.STRONG_RAMP_MAX_DAYS = 0
        self.tool.valves.READ_STRONG_EACH = 3
        mod._rng.seed(1)
        seen = collections_counter()
        for _ in range(60):
            picks = [e["entry_no"] for e in self.read()["strong_memories"]["positive"]]
            self.assertEqual(len(picks), len(set(picks)))
            self.assertTrue(set(picks) <= set(range(1, 21)) | {25})
            seen.update(picks)
        self.assertGreater(len(seen), 10)  # a spread of many, not the same three
        self.assertGreater(seen[25], max(seen[n] for n in range(1, 21)))  # the 10 comes up more than any single 8

    def test_spread_returns_everything_when_there_are_few_and_never_touches_persona(self):
        self.fill(30, scores={3: 7, 7: 9})
        self.tool.valves.STRONG_SPREAD = True
        for _ in range(5):
            self.assertEqual([e["entry_no"] for e in self.read()["strong_memories"]["positive"]], [7, 3])  # all qualify, strongest first
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE note_id SET kind = 'persona'")
        conn.commit(); conn.close()
        self.tool.valves.READ_STRONG_EACH = 1
        for _ in range(5):
            self.assertEqual([e["entry_no"] for e in self.read()["strong_memories"]["positive"]], [7])

    def test_age_factor(self):
        import datetime
        now = datetime.datetime(2026, 1, 31, tzinfo=datetime.timezone.utc)
        self.assertAlmostEqual(mod.age_factor("2026-01-31T00:00:00+00:00", 30, now), mod.RAMP_FLOOR)
        self.assertAlmostEqual(mod.age_factor("2026-01-16T00:00:00+00:00", 30, now), mod.RAMP_FLOOR + (1 - mod.RAMP_FLOOR) / 2)
        self.assertEqual(mod.age_factor("2025-01-01T00:00:00+00:00", 30, now), 1.0)
        self.assertEqual(mod.age_factor("2026-01-31T00:00:00+00:00", 0, now), 1.0)
        self.assertEqual(mod.age_factor("garbage", 30, now), 1.0)

    def test_strong_cutoffs_are_valves_and_a_small_note_is_shown_whole(self):
        self.fill(30, scores={3: 6})
        self.tool.valves.STRONG_HIGH = 6
        self.assertEqual([e["entry_no"] for e in self.read()["strong_memories"]["positive"]], [3])
        self.fill(4, name="small")
        out = self.run_async(self.tool.agent_notes_read("small"))
        self.assertEqual([e["text"] for e in out["entries"]], ["e1", "e2", "e3", "e4"])
        self.assertNotIn("note", out)  # nothing left out, nothing to say

    def test_read_limit_sets_the_recent_count_and_the_valve_caps_it(self):
        self.fill(30)
        self.assertEqual(len(self.read(limit="4")["entries"]), 4)
        self.tool.valves.MAX_READ_ENTRIES = 6
        self.assertEqual(len(self.read(limit="25")["entries"]), 6)
        self.tool.valves.READ_DEFAULT_RECENT = 3
        self.assertEqual(len(self.read()["entries"]), 3)

    def test_read_size_budget_keeps_the_newest_whole_entries_and_says_what_it_dropped(self):
        self.fill(12, text=lambda i: f"{i:02d} " + "x" * 120, scores={1: 9, 2: 1})
        self.tool.valves.READ_MAX_CHARS = 700  # 500 is the floor; each entry costs its text plus a little
        out = self.read()
        shown = [e["entry_no"] for e in out["entries"]]
        self.assertEqual(shown, list(range(12 - len(shown) + 1, 13)))  # a run ending at the newest
        self.assertLess(len(shown), 10)
        # the budget is shared out one entry at a time - newest, strongest positive, strongest negative, next newest... - so a tight
        # budget still carries both strong memories instead of letting long recent entries starve them
        self.assertEqual([e["entry_no"] for e in out["strong_memories"]["positive"]], [1])
        self.assertEqual([e["entry_no"] for e in out["strong_memories"]["negative"]], [2])
        self.assertIn("left out to stay within the size limit", out["note"])
        every = out["entries"] + out["strong_memories"]["positive"] + out["strong_memories"]["negative"]
        self.assertTrue(all(len(e["text"]) == 123 for e in every))  # whole entries, never cut

    def test_read_before_pages_back_and_says_where_to_continue(self):
        self.fill(30)
        page = self.read(before="21", limit="5")
        self.assertEqual([e["entry_no"] for e in page["entries"]], [16, 17, 18, 19, 20])
        self.assertNotIn("strong_memories", page)
        self.assertIn("15 older entries remain; continue with before=16", page["note"])
        last = self.read(before="4")
        self.assertEqual([e["entry_no"] for e in last["entries"]], [1, 2, 3])
        self.assertNotIn("note", last)
        self.assertIn("No entries are older than #1", self.read(before="1")["note"])

    def test_read_entry_fetches_particular_entries_in_full_whatever_their_age(self):
        self.fill(30)
        out = self.read(entry="#3, 12 99")
        self.assertEqual([e["entry_no"] for e in out["entries"]], [3, 12])
        self.assertIn("#99", out["note"])
        self.assertFalse(self.run_async(self.tool.agent_notes_read("n", entry="soon"))["success"])
        self.assertEqual(len(self.read(entry=" ".join(str(i) for i in range(1, 25)))["entries"]), 10)  # at most ten at a time

    def test_read_sample_brings_back_random_older_entries_not_the_recent_ones(self):
        self.fill(40)
        seen = set()
        for _ in range(6):
            out = self.read(sample="3")
            numbers = [e["entry_no"] for e in out["entries"]]
            self.assertEqual(len(numbers), 3)
            self.assertEqual(numbers, sorted(set(numbers)))
            self.assertTrue(all(n <= 30 for n in numbers))  # the newest 10 are left out
            seen.update(numbers)
        self.assertGreater(len(seen), 3)  # it really is random
        self.assertEqual(len(self.read(sample="50")["entries"]), 10)  # capped

    def test_count_false_keeps_a_sweep_out_of_the_popularity_counts(self):
        self.fill(3)
        self.read(count="false")
        self.read(count=False)
        self.assertEqual(self.hits(), (0, [0, 0, 0]))
        self.read(count="")
        self.read()
        self.assertEqual(self.hits(), (2, [2, 2, 2]))

    def test_read_counts_only_the_entries_it_delivered_including_strong_ones(self):
        self.fill(30, scores={3: 9})
        self.read()
        counted = {r[0]: r[1] for r in self.sql("SELECT entry_no, hits FROM note_data")}
        self.assertEqual(counted[3], 1)  # the strong memory
        self.assertEqual(counted[30], 1)
        self.assertEqual(counted[15], 0)  # neither recent nor strong

    def test_search_cards_snippet_total_paging_and_all_words(self):
        self.fill(1, name="a", text=lambda i: "Gordon likes tea " + "padding " * 50 + "and the lighthouse at dusk")
        self.run_async(self.tool.agent_notes_append("a", "Gordon likes coffee"))
        self.run_async(self.tool.agent_notes_append("a", "Gordon, tea, again"))
        both = self.run_async(self.tool.agent_notes_search("gordon TEA"))
        self.assertEqual(both["total_matching_entries"], 2)  # all words, any case
        self.assertEqual(self.run_async(self.tool.agent_notes_search("gordon lighthouse"))["total_matching_entries"], 1)
        card = self.run_async(self.tool.agent_notes_search("lighthouse"))["cards"][0]
        self.assertIn("lighthouse", card["snippet"])  # the snippet is cut around the match, not just the start
        self.assertLessEqual(len(card["snippet"]), self.tool.valves.SEARCH_SNIPPET_CHARS + 2)
        self.assertTrue(card["snippet"].startswith("\N{HORIZONTAL ELLIPSIS}"))
        first = self.run_async(self.tool.agent_notes_search("gordon", limit="2"))
        self.assertEqual((first["showing"], first["total_matching_entries"], len(first["cards"])), ("1-2 of 3", 3, 2))
        self.assertIn("page=2", first["note"])
        second = self.run_async(self.tool.agent_notes_search("gordon", limit="2", page="2"))
        self.assertEqual((second["showing"], len(second["cards"])), ("3-3 of 3", 1))
        self.assertNotIn("page=3", second["note"])
        past = self.run_async(self.tool.agent_notes_search("gordon", limit="2", page="9"))
        self.assertEqual(past["cards"], [])
        self.assertIn("past the last", past["note"])
        self.assertIn("read a whole entry", first["note"])
        none = self.run_async(self.tool.agent_notes_search("zebra"))
        self.assertIn("No entry contains any of those words", none["note"])

    def test_search_words_drops_or_and_punctuation(self):
        self.assertEqual(mod.search_words('Outfit OR gig, "clothes" | Outfit'), ["outfit", "gig", "clothes"])
        self.assertEqual(mod.search_words("or"), ["or"])
        self.assertEqual(mod.search_words("  "), [])

    def test_search_with_no_entry_holding_every_word_falls_back_to_any_word_most_words_first(self):
        self.create("a", "black dress for the gig")
        self.run_async(self.tool.agent_notes_append("a", "gig tomorrow at eight"))
        self.run_async(self.tool.agent_notes_append("a", "boots and a jacket"))
        self.run_async(self.tool.agent_notes_append("a", "unrelated"))
        out = self.run_async(self.tool.agent_notes_search("dress OR gig OR jacket"))
        self.assertEqual(out["total_matching_entries"], 3)
        self.assertEqual(out["matched"], "some of the words")
        self.assertEqual({c["entry_no"]: c["words_matched"] for c in out["cards"]}, {1: 2, 2: 1, 3: 1})
        self.assertEqual(out["cards"][0]["entry_no"], 1)  # two words beat one
        self.assertIn("some of them", out["note"])

    def test_search_prefers_entries_with_every_word_and_shows_no_fallback_marks(self):
        self.create("a", "black dress for the gig")
        self.run_async(self.tool.agent_notes_append("a", "gig tomorrow"))
        out = self.run_async(self.tool.agent_notes_search("gig OR dress"))
        self.assertEqual([c["entry_no"] for c in out["cards"]], [1])
        self.assertNotIn("matched", out)
        self.assertNotIn("words_matched", out["cards"][0])

    def test_search_fallback_also_finds_notes_by_any_keyword_and_respects_note_filter(self):
        self.create("outfit-plans", "x")
        self.create("other", "dress is blue")
        out = self.run_async(self.tool.agent_notes_search("outfit dress"))
        self.assertEqual([n["note_name"] for n in out["matching_notes"]], ["outfit-plans"])
        self.assertEqual([c["note_name"] for c in out["cards"]], ["other"])
        only = self.run_async(self.tool.agent_notes_search("outfit dress", note="outfit-plans"))
        self.assertEqual(only["cards"], [])

    def test_search_can_be_limited_to_one_note_and_does_not_list_notes_then(self):
        self.create("a", "needle in a")
        self.create("needle-box", "nothing")
        self.run_async(self.tool.agent_notes_append("needle-box", "a needle here too"))
        everywhere = self.run_async(self.tool.agent_notes_search("needle"))
        self.assertEqual({c["note_name"] for c in everywhere["cards"]}, {"a", "needle-box"})
        self.assertEqual([n["note_name"] for n in everywhere["matching_notes"]], ["needle-box"])
        inside = self.run_async(self.tool.agent_notes_search("needle", note="NEEDLE-box"))
        self.assertEqual([c["note_name"] for c in inside["cards"]], ["needle-box"])
        self.assertNotIn("matching_notes", inside)
        missing = self.run_async(self.tool.agent_notes_search("needle", note="nope"))
        self.assertFalse(missing["success"])

    def test_deleting_or_editing_a_piece_keeps_the_chain_consistent(self):
        self.run_async(self.tool.agent_notes_create("n", "word " * 400, continues=True))  # entries 1-4
        self.run_async(self.tool.agent_notes_delete_entry("n", "2"))
        self.run_async(self.tool.agent_notes_edit_entry("n", "3", "edited"))
        entries = {e["entry_no"]: e for e in self.run_async(self.tool.agent_notes_read("n"))["entries"]}
        self.assertEqual((entries[1]["continues_at"], entries[3]["continues_from"], entries[3]["continues_at"]), (3, 1, 4))
        self.assertEqual(entries[3]["text"], "edited")
        self.run_async(self.tool.agent_notes_delete_entry("n", "1"))
        self.run_async(self.tool.agent_notes_delete_entry("n", "4"))
        self.assertNotIn("continues_at", self.run_async(self.tool.agent_notes_read("n"))["entries"][0])

    def test_upgrade_turns_v1_7_text_markers_into_chains(self):
        self.create("old", "plain")  # makes the database, already at the new schema
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE note_data DROP COLUMN chain_id")
        pk = conn.execute("SELECT note_pk FROM note_id").fetchone()[0]
        for no, text in ((2, "alpha [continues at entry #3]"), (3, "beta [continues at entry #4]"), (4, "gamma")):
            conn.execute("INSERT INTO note_data (note_pk, entry_no, note_text, created_at) VALUES (?, ?, ?, '2026-01-01')", (pk, no, text))
        conn.commit(); conn.close()
        mod._schema_ready.discard(self.db)
        entries = {e["entry_no"]: e for e in self.run_async(self.tool.agent_notes_read("old"))["entries"]}
        self.assertEqual([entries[n]["text"] for n in (1, 2, 3, 4)], ["plain", "alpha", "beta", "gamma"])
        self.assertEqual((entries[2]["continues_at"], entries[3]["continues_at"], entries[4]["continues_from"]), (3, 4, 3))
        self.assertNotIn("continues_at", entries[1])

    def test_continues_keeps_the_break_so_joined_pieces_equal_the_text(self):
        for text in (
            "alpha beta gamma. " * 90,  # spaces
            "\n\n".join(f"Paragraph {i} " + "lorem ipsum " * 12 for i in range(12)),  # paragraph breaks
            "\n".join(f"line {i} " + "dolor sit " * 9 for i in range(20)),  # line breaks
            "word   " * 150,  # runs of spaces
        ):
            parts = mod.split_text(text, 20)
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(len(p) <= mod.NOTE_TEXT_MAX for p in parts))
            self.assertEqual("".join(parts), text.strip())
            self.assertTrue(all(not p[0].isspace() for p in parts[1:]) or "   " in text)

    def test_continues_stored_pieces_keep_their_trailing_break(self):
        text = "abcd " * 300
        self.run_async(self.tool.agent_notes_create("sp", text, continues=True))
        entries = self.run_async(self.tool.agent_notes_read("sp"))["entries"]
        self.assertEqual("".join(e["text"] for e in entries), text.strip())
        self.assertTrue(entries[0]["text"].endswith(" "))

    def test_continues_hard_cuts_an_unbroken_run_and_every_entry_fits(self):
        self.run_async(self.tool.agent_notes_create("blob", "x" * 1400, continues=True))
        entries = self.run_async(self.tool.agent_notes_read("blob"))["entries"]
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(len(e["text"]) <= mod.NOTE_TEXT_MAX for e in entries))
        self.assertEqual(sum(e["text"].count("x") for e in entries), 1400)

    def test_continues_off_still_refuses_and_short_text_is_one_entry(self):
        self.assertFalse(self.run_async(self.tool.agent_notes_create("n", "x" * 501))["success"])
        one = self.run_async(self.tool.agent_notes_create("n", "short", continues=True))
        self.assertEqual(one["entry_no"], 1)
        self.assertNotIn("entry_numbers", one)

    def test_continues_over_the_valve_saves_nothing(self):
        self.tool.valves.MAX_CONTINUATION_ENTRIES = 2
        self.create("m", "ok")
        out = self.run_async(self.tool.agent_notes_create("n", "word " * 400, continues=True))
        self.assertFalse(out["success"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_id")[0][0], 1)
        self.assertFalse(self.run_async(self.tool.agent_notes_append("m", "word " * 400, continues=True))["success"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data")[0][0], 1)

    def test_search_treats_like_wildcards_literally(self):
        self.create("a", "100% done")
        self.create("b", "nothing here")
        res = self.run_async(self.tool.agent_notes_search("%"))
        self.assertEqual([e["note_name"] for e in res["cards"]], ["a"])

    def test_search_requires_a_query(self):
        self.assertFalse(self.run_async(self.tool.agent_notes_search(""))["success"])


class ProvenanceTests(NotesTestCase):
    """The preset, base model and chat Open WebUI reports for a call are recorded with an entry (and on a rating), apart from the
    author name the model gives itself."""
    MODEL = {"id": "hannah-duo", "name": "Hannah", "info": {"base_model_id": "or.nvidia/nemotron-3-ultra-550b-a55b:free"}}
    META = {"chat_id": "chat-123", "message_id": "m1"}
    COLS = "preset, base_model, source_chat, rated_by"

    def row(self, entry_no):
        return self.sql(f"SELECT {self.COLS} FROM note_data WHERE entry_no = ?", (entry_no,))[0]

    def test_provenance_reads_the_preset_base_and_chat_and_never_raises(self):
        self.assertEqual(mod.provenance(self.MODEL, self.META),
                         {"preset": "hannah-duo", "base_model": "or.nvidia/nemotron-3-ultra-550b-a55b:free", "source_chat": "chat-123"})
        self.assertEqual(mod.provenance({"id": "gpt-x"}, None), {"preset": "gpt-x", "base_model": "gpt-x", "source_chat": None})
        for odd in (None, "x", 5, [], {"info": "no"}):
            self.assertEqual(set(mod.provenance(odd, odd)), {"preset", "base_model", "source_chat"})

    def test_create_and_append_record_where_they_came_from_and_who_rated(self):
        self.run_async(self.tool.agent_notes_create("n", "one", weight="8", __model__=self.MODEL, __metadata__=self.META))
        self.run_async(self.tool.agent_notes_append("n", "two", __model__=self.MODEL, __metadata__=self.META))
        label = "hannah-duo (or.nvidia/nemotron-3-ultra-550b-a55b:free)"
        self.assertEqual(tuple(self.row(1)), ("hannah-duo", "or.nvidia/nemotron-3-ultra-550b-a55b:free", "chat-123", label))
        self.assertEqual(self.row(2)[3], None)  # no weight given: nobody rated it

    def test_set_weight_and_edit_record_the_rater_but_keep_the_writer(self):
        self.run_async(self.tool.agent_notes_create("n", "one", __model__={"id": "writer"}, __metadata__={"chat_id": "c1"}))
        self.run_async(self.tool.agent_notes_set_weight("n", "1", weight="9", __model__={"id": "rater"}, __metadata__={"chat_id": "c2"}))
        self.assertEqual(tuple(self.row(1)), ("writer", "writer", "c1", "rater"))
        self.run_async(self.tool.agent_notes_edit_entry("n", "1", "one, revised", weight="7", __model__={"id": "editor"}))
        self.assertEqual(tuple(self.row(1)), ("writer", "writer", "c1", "editor"))
        self.run_async(self.tool.agent_notes_edit_entry("n", "1", "no new weight", __model__={"id": "third"}))
        self.assertEqual(self.row(1)[3], "editor")  # an edit without a weight is not a rating

    def test_a_call_without_openwebui_context_still_saves_and_leaves_blanks(self):
        self.run_async(self.tool.agent_notes_create("n", "one", weight="6"))
        self.assertEqual(tuple(self.row(1)), (None, None, None, None))

    def test_an_old_database_gains_the_columns_and_keeps_its_rows(self):
        conn = sqlite3.connect(self.db) if Path(self.db).exists() else None
        if conn is None:
            Path(self.db).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db)
        conn.close()
        self.run_async(self.tool.agent_notes_create("n", "kept"))
        conn = sqlite3.connect(self.db)
        for col in mod.PROVENANCE_COLUMNS:
            conn.execute(f"ALTER TABLE note_data DROP COLUMN {col}")
        conn.commit()
        conn.close()
        mod._schema_ready.discard(self.db)
        self.run_async(self.tool.agent_notes_append("n", "after", __model__=self.MODEL, __metadata__=self.META))
        self.assertEqual(self.sql("SELECT note_text FROM note_data ORDER BY entry_no")[0][0], "kept")
        self.assertEqual(self.row(2)[0], "hannah-duo")
        self.assertIsNone(self.row(1)[0])

    def test_what_a_model_reads_back_does_not_include_the_provenance(self):
        self.run_async(self.tool.agent_notes_create("n", "one", weight="8", __model__=self.MODEL, __metadata__=self.META))
        out = json.dumps(self.run_async(self.tool.agent_notes_read("n", verbose=True)))
        for secret in ("hannah-duo", "chat-123", "nemotron"):
            self.assertNotIn(secret, out)


class AuthorTests(NotesTestCase):
    def authors(self):
        return self.sql("SELECT author_id, author_name, created_at FROM note_author ORDER BY author_id")

    def test_fresh_database_has_mara_voss_as_author_one_with_a_timestamp(self):
        self.create()
        first = self.authors()[0]
        self.assertEqual(first[:2], (1, "Mara Voss"))
        self.assertTrue(first[2])

    def test_explicit_author_name_is_recorded_on_note_and_entries(self):
        self.run_async(self.tool.agent_notes_create("n", "one", "", "Claude"))
        self.run_async(self.tool.agent_notes_append("n", "two", "Gemma"))
        res = self.run_async(self.tool.agent_notes_read("n", verbose=True))
        self.assertEqual(res["started_by"], "Claude")
        self.assertEqual([e["author"] for e in res["entries"]], ["Claude", "Gemma"])
        self.assertEqual([a[1] for a in self.authors()], ["Mara Voss", "Claude", "Gemma"])

    def test_model_name_from_open_webui_is_the_default(self):
        self.run_async(self.tool.agent_notes_create("n", "one", __model__={"id": "qwen3:32b", "name": "Qwen 3"}))
        self.run_async(self.tool.agent_notes_append("n", "two", author_name="", __model__={"id": "only-an-id"}))
        self.assertEqual([e["author"] for e in self.run_async(self.tool.agent_notes_read("n", verbose=True))["entries"]], ["Qwen 3", "only-an-id"])

    def test_explicit_name_beats_model_and_blank_everything_falls_back(self):
        self.run_async(self.tool.agent_notes_create("n", "one", author_name="Me", __model__={"name": "Qwen 3"}))
        self.run_async(self.tool.agent_notes_append("n", "two"))
        self.assertEqual([e["author"] for e in self.run_async(self.tool.agent_notes_read("n", verbose=True))["entries"]], ["Me", mod.FALLBACK_AUTHOR])

    def test_default_author_valve_fills_in_a_missing_name_but_not_a_given_one(self):
        self.tool.valves.DEFAULT_AUTHOR = "Agent"
        self.run_async(self.tool.agent_notes_create("n", "one", __model__={"name": "Qwen 3"}))
        self.run_async(self.tool.agent_notes_append("n", "two", author_name=""))
        self.run_async(self.tool.agent_notes_append("n", "three", author_name="Other"))
        self.assertEqual([e["author"] for e in self.run_async(self.tool.agent_notes_read("n", verbose=True))["entries"]], ["Agent", "Agent", "Other"])

    def test_blank_default_author_valve_keeps_the_model_name(self):
        self.tool.valves.DEFAULT_AUTHOR = "  "
        self.run_async(self.tool.agent_notes_create("n", "one", __model__={"name": "Qwen 3"}))
        self.assertEqual(self.run_async(self.tool.agent_notes_read("n", verbose=True))["started_by"], "Qwen 3")

    def test_authors_are_reused_ignoring_case_and_whitespace(self):
        self.run_async(self.tool.agent_notes_create("n", "one", author_name="Claude"))
        self.run_async(self.tool.agent_notes_append("n", "two", "  claude "))
        self.run_async(self.tool.agent_notes_append("n", "three", "mara voss"))
        self.assertEqual([a[1] for a in self.authors()], ["Mara Voss", "Claude"])
        self.assertEqual(self.sql("SELECT author_id FROM note_data ORDER BY entry_no"), [(2,), (2,), (1,)])

    def test_author_name_limit(self):
        res = self.run_async(self.tool.agent_notes_create("n", "one", author_name="x" * 61))
        self.assertFalse(res["success"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_id") if Path(self.db).exists() else [(0,)], [(0,)])

    def test_list_and_search_show_authors(self):
        self.run_async(self.tool.agent_notes_create("n", "needle", author_name="Claude"))
        self.assertIn("| Claude |", self.run_async(self.tool.agent_notes_list(verbose=True))["table"])
        self.assertEqual(self.run_async(self.tool.agent_notes_search("needle", verbose=True))["cards"][0]["author"], "Claude")


class LeadingDateTests(NotesTestCase):
    def test_strip_leading_date(self):
        for text, want in [
            ("[2026-10-14] Gordon said hi", "Gordon said hi"),
            ("2026-10-14 14:30: Gordon said hi", "Gordon said hi"),
            ("October 14, 2026 - Gordon", "Gordon"),
            ("**Thursday, October 14, 2026** Gordon", "Gordon"),
            ("(10/14/2026 3:05 PM) hi", "hi"),
            ("Date: 2026-10-14 - hi", "hi"),
            ("14 October 2026, Gordon", "Gordon"),
            ("Oct 14: we talked", "we talked"),
            ("[Oct 14] hi", "hi"),
            ("May 5 people came", "May 5 people came"),
            ("March 3 deadlines loom", "March 3 deadlines loom"),
            ("In 2026 we met", "In 2026 we met"),
            ("We met on 2026-10-14 and talked", "We met on 2026-10-14 and talked"),
            ("2026-10-14", "2026-10-14"),
            ("ACT - Monday 10:11 PM EDT (quiet hours). No emails", "ACT - (quiet hours). No emails"),
            ("NOOP: 2026-10-18 22:11 EDT - nothing", "NOOP: nothing"),
            ("ACT - 2026-10-18T22:11:04-04:00 did x", "ACT - did x"),
            ("Monday 10:11 PM EDT - x", "x"),
            ("Sun 22:11 hi", "hi"),
            ("Monday: we met", "we met"),
            ("10:11 PM - did x", "did x"),
            ("[10:11 PM] did x", "did x"),
            ("Monday was rough", "Monday was rough"),
            ("Friday night plans", "Friday night plans"),
            ("We met at 10:11 PM", "We met at 10:11 PM"),
            ("22:11 next day we met", "22:11 next day we met"),
            ("ACT - did the thing", "ACT - did the thing"),
            ("ACT - Monday", "ACT - Monday"),
            ("NOOP - 10pm EDT Sunday, all emails read", "NOOP - all emails read"),
            ("9pm - did x", "did x"),
            ("3pm story brainstorming was fun", "3pm story brainstorming was fun"),
            ("NOOP - 2026-10-19 02:30 UTC: checked email", "NOOP - checked email"),
        ]:
            self.assertEqual(mod.strip_leading_date(text), want, text)

    def test_create_append_and_edit_scrub_the_start(self):
        self.run_async(self.tool.agent_notes_create("n", "[2026-10-14] one"))
        self.run_async(self.tool.agent_notes_append("n", "October 15, 2026 - two"))
        self.run_async(self.tool.agent_notes_edit_entry("n", "1", "2026-10-16: uno"))
        self.assertEqual([e["text"] for e in self.run_async(self.tool.agent_notes_read("n"))["entries"]], ["uno", "two"])

    def test_continues_scrubs_only_the_start(self):
        text = "[2026-10-14] " + "word " * 150 + "2026-10-15 end"
        self.run_async(self.tool.agent_notes_create("n", text, continues=True))
        entries = [e["text"] for e in self.run_async(self.tool.agent_notes_read("n", limit="10"))["entries"]]
        self.assertTrue(entries[0].startswith("word"))
        self.assertIn("2026-10-15 end", " ".join(entries))


class TimestampTests(NotesTestCase):
    def test_create_with_created_at_dates_note_and_first_entry(self):
        res = self.run_async(self.tool.agent_notes_create("old", "x", created_at="2026-03-01T14:30:00Z"))
        self.assertEqual(res["created_at"], "2026-03-01T14:30:00+00:00")
        self.assertEqual(self.sql("SELECT created_at, updated_at FROM note_id"), [("2026-03-01T14:30:00+00:00",) * 2])
        self.assertEqual(self.sql("SELECT created_at FROM note_data"), [("2026-03-01T14:30:00+00:00",)])

    def test_backdated_append_sorts_by_time_and_keeps_updated_at(self):
        self.create("n", "now-ish")
        before = self.sql("SELECT updated_at FROM note_id")
        self.run_async(self.tool.agent_notes_append("n", "from last year", created_at="2025-01-02"))
        self.assertEqual(self.sql("SELECT updated_at FROM note_id"), before)
        entries = self.run_async(self.tool.agent_notes_read("n", verbose=True))["entries"]
        self.assertEqual([(e["entry_no"], e["created_at"][:10]) for e in entries][0], (2, "2025-01-02"))
        self.assertEqual(entries[1]["entry_no"], 1)

    def test_newer_than_note_timestamp_advances_updated_at(self):
        self.run_async(self.tool.agent_notes_create("n", "a", created_at="2025-01-01"))
        self.run_async(self.tool.agent_notes_append("n", "b", created_at="2025-06-01T00:00:00+00:00"))
        self.assertEqual(self.sql("SELECT updated_at FROM note_id"), [("2025-06-01T00:00:00+00:00",)])

    def test_accepted_formats_normalize_to_utc(self):
        for value, expected in [
            ("2026-03-01", "2026-03-01T00:00:00+00:00"),
            ("2026-03-01T14:30:00", "2026-03-01T14:30:00+00:00"),
            ("2026-03-01T09:30:00-05:00", "2026-03-01T14:30:00+00:00"),
            (1772375400, "2026-03-01T14:30:00+00:00"),  # seconds
            ("1772375400000", "2026-03-01T14:30:00+00:00"),  # milliseconds
            (1772375400000000000, "2026-03-01T14:30:00+00:00"),  # nanoseconds (Open WebUI's own notes)
        ]:
            self.assertEqual(mod.validate_timestamp(value), expected, value)

    def test_blank_means_now_and_bad_values_are_refused_without_writing(self):
        self.assertIsNone(mod.validate_timestamp(""))
        before = self.create("n", "x")["created_at"]
        for bad in ["yesterday", "2999-01-01", "1960-01-01"]:
            res = self.run_async(self.tool.agent_notes_append("n", "y", created_at=bad))
            self.assertFalse(res["success"], bad)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data"), [(1,)])
        self.assertGreaterEqual(self.run_async(self.tool.agent_notes_append("n", "z"))["created_at"], before)

    def test_author_row_keeps_real_time_when_entry_is_backdated(self):
        self.run_async(self.tool.agent_notes_create("n", "x", author_name="Claude", created_at="2020-01-01"))
        self.assertGreater(self.sql("SELECT created_at FROM note_author WHERE author_name = 'Claude'")[0][0], "2026")


class SummaryTests(NotesTestCase):
    def summarize(self, name="n", text="the dragon lives in a lair", **kw):
        return self.run_async(self.tool.agent_notes_update_summary(name, text, **kw))

    def test_update_creates_then_replaces(self):
        self.create("n", "x")
        first = self.summarize(author_name="Nightly")
        self.assertTrue(first["success"] and first["created"])
        second = self.summarize(text="revised summary")
        self.assertTrue(second["success"] and not second["created"])
        self.assertEqual(self.sql("SELECT COUNT(*), summary_text FROM note_summary"), [(1, "revised summary")])

    def test_summary_requires_existing_note_and_valid_text(self):
        self.assertFalse(self.summarize("nope")["success"])
        self.create("n", "x")
        self.assertFalse(self.summarize(text="")["success"])
        long = self.summarize(text="y" * 501)
        self.assertFalse(long["success"])
        self.assertIn("limit is 500", long["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_summary"), [(0,)])

    def test_summary_whitespace_is_collapsed(self):
        self.create("n", "x")
        self.summarize(text="line one\n\nline   two")
        self.assertEqual(self.sql("SELECT summary_text FROM note_summary"), [("line one line two",)])

    def test_author_and_read_note_shows_summary_and_staleness(self):
        self.create("n", "x")
        self.summarize(author_name="Nightly")
        got = self.run_async(self.tool.agent_notes_read("n", verbose=True))["summary"]
        self.assertEqual((got["summarized_by"], got["stale"]), ("Nightly", False))
        self.run_async(self.tool.agent_notes_append("n", "more"))
        self.assertTrue(self.run_async(self.tool.agent_notes_read("n"))["summary"]["stale"])

    def test_edit_backdated_append_and_delete_entry_all_make_it_stale(self):
        self.create("n", "x")
        self.run_async(self.tool.agent_notes_append("n", "y"))
        steps = [
            lambda: self.tool.agent_notes_edit_entry("n", 1, "x2"),
            lambda: self.tool.agent_notes_append("n", "old", created_at="2020-01-01"),  # leaves updated_at alone
            lambda: self.tool.agent_notes_delete_entry("n", 2),
        ]
        for step in steps:
            self.summarize()
            self.assertFalse(self.run_async(self.tool.agent_notes_read("n"))["summary"]["stale"])
            self.run_async(step())
            self.assertTrue(self.run_async(self.tool.agent_notes_read("n"))["summary"]["stale"])

    def test_rename_keeps_summary_and_delete_note_removes_it(self):
        self.create("n", "x")
        self.summarize()
        self.run_async(self.tool.agent_notes_update("n", new_name="m"))
        self.assertIn("summary", self.run_async(self.tool.agent_notes_read("m")))
        self.run_async(self.tool.agent_notes_delete("m"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_summary"), [(0,)])

    def test_list_notes_summary_column_and_needs_summary_filter(self):
        self.create("none", "x")
        self.create("stale", "x")
        self.create("current", "x")
        self.summarize("stale")
        self.run_async(self.tool.agent_notes_append("stale", "changed"))
        self.summarize("current")
        table = self.run_async(self.tool.agent_notes_list())["table"]
        for name, state in [("none", "none"), ("stale", "stale"), ("current", "current")]:
            self.assertRegex(table, rf"\| {name} \|.*\| {state} \|")
        todo = self.run_async(self.tool.agent_notes_list(needs_summary=True))
        self.assertEqual(todo["count"], 2)
        self.assertNotIn("\n| current |", todo["table"])
        self.summarize("none")
        self.summarize("stale")
        self.assertIn("No note needs a summary", self.run_async(self.tool.agent_notes_list(needs_summary=True))["table"])

    def test_search_ranks_by_matched_words_and_matches_note_names(self):
        for name, text in [("a", "dragon lair map"), ("b", "dragon only"), ("c", "unrelated tavern"), ("lair-notes", "misc")]:
            self.create(name, "x")
            self.summarize(name, text)
        res = self.run_async(self.tool.agent_notes_search_summary("Dragon LAIR", verbose=True))
        self.assertEqual(res["results"][0]["note_name"], "a")  # both words; the other two match one each
        self.assertEqual(res["results"][0]["matched_words"], "2 of 2")
        self.assertEqual({r["note_name"] for r in res["results"]}, {"a", "b", "lair-notes"})
        self.assertEqual(self.run_async(self.tool.agent_notes_search_summary("zzz"))["results"], [])

    def test_search_wildcards_are_literal_limit_and_required_query(self):
        self.create("n", "x")
        self.summarize("n", "100% done")
        self.assertEqual(self.run_async(self.tool.agent_notes_search_summary("%"))["count"], 1)
        self.assertEqual(self.run_async(self.tool.agent_notes_search_summary("_"))["count"], 0)
        self.assertFalse(self.run_async(self.tool.agent_notes_search_summary(" "))["success"])

    def test_search_flags_stale_results(self):
        self.create("n", "x")
        self.summarize("n", "alpha")
        self.run_async(self.tool.agent_notes_append("n", "y"))
        self.assertTrue(self.run_async(self.tool.agent_notes_search_summary("alpha"))["results"][0]["stale"])

    def test_delete_summary(self):
        self.create("n", "x")
        self.assertFalse(self.run_async(self.tool.agent_notes_delete_summary("n"))["success"])  # none yet
        self.summarize()
        self.assertTrue(self.run_async(self.tool.agent_notes_delete_summary("n"))["success"])
        self.assertNotIn("summary", self.run_async(self.tool.agent_notes_read("n")))
        self.assertEqual(self.run_async(self.tool.agent_notes_read("n"))["total_entries"], 1)


class AuthorMigrationTests(NotesTestCase):
    OLD_SCHEMA = """
    CREATE TABLE note_id (
        note_pk INTEGER PRIMARY KEY AUTOINCREMENT,
        note_name TEXT NOT NULL, note_comment TEXT NOT NULL DEFAULT '', next_entry_no INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX idx_note_name ON note_id(lower(note_name));
    CREATE TABLE note_data (
        note_pk INTEGER NOT NULL REFERENCES note_id(note_pk) ON DELETE CASCADE, entry_no INTEGER NOT NULL,
        note_text TEXT NOT NULL, created_at TEXT NOT NULL, edited_at TEXT, PRIMARY KEY (note_pk, entry_no));
    INSERT INTO note_id VALUES (1, 'old', '', 3, '2026-09-30T00:00:00+00:00', '2026-09-30T00:00:00+00:00');
    INSERT INTO note_data VALUES (1, 1, 'a', '2026-09-30T00:00:00+00:00', NULL), (1, 2, 'b', '2026-09-30T00:01:00+00:00', NULL);
    """

    def exec_raw(self, script):
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.executescript(script)
        conn.close()

    def make_old_db(self):
        self.exec_raw(self.OLD_SCHEMA)

    def test_existing_rows_are_attributed_to_author_one(self):
        self.make_old_db()
        res = self.run_async(self.tool.agent_notes_read("old", verbose=True))
        self.assertEqual(res["started_by"], "Mara Voss")
        self.assertEqual([e["author"] for e in res["entries"]], ["Mara Voss", "Mara Voss"])
        self.assertEqual(self.sql("SELECT author_id, author_name FROM note_author"), [(1, "Mara Voss")])

    def test_new_writes_after_migration_get_their_own_author(self):
        self.make_old_db()
        self.run_async(self.tool.agent_notes_append("old", "c", "Claude"))
        authors = [e["author"] for e in self.run_async(self.tool.agent_notes_read("old", verbose=True))["entries"]]
        self.assertEqual(authors, ["Mara Voss", "Mara Voss", "Claude"])

    def test_migration_is_idempotent_and_does_not_reattribute_later_unattributed_rows(self):
        self.make_old_db()
        self.run_async(self.tool.agent_notes_list())
        # a row written by an older copy of the tool, after the upgrade, has no author...
        self.exec_raw("INSERT INTO note_data (note_pk, entry_no, note_text, created_at) VALUES (1, 3, 'late', 'x')")
        mod._schema_ready.discard(self.db)  # ...and stays that way when another process migrates again
        res = self.run_async(self.tool.agent_notes_read("old", verbose=True))
        self.assertEqual(res["entries"][-1]["author"], "unknown")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_author"), [(1,)])


class TagTests(NotesTestCase):
    def setUp(self):
        super().setUp()
        self.create("alpha")
        self.create("beta")

    def tag(self, name, tags, create=False):
        return self.run_async(self.tool.agent_notes_add_tags(name, tags, create))

    def test_add_and_read_back_tags(self):
        res = self.tag("alpha", "ComfyUI, SDXL Poses")
        self.assertTrue(res["success"])
        self.assertEqual(res["tags"], ["comfyui", "sdxl-poses"])
        self.assertEqual(self.run_async(self.tool.agent_notes_read("alpha"))["tags"], ["comfyui", "sdxl-poses"])

    def test_same_tag_on_another_note_is_shared_and_counted(self):
        self.tag("alpha", "comfy")
        res = self.tag("beta", "Comfy")
        self.assertEqual(res["new_tags"], [])
        self.assertEqual(self.run_async(self.tool.agent_notes_list_tags())["tags"], [{"tag": "comfy", "notes": 2}])

    def test_variant_spellings_reuse_the_existing_tag(self):
        self.tag("alpha", "comfy-ui")
        res = self.tag("beta", "comfyui")
        self.assertEqual(res["tags"], ["comfy-ui"])
        self.assertEqual(res["matched_existing"], {"comfyui": "comfy-ui"})
        res = self.tag("beta", "Comfy UI")  # already on beta
        self.assertEqual(res["already_had"], ["comfy-ui"])

    def test_plural_is_folded(self):
        self.tag("alpha", "pose")
        self.assertEqual(self.tag("beta", "poses")["tags"], ["pose"])

    def test_similar_new_tag_is_refused_with_suggestions_and_saves_nothing(self):
        self.tag("alpha", "sdxl")
        res = self.tag("beta", "unrelated, sdxl-poses")
        self.assertFalse(res["success"])
        self.assertIn("'sdxl'", res["error"])
        self.assertIn("create=true", res["error"])
        self.assertEqual(self.run_async(self.tool.agent_notes_read("beta"))["tags"], [])

    def test_create_true_overrides_the_similarity_check(self):
        self.tag("alpha", "sdxl")
        res = self.tag("beta", "sdxl-poses", create=True)
        self.assertEqual(res["new_tags"], ["sdxl-poses"])

    def test_misspelling_is_caught(self):
        self.tag("alpha", "character")
        self.assertFalse(self.tag("beta", "charcter")["success"])

    def test_tag_validation(self):
        self.assertFalse(self.tag("alpha", "")["success"])
        self.assertFalse(self.tag("alpha", "x" * 41)["success"])
        self.assertFalse(self.tag("alpha", "!!!")["success"])
        self.assertFalse(self.tag("missing", "ok")["success"])

    def test_punctuation_is_cleaned_up_and_reported(self):
        res = self.tag("alpha", "Habits & Interests, don't/stop, C++")
        self.assertEqual(res["tags"], ["c++", "dont-stop", "habits-and-interests"])
        self.assertEqual(res["adjusted"], {"Habits & Interests": "habits-and-interests", "don't/stop": "dont-stop"})
        self.assertNotIn("adjusted", self.tag("beta", "Plain Tag")) 

    def test_per_note_limit(self):
        self.tag("alpha", ", ".join(f"topic{chr(97 + i) * 6}" for i in range(mod.MAX_TAGS_PER_NOTE)))
        res = self.tag("alpha", "onemore", create=True)
        self.assertFalse(res["success"])
        self.assertIn("agent_notes_remove_tag", res["error"])

    def test_remove_tag_and_unused_tag_disappears(self):
        self.tag("alpha", "one, two")
        res = self.run_async(self.tool.agent_notes_remove_tag("alpha", "One"))
        self.assertEqual((res["removed"], res["tags"]), ("one", ["two"]))
        self.assertEqual(self.sql("SELECT tag_name FROM tag"), [("two",)])
        res = self.run_async(self.tool.agent_notes_remove_tag("alpha", "one"))
        self.assertFalse(res["success"])
        self.assertIn("two", res["error"])

    def test_deleting_a_note_drops_its_tags(self):
        self.tag("alpha", "solo")
        self.run_async(self.tool.agent_notes_delete("alpha"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_tag"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tag"), [(0,)])

    def test_list_notes_filters_by_tag_and_shows_tags(self):
        self.tag("alpha", "red")
        res = self.run_async(self.tool.agent_notes_list(tag="Red"))
        self.assertEqual(res["count"], 1)
        self.assertIn("alpha", res["table"])
        self.assertNotIn("beta", res["table"])
        self.assertIn("red (1)", res["tags_in_use"])
        self.assertIn("| red |", self.run_async(self.tool.agent_notes_list())["table"])
        self.assertFalse(self.run_async(self.tool.agent_notes_list(tag="nonexistent"))["success"])

    def test_tagging_does_not_make_a_summary_stale(self):
        self.run_async(self.tool.agent_notes_update_summary("alpha", "about things"))
        self.tag("alpha", "red")
        self.assertEqual(self.run_async(self.tool.agent_notes_list(needs_summary=True))["count"], 1)  # only beta

    def test_search_finds_notes_by_tag(self):
        self.tag("alpha", "dragons")
        res = self.run_async(self.tool.agent_notes_search("dragon"))
        self.assertEqual([n["note_name"] for n in res["matching_notes"]], ["alpha"])

    def test_rename_and_merge(self):
        self.tag("alpha", "sdxl")
        self.tag("beta", "sdxl-poses", create=True)
        res = self.run_async(self.tool.agent_notes_rename_tag("sdxl-poses", "sdxl"))
        self.assertTrue(res["merged"])
        self.assertEqual(res["notes"], 2)
        self.assertEqual(self.sql("SELECT tag_name FROM tag"), [("sdxl",)])
        res = self.run_async(self.tool.agent_notes_rename_tag("sdxl", "Graphics"))
        self.assertEqual((res["merged"], res["tag"]), (False, "graphics"))
        self.assertFalse(self.run_async(self.tool.agent_notes_rename_tag("nope", "x"))["success"])

    def test_existing_database_gains_tag_tables(self):
        self.create("old")
        self.sql("SELECT 1")
        conn = sqlite3.connect(self.db)
        conn.executescript("DROP TABLE note_tag; DROP TABLE tag;")
        conn.close()
        mod._schema_ready.discard(self.db)
        self.assertTrue(self.tag("old", "fresh")["success"])


class ReadTaggedTests(NotesTestCase):
    def setUp(self):
        super().setUp()
        self.create("voice", "speaks softly")
        self.create("history", "born in 1990")
        self.create("hobbies", "paints")
        for name, tags in (("voice", "identity, speech"), ("history", "identity"), ("hobbies", "pastimes")):
            self.run_async(self.tool.agent_notes_add_tags(name, tags))

    def read(self, tags, match=None, verbose=True):
        return self.run_async(self.tool.agent_notes_read_tagged(tags, match, verbose))

    def names(self, res):
        return [n["note_name"] for n in res["notes"]]

    def test_any_returns_union_best_match_first_with_contents(self):
        res = self.read("identity, speech")
        self.assertEqual(self.names(res), ["voice", "history"])  # voice carries both
        self.assertEqual(res["notes"][0]["entries"][0]["text"], "speaks softly")
        self.assertEqual(res["notes"][0]["matched_tags"], ["identity", "speech"])

    def test_all_requires_every_tag(self):
        self.assertEqual(self.names(self.read("identity, speech", "all")), ["voice"])

    def test_variant_spelling_and_unknown_tag(self):
        res = self.read("Identities, nonsense")
        self.assertEqual(sorted(self.names(res)), ["history", "voice"])
        self.assertIn("nonsense", res["note"])
        self.assertFalse(self.read("identity, nonsense", "all")["success"])
        self.assertFalse(self.read("nonsense")["success"])
        self.assertFalse(self.read("identity", "some")["success"])

    def test_plain_mode_is_just_names_summaries_and_text(self):
        self.run_async(self.tool.agent_notes_update_summary("voice", "how she talks"))
        res = self.read("identity, speech", verbose=False)
        self.assertEqual(set(res), {"success", "notes"})
        voice = next(n for n in res["notes"] if n["note_name"] == "voice")
        self.assertEqual(voice, {"note_name": "voice", "summary": "how she talks", "entries": ["speaks softly"]})
        history = next(n for n in res["notes"] if n["note_name"] == "history")
        self.assertEqual(history, {"note_name": "history", "entries": ["born in 1990"]})  # no summary key, no counts when nothing is cut

    def test_plain_mode_fits_more_than_verbose(self):
        for i in range(30):
            self.run_async(self.tool.agent_notes_append("voice", f"line {i} " + "z" * 60))
        self.tool.valves.MAX_TAGGED_CHARS = 2500
        plain = sum(len(n["entries"]) for n in self.read("speech", verbose=False)["notes"])
        full = sum(len(n["entries"]) for n in self.read("speech", verbose=True)["notes"])
        self.assertGreater(plain, full)

    def test_plain_mode_reports_cut_entries(self):
        for i in range(30):
            self.run_async(self.tool.agent_notes_append("voice", f"line {i} " + "z" * 60))
        self.tool.valves.MAX_TAGGED_CHARS = 1000
        voice = self.read("speech", verbose=False)["notes"][0]
        self.assertGreater(voice["entries_not_shown"], 0)

    def test_summary_is_included(self):
        self.run_async(self.tool.agent_notes_update_summary("voice", "how she talks"))
        self.assertEqual(self.read("speech")["notes"][0]["summary"], "how she talks")

    def test_budget_never_cuts_a_string_and_reports_what_was_left_out(self):
        for i in range(8):
            self.run_async(self.tool.agent_notes_append("voice", f"line {i} " + "x" * 300))
        self.tool.valves.MAX_TAGGED_CHARS = 1200
        res = self.read("identity, speech")
        self.assertLessEqual(len(str(res["notes"])), 1500)
        for note in res["notes"]:
            for e in note["entries"]:
                self.assertTrue(e["text"] in ("speaks softly", "born in 1990") or e["text"].endswith("x" * 300))  # whole entries only
        voice = res["notes"][0]
        self.assertGreater(voice["entries_not_shown"], 0)
        self.assertEqual([e["entry_no"] for e in voice["entries"]], sorted(e["entry_no"] for e in voice["entries"]))  # time order
        self.assertIn("agent_notes_read", res["note"])

    def test_one_long_note_cannot_starve_the_others(self):
        for i in range(10):
            self.run_async(self.tool.agent_notes_append("voice", f"v{i} " + "y" * 400))
        self.tool.valves.MAX_TAGGED_CHARS = 1500
        res = self.read("identity")
        history = next(n for n in res["notes"] if n["note_name"] == "history")
        self.assertEqual([e["text"] for e in history["entries"]], ["born in 1990"])

    def test_notes_that_do_not_fit_are_named(self):
        notes = [{"note_name": n, "tags": ["t"], "matched_tags": ["t"], "summary": None, "entries": [{"entry_no": 1, "text": "hi"}]} for n in ("a", "b", "c")]
        packed, left_out = mod.pack_tagged(notes, 200)
        self.assertEqual([n["note_name"] for n in packed], ["a"])
        self.assertEqual(left_out, ["b", "c"])


class VerboseTests(NotesTestCase):
    def setUp(self):
        super().setUp()
        self.create("n", "needle text")
        self.run_async(self.tool.agent_notes_update_summary("n", "needle summary", "Nightly"))

    def test_read_plain_keeps_entry_numbers_but_drops_bookkeeping(self):
        res = self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual(res["entries"], [{"entry_no": 1, "text": "needle text"}])
        self.assertNotIn("started_by", res)
        self.assertEqual(res["summary"], {"text": "needle summary", "stale": False})

    def test_read_verbose_has_everything(self):
        res = self.run_async(self.tool.agent_notes_read("n", verbose=True))
        self.assertEqual(set(res["entries"][0]), {"entry_no", "author", "created_at", "text", "hits", "weight"})
        self.assertIn("summarized_by", res["summary"])
        self.assertIn("started_by", res)

    def test_search_plain_and_verbose(self):
        plain = self.run_async(self.tool.agent_notes_search("needle"))["cards"][0]
        self.assertEqual(set(plain), {"note_name", "entry_no", "weight", "date", "snippet"})
        self.assertIn("author", self.run_async(self.tool.agent_notes_search("needle", verbose=True))["cards"][0])

    def test_search_summary_plain_keeps_staleness_only(self):
        plain = self.run_async(self.tool.agent_notes_search_summary("needle"))["results"][0]
        self.assertEqual(set(plain), {"note_name", "summary", "stale"})

    def test_list_plain_drops_author_and_time_columns(self):
        plain = self.run_async(self.tool.agent_notes_list())["table"].splitlines()[0]
        self.assertNotIn("Started by", plain)
        self.assertNotIn("Last updated", plain)
        self.assertIn("Started by", self.run_async(self.tool.agent_notes_list(verbose=True))["table"])


class UpdateAuthorTests(NotesTestCase):
    def setUp(self):
        super().setUp()
        self.run_async(self.tool.agent_notes_create("n", "first", author_name="Alice"))

    def started_by(self):
        return self.run_async(self.tool.agent_notes_read("n", verbose=True))["started_by"]

    def test_author_can_be_changed_and_entries_keep_theirs(self):
        res = self.run_async(self.tool.agent_notes_update("n", author_name="Bob"))
        self.assertEqual((res["success"], res["author"]), (True, "Bob"))
        self.assertEqual(self.started_by(), "Bob")
        entry = self.run_async(self.tool.agent_notes_read("n", verbose=True))["entries"][0]
        self.assertEqual(entry["author"], "Alice")

    def test_author_match_ignores_case_and_reuses_the_author_row(self):
        self.run_async(self.tool.agent_notes_update("n", author_name="alice"))
        self.assertEqual(self.started_by(), "Alice")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_author WHERE lower(author_name) = 'alice'"), [(1,)])

    def test_changing_only_the_author_does_not_stale_a_summary_or_move_updated_at(self):
        self.run_async(self.tool.agent_notes_update_summary("n", "about things"))
        before = self.sql("SELECT updated_at FROM note_id")
        self.run_async(self.tool.agent_notes_update("n", author_name="Bob"))
        self.assertEqual(self.sql("SELECT updated_at FROM note_id"), before)
        self.tool.valves.MIN_SUMMARY_CHARS = 0
        self.assertEqual(self.run_async(self.tool.agent_notes_list(needs_summary=True))["count"], 0)

    def test_combined_with_rename_and_validation(self):
        res = self.run_async(self.tool.agent_notes_update("n", new_name="m", author_name="Bob"))
        self.assertEqual((res["note_name"], res["author"]), ("m", "Bob"))
        bad = self.run_async(self.tool.agent_notes_update("m", author_name="x" * 61))
        self.assertFalse(bad["success"])

    def test_nothing_to_update_mentions_author(self):
        self.assertIn("author_name", self.run_async(self.tool.agent_notes_update("n"))["error"])


class ShortNoteTests(NotesTestCase):
    def setUp(self):
        super().setUp()
        self.create("tiny", "a few words")
        self.create("big", "x" * 400)
        self.run_async(self.tool.agent_notes_append("big", "y" * 400))
        self.tool.valves.MIN_SUMMARY_CHARS = 500

    def table(self, **kw):
        return self.run_async(self.tool.agent_notes_list(**kw))

    def test_short_note_is_labelled_and_not_offered_for_summarizing(self):
        self.assertIn("| tiny | 1 | short |", self.table()["table"])
        todo = self.table(needs_summary=True)
        self.assertEqual(todo["count"], 1)
        self.assertIn("big", todo["table"])
        self.assertNotIn("tiny", todo["table"])

    def test_nothing_to_do_when_only_short_notes_lack_summaries(self):
        self.run_async(self.tool.agent_notes_update_summary("big", "two long entries"))
        self.assertEqual(self.table(needs_summary=True)["count"], 0)

    def test_growing_past_the_threshold_makes_it_summarizable(self):
        self.run_async(self.tool.agent_notes_append("tiny", "z" * 500))
        self.assertIn("tiny", self.table(needs_summary=True)["table"])

    def test_zero_turns_the_threshold_off(self):
        self.tool.valves.MIN_SUMMARY_CHARS = 0
        self.assertEqual(self.table(needs_summary=True)["count"], 2)


class OpenWebUICoercionTests(unittest.TestCase):
    def test_no_method_is_annotated_int(self):
        """Open WebUI int()s a string for an int-annotated parameter before our code runs (so "" or "#3" would fail)."""
        import inspect
        for name, fn in inspect.getmembers(mod.Tools, inspect.iscoroutinefunction):
            for pname, param in inspect.signature(fn).parameters.items():
                self.assertNotIn(param.annotation, (int, Optional[int]), f"{name}({pname})")


class PinnedTests(NotesTestCase):
    """Entries the owner pins (web interface only) always come back, outside every quota and size limit, and a model is never told."""

    def fill(self, name="n", count=30, weight=None, kind=None):
        self.create(name, "entry 1")
        for i in range(2, count + 1):
            self.run_async(self.tool.agent_notes_append(name, f"entry {i}", weight=weight))
        if kind:
            conn = sqlite3.connect(self.db)
            conn.execute("UPDATE note_id SET kind = ? WHERE lower(note_name) = ?", (kind, name))
            conn.commit()
            conn.close()

    def pin(self, name, no, on=True):
        return mod.set_pinned_db(self.db, name, no, on)

    def texts(self, out):
        return [e["text"] for e in out["entries"]] + [e["text"] for rows in out.get("strong_memories", {}).values() for e in rows]

    def test_column_is_added_to_an_older_database(self):
        self.create("n")
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE note_data DROP COLUMN pinned")
        conn.commit()
        conn.close()
        mod._schema_ready.clear()
        self.assertEqual(self.pin("n", 1)["entries_changed"], 1)
        self.assertEqual(self.sql("SELECT pinned FROM note_data"), [(1,)])

    def test_pinned_old_entry_is_in_the_default_read_and_unmarked(self):
        self.fill()
        self.pin("n", 2)
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertIn("entry 2", self.texts(out))
        self.assertEqual(len(out["entries"]), 10)
        self.assertNotIn("pinned", json.dumps(out).lower())
        self.assertNotIn("pinned", json.dumps(self.run_async(self.tool.agent_notes_read("n", verbose=True))).lower())

    def test_unpinned_old_entry_is_not(self):
        self.fill()
        self.pin("n", 2)
        self.pin("n", 2, False)
        self.assertNotIn("entry 2", self.texts(self.run_async(self.tool.agent_notes_read("n"))))
        self.assertEqual(self.sql("SELECT pinned FROM note_data WHERE entry_no = 2"), [(None,)])

    def test_pinned_entry_ignores_the_strong_quota_and_does_not_use_a_slot(self):
        self.tool.valves.READ_STRONG_EACH = 2
        self.fill(count=20)
        for no in (2, 3, 4, 5):
            self.run_async(self.tool.agent_notes_set_weight("n", str(no), weight="9"))
        self.pin("n", 2)
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual(len(out["strong_memories"]["positive"]), 3)  # the pinned one plus the usual two
        self.assertIn("entry 2", self.texts(out))
        self.tool.valves.READ_STRONG_EACH = 0
        self.assertIn("entry 2", self.texts(self.run_async(self.tool.agent_notes_read("n"))))  # even with the strong tier off

    def test_pinned_entry_goes_on_the_side_of_its_score(self):
        self.fill(count=20)
        self.run_async(self.tool.agent_notes_set_weight("n", "2", weight="1"))
        self.pin("n", 2)
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual([e["text"] for e in out["strong_memories"]["negative"]], ["entry 2"])

    def test_pinned_entry_in_a_persona_note_is_a_trait_shown(self):
        self.fill(count=20, kind="persona")
        self.pin("n", 3)
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual([e["text"] for e in out["strong_memories"]["positive"]], ["entry 3"])

    def test_pinned_entry_survives_the_size_limit(self):
        self.tool.valves.READ_MAX_CHARS = 500
        self.create("n", "start")
        for i in range(2, 12):
            self.run_async(self.tool.agent_notes_append("n", f"entry {i} " + "x" * 400))
        self.pin("n", 1)
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertIn("start", self.texts(out))
        self.assertTrue(any(e["text"].startswith("entry 11") for e in out["entries"]))  # the newest is still there

    def test_other_read_modes_are_unchanged(self):
        self.fill()
        self.pin("n", 2)
        out = self.run_async(self.tool.agent_notes_read("n", before="20", limit="5"))
        self.assertNotIn("entry 2", self.texts(out))

    def test_read_tagged_keeps_a_pinned_entry_the_budget_would_drop(self):
        self.tool.valves.MAX_TAGGED_CHARS = 800
        self.create("n", "oldest special entry")
        for i in range(2, 15):
            self.run_async(self.tool.agent_notes_append("n", f"entry {i} " + "y" * 150))
        self.run_async(self.tool.agent_notes_add_tags("n", "topic"))
        before = self.run_async(self.tool.agent_notes_read_tagged("topic"))
        self.assertNotIn("oldest special entry", json.dumps(before))
        self.pin("n", 1)
        out = self.run_async(self.tool.agent_notes_read_tagged("topic"))
        self.assertIn("oldest special entry", json.dumps(out))
        self.assertEqual(out["notes"][0]["entries"][0], "oldest special entry")  # time order is kept
        self.assertNotIn("pinned", json.dumps(self.run_async(self.tool.agent_notes_read_tagged("topic", verbose=True))).lower())

    def test_read_tagged_note_over_budget_is_still_admitted_for_its_pinned_entry(self):
        self.tool.valves.MAX_TAGGED_CHARS = 500
        for name in ("a", "b"):
            self.create(name, "first of " + name + " " + "z" * 200)
            self.run_async(self.tool.agent_notes_add_tags(name, "topic"))
        self.pin("b", 1)
        out = self.run_async(self.tool.agent_notes_read_tagged("topic"))
        self.assertIn("b", [n["note_name"] for n in out["notes"]])

    def test_search_always_shows_a_matching_pinned_entry(self):
        self.create("n", "apple pie")
        for i in range(2, 8):
            self.run_async(self.tool.agent_notes_append("n", f"apple entry {i}", weight="9"))
        self.pin("n", 1)
        out = self.run_async(self.tool.agent_notes_search("apple", limit="2"))
        self.assertEqual(len(out["cards"]), 3)
        self.assertIn(1, [c["entry_no"] for c in out["cards"]])
        self.assertEqual(out["showing"], "1-2 of 7")
        self.assertIn("5 more match", out["note"])
        self.assertNotIn("pinned", json.dumps(out).lower())
        page2 = self.run_async(self.tool.agent_notes_search("apple", limit="2", page="2"))
        self.assertEqual(len(page2["cards"]), 2)

    def test_search_does_not_show_a_pinned_entry_that_does_not_match(self):
        self.create("n", "pear tart")
        self.pin("n", 1)
        out = self.run_async(self.tool.agent_notes_search("apple"))
        self.assertEqual(out["cards"], [])

    def test_pinning_one_piece_of_a_chain_pins_them_all(self):
        self.run_async(self.tool.agent_notes_create("n", "word " * 300, continues=True))
        pieces = self.sql("SELECT COUNT(*) FROM note_data")[0][0]
        self.assertGreater(pieces, 1)
        self.assertEqual(self.pin("n", 2)["entries_changed"], pieces)
        self.pin("n", 2, False)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data WHERE pinned IS NOT NULL"), [(0,)])

    def test_editing_an_entry_keeps_its_pin(self):
        self.create("n", "before")
        self.pin("n", 1)
        self.run_async(self.tool.agent_notes_edit_entry("n", "1", "after"))
        self.assertEqual(self.sql("SELECT pinned FROM note_data"), [(1,)])

    def test_no_tool_method_can_pin(self):
        self.assertFalse([m for m in dir(self.tool) if "pin" in m.lower()])

    # ---- the lock: a model's changes to a pinned entry are quietly dropped ----
    def two(self):
        self.create("n", "pinned text")
        self.run_async(self.tool.agent_notes_append("n", "plain text"))
        self.pin("n", 1)

    def test_edit_of_a_pinned_entry_looks_successful_and_changes_nothing(self):
        self.two()
        locked = self.run_async(self.tool.agent_notes_edit_entry("n", "1", "rewritten", weight="9"))
        plain = self.run_async(self.tool.agent_notes_edit_entry("n", "2", "rewritten", weight="9"))
        self.assertTrue(locked["success"])
        self.assertEqual(set(locked), set(plain))
        self.assertEqual(self.sql("SELECT note_text, weight, edited_at FROM note_data WHERE entry_no = 1"), [("pinned text", 50, None)])
        self.assertEqual(self.sql("SELECT note_text, weight FROM note_data WHERE entry_no = 2"), [("rewritten", 90)])

    def test_delete_of_a_pinned_entry_looks_successful_and_changes_nothing(self):
        self.two()
        locked = self.run_async(self.tool.agent_notes_delete_entry("n", "1"))
        self.assertEqual((locked["success"], locked["remaining"]), (True, 1))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data"), [(2,)])
        self.assertEqual(self.sql("SELECT pinned FROM note_data WHERE entry_no = 1"), [(1,)])

    def test_reweighting_a_pinned_entry_looks_successful_and_changes_nothing(self):
        self.two()
        locked = self.run_async(self.tool.agent_notes_set_weight("n", "1", weight="9"))
        plain = self.run_async(self.tool.agent_notes_set_weight("n", "2", weight="9"))
        self.assertEqual(locked, {**plain, "entry_no": 1})
        self.assertEqual(self.sql("SELECT weight FROM note_data ORDER BY entry_no"), [(50,), (90,)])

    def test_the_lock_does_not_bind_the_owner(self):
        self.two()
        mod.edit_entry_db(self.db, "n", 1, "owner edit", 8)
        self.assertEqual(self.sql("SELECT note_text, weight FROM note_data WHERE entry_no = 1"), [("owner edit", 80)])
        mod.set_weight_db(self.db, "n", 1, 2, None)
        self.assertEqual(self.sql("SELECT weight FROM note_data WHERE entry_no = 1"), [(20,)])
        mod.delete_entry_db(self.db, "n", 1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data"), [(1,)])

    def test_unpinning_releases_the_lock(self):
        self.two()
        self.pin("n", 1, False)
        self.run_async(self.tool.agent_notes_edit_entry("n", "1", "now editable"))
        self.assertEqual(self.sql("SELECT note_text FROM note_data WHERE entry_no = 1"), [("now editable",)])

    def test_a_locked_edit_still_reports_a_missing_entry_and_bad_text(self):
        self.two()
        self.assertFalse(self.run_async(self.tool.agent_notes_edit_entry("n", "9", "x"))["success"])
        self.assertFalse(self.run_async(self.tool.agent_notes_edit_entry("n", "1", "x" * 501))["success"])

    def test_bad_entry_number(self):
        self.create("n")
        with self.assertRaises(mod.NoteError):
            self.pin("n", 9)


class VerboseFieldsTests(unittest.TestCase):
    """The VERBOSE_FIELDS valve: by default verbose=true adds only timestamps."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "n.sqlite3")
        self.tool = mod.Tools()
        self.tool.valves.NOTES_DB_PATH = self.db
        self.tool.valves.STRONG_SPREAD = False
        self.tool.valves.RESULT_FORMAT = "pretty"
        self.run = asyncio.run
        self.run(self.tool.agent_notes_create("n", "first entry", "a note", author_name="Alice"))
        self.run(self.tool.agent_notes_append("n", "second entry about needles", author_name="Bob", weight="9"))
        self.run(self.tool.agent_notes_add_tags("n", "topic")) if hasattr(self.tool, "agent_notes_add_tags") else None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_timestamps_only(self):
        self.assertEqual(self.tool.valves.VERBOSE_FIELDS, "created_at")
        out = self.run(self.tool.agent_notes_read("n", verbose=True))
        e = out["entries"][0]
        self.assertEqual(set(e), {"entry_no", "created_at", "text"})
        self.assertEqual(set(out["entries"][1]), {"entry_no", "created_at", "text", "weight"})  # a weight someone set is still shown
        self.assertNotIn("started_by", out)
        self.assertNotIn("hits", out)

    def test_plain_read_is_unchanged(self):
        e = self.run(self.tool.agent_notes_read("n"))["entries"][0]
        self.assertEqual(set(e), {"entry_no", "text"})

    def test_each_field_can_be_switched_on(self):
        self.tool.valves.VERBOSE_FIELDS = "author, HITS , weight, nonsense"
        out = self.run(self.tool.agent_notes_read("n", verbose=True))
        self.assertEqual(set(out["entries"][0]), {"entry_no", "author", "text", "hits", "weight"})
        self.assertEqual(out["started_by"], "Alice")
        self.assertIn("hits", out)
        self.assertNotIn("created_at", out["entries"][0])

    def test_blank_valve_means_plain(self):
        self.tool.valves.VERBOSE_FIELDS = ""
        self.assertEqual(set(self.run(self.tool.agent_notes_read("n", verbose=True))["entries"][0]), {"entry_no", "text"})

    def test_read_tagged_verbose_follows_the_valve(self):
        note = self.run(self.tool.agent_notes_read_tagged("topic", verbose=True))["notes"][0]
        self.assertEqual(set(note["entries"][0]), {"entry_no", "created_at", "text"})
        self.assertNotIn("hits", note)
        self.tool.valves.VERBOSE_FIELDS = "author,created_at,hits,weight"
        note = self.run(self.tool.agent_notes_read_tagged("topic", verbose=True))["notes"][0]
        self.assertEqual(set(note["entries"][0]), {"entry_no", "author", "created_at", "text", "hits", "weight"})
        self.assertIn("hits", note)

    def test_search_and_rank_verbose_follow_the_valve(self):
        card = self.run(self.tool.agent_notes_search("needles", verbose=True))["cards"][0]
        self.assertNotIn("author", card)
        self.assertNotIn("hits", card)
        item = self.run(self.tool.agent_notes_rank_entries("n", "strongest", verbose=True))["entries"][0]
        self.assertEqual(set(item), {"entry_no", "text", "weight", "hits", "created_at"})
        self.tool.valves.VERBOSE_FIELDS = "author,hits"
        card = self.run(self.tool.agent_notes_search("needles", verbose=True))["cards"][0]
        self.assertEqual((card["author"], "hits" in card), ("Bob", True))

    def test_the_timestamp_is_what_recent_means(self):
        out = self.run(self.tool.agent_notes_read("n", verbose=True))
        self.assertTrue(all(len(e["created_at"]) > 20 for e in out["entries"]))


class ResultFormatTests(unittest.TestCase):
    """RESULT_FORMAT: Open WebUI indents a dict reply by two spaces; a string passes through, so the tool can send it leaner."""

    SAMPLE = {"success": True, "note_name": "n", "tags": ["a", "b"], "entries": [{"entry_no": 1, "text": "one \u2014 \"quoted\""}, {"entry_no": 2, "text": "two"}],
              "strong_memories": {"positive": [{"entry_no": 3, "text": "x" * 100}], "negative": []}, "note": "ok"}

    def test_pretty_leaves_the_dict_alone(self):
        self.assertIs(mod.render_result(self.SAMPLE, "pretty"), self.SAMPLE)
        self.assertIs(mod.render_result(self.SAMPLE, ""), self.SAMPLE)
        self.assertIs(mod.render_result(self.SAMPLE, "nonsense"), self.SAMPLE)

    def test_every_format_is_the_same_json(self):
        for mode in ("lines", "compact"):
            out = mod.render_result(self.SAMPLE, mode)
            self.assertIsInstance(out, str)
            self.assertEqual(json.loads(out), self.SAMPLE)
        self.assertEqual(json.loads(mod.render_result([1, {"a": 2}], "lines")), [1, {"a": 2}])

    def test_compact_is_one_line_and_lines_puts_each_entry_on_its_own(self):
        self.assertNotIn("\n", mod.render_result(self.SAMPLE, "compact"))
        out = mod.render_result(self.SAMPLE, "lines")
        self.assertIn('{"entry_no":1,"text":"one \u2014 \\"quoted\\""},\n{"entry_no":2,"text":"two"}', out)
        self.assertEqual(out.splitlines()[0], '{"success":true,')
        self.assertNotIn("  ", out)  # no indentation
        self.assertLess(len(out), len(json.dumps(self.SAMPLE, indent=2, ensure_ascii=False)))

    def test_non_dict_results_pass_through(self):
        self.assertEqual(mod.render_result("text", "lines"), "text")
        self.assertIsNone(mod.render_result(None, "compact"))

    def test_tool_methods_honour_the_valve_and_keep_their_specs(self):
        import inspect
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tool = mod.Tools()
        tool.valves.NOTES_DB_PATH = str(Path(tmp) / "n.sqlite3")
        self.assertEqual(tool.valves.RESULT_FORMAT, "lines")
        created = asyncio.run(tool.agent_notes_create("n", "one"))
        self.assertIsInstance(created, str)
        self.assertTrue(json.loads(created)["success"])
        tool.valves.RESULT_FORMAT = "pretty"
        self.assertIsInstance(asyncio.run(tool.agent_notes_append("n", "two")), dict)
        # what Open WebUI builds a tool's spec and extra parameters from must still be there on the wrapped method
        sig = inspect.signature(tool.agent_notes_create)
        self.assertIn("continues", sig.parameters)
        self.assertIn("__model__", sig.parameters)
        self.assertTrue(inspect.iscoroutinefunction(tool.agent_notes_create))
        self.assertIn("Start a new note", tool.agent_notes_create.__doc__)
        self.assertEqual(tool.agent_notes_create.__name__, "agent_notes_create")
        self.assertIn("text", typing.get_type_hints(tool.agent_notes_create))

    def test_curation_tool_has_no_valve_and_keeps_open_webuis_formatting(self):
        import agent_notes_curation as cur
        self.assertNotIn("RESULT_FORMAT", cur.Tools().valves.model_dump())
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tool = cur.Tools()
        tool.valves.NOTES_DB_PATH = str(Path(tmp) / "n.sqlite3")
        self.assertIsInstance(asyncio.run(tool.agent_notes_delete("missing")), dict)


if __name__ == "__main__":
    unittest.main()


class NoteSettingsTests(NotesTestCase):
    """note_id.kind and note_id.summary_max: set by the curation tool's agent_notes_update, defaults for everything else."""
    def update(self, name="n", **kw):
        return self.run_async(self.tool.agent_notes_update(name, **kw))

    def summarize(self, text, name="n"):
        return self.run_async(self.tool.agent_notes_update_summary(name, text))

    def test_defaults_and_validation(self):
        self.create("n", "x")
        self.assertEqual(self.sql("SELECT kind, summary_max FROM note_id"), [("memory", None)])
        for bad in ({"kind": "feelings"}, {"summary_max": "50"}, {"summary_max": "5000"}, {"summary_max": "lots"}):
            self.assertFalse(self.update(**bad)["success"], bad)
        self.assertFalse(self.update()["success"])  # nothing to change

    def test_set_and_reset(self):
        self.create("n", "x")
        got = self.update(kind="Persona", summary_max="1000")
        self.assertEqual((got["success"], got["kind"], got["summary_limit"]), (True, "persona", 1000))
        self.assertEqual(self.sql("SELECT kind, summary_max FROM note_id"), [("persona", 1000)])
        got = self.update(summary_max="reset")
        self.assertEqual(got["summary_limit"], 500)
        self.assertEqual(self.sql("SELECT kind, summary_max FROM note_id"), [("persona", None)])

    def test_changing_them_does_not_make_the_summary_stale(self):
        self.create("n", "x")
        self.summarize("a summary")
        self.update(kind="persona", summary_max="800")
        self.assertEqual(self.run_async(self.tool.agent_notes_list(needs_summary=True))["count"], 0)

    def test_resending_the_same_name_and_comment_does_not_make_the_summary_stale(self):
        self.create("n", "x", "same")
        self.summarize("a summary")
        self.update(new_name="n", comment="same", kind="persona")
        self.assertEqual(self.run_async(self.tool.agent_notes_list(needs_summary=True))["count"], 0)
        self.update(comment="different")  # a real change still does
        self.assertEqual(self.run_async(self.tool.agent_notes_list(needs_summary=True))["count"], 1)

    def test_summary_limit_is_per_note(self):
        self.create("n", "x")
        self.create("m", "x")
        self.update(summary_max="1000")
        self.assertTrue(self.summarize("y" * 900)["success"])
        refused = self.summarize("y" * 900, name="m")
        self.assertFalse(refused["success"])
        self.assertIn("limit is 500", refused["error"])
        self.assertFalse(self.summarize("y" * 1001)["success"])

    def test_list_shows_limit_and_kind_only_when_they_differ(self):
        self.create("n", "x")
        self.create("m", "x")
        plain = self.run_async(self.tool.agent_notes_list())["table"]
        self.assertNotIn("Kind", plain)
        self.assertNotIn("up to", plain)
        self.update(kind="persona", summary_max="1000")
        shown = self.run_async(self.tool.agent_notes_list())["table"]
        self.assertIn("| Kind |", shown)
        self.assertIn("| persona |", shown)
        self.assertIn("none, up to 1000", shown)
        self.assertEqual(shown.count("up to"), 1)

    def test_persona_read_has_no_negative_side(self):
        self.create("n", "e1")
        for i in range(2, 9):
            self.run_async(self.tool.agent_notes_append("n", f"e{i}", weight="2" if i == 3 else "9" if i == 4 else None))
        self.tool.valves.READ_DEFAULT_RECENT = 2
        before = self.run_async(self.tool.agent_notes_read("n"))["strong_memories"]
        self.assertEqual(([e["entry_no"] for e in before["positive"]], [e["entry_no"] for e in before["negative"]]), ([4], [3]))
        self.update(kind="persona")
        after = self.run_async(self.tool.agent_notes_read("n"))["strong_memories"]
        self.assertEqual(([e["entry_no"] for e in after["positive"]], "negative" in after and after["negative"]), ([4], False))

    def test_persona_notes_pack_strongest_first_in_a_tight_tagged_read(self):
        self.create("p", "plain one")
        for text, weight in (("core trait", "9"), ("passing mood", "2"), ("middling", None), ("another core", "8")):
            self.run_async(self.tool.agent_notes_append("p", text, weight=weight))
        self.run_async(self.tool.agent_notes_add_tags("p", "facet", True))
        self.update(name="p", kind="persona")
        self.tool.valves.MAX_TAGGED_CHARS = 500
        got = self.run_async(self.tool.agent_notes_read_tagged("facet"))["notes"][0]
        self.assertEqual(got["entries"][0]["text"], "core trait")  # heaviest first, whatever the budget cut
        self.assertEqual({e["text"] if isinstance(e, dict) else e for e in got["entries"][:2]}, {"core trait", "another core"})
        self.tool.valves.MAX_TAGGED_CHARS = 6000
        full = self.run_async(self.tool.agent_notes_read_tagged("facet"))["notes"][0]
        texts = [e["text"] if isinstance(e, dict) else e for e in full["entries"]]
        self.assertEqual(texts[:2], ["core trait", "another core"])
        self.assertEqual(texts[-1], "passing mood")


class SettingsMigrationTests(NotesTestCase):
    def test_old_database_gets_columns_and_a_longer_summary_allowed(self):
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE note_id (note_pk INTEGER PRIMARY KEY AUTOINCREMENT, note_name TEXT NOT NULL, note_comment TEXT NOT NULL DEFAULT '',
                next_entry_no INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE note_data (note_pk INTEGER NOT NULL, entry_no INTEGER NOT NULL, note_text TEXT NOT NULL, created_at TEXT NOT NULL,
                edited_at TEXT, PRIMARY KEY (note_pk, entry_no));
            CREATE TABLE note_summary (note_pk INTEGER PRIMARY KEY REFERENCES note_id(note_pk) ON DELETE CASCADE,
                summary_text TEXT NOT NULL CHECK (length(summary_text) BETWEEN 1 AND 500), author_id INTEGER,
                last_summarized TEXT NOT NULL DEFAULT 'x', source_updated_at TEXT NOT NULL, source_entries INTEGER NOT NULL);
            INSERT INTO note_id VALUES (1, 'old', '', 2, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            INSERT INTO note_data VALUES (1, 1, 'text', '2026-01-01T00:00:00+00:00', NULL);
            INSERT INTO note_summary VALUES (1, 'kept summary', NULL, 'x', '2026-01-01T00:00:00+00:00', 1);
        """)
        conn.close()
        self.run_async(self.tool.agent_notes_list())
        self.assertEqual(self.sql("SELECT kind, summary_max FROM note_id"), [("memory", None)])
        self.assertEqual(self.sql("SELECT summary_text FROM note_summary"), [("kept summary",)])
        self.run_async(self.tool.agent_notes_update("old", summary_max="1000"))
        self.assertTrue(self.run_async(self.tool.agent_notes_update_summary("old", "z" * 900))["success"])
        mod._schema_ready.discard(self.db)  # a second process migrating again changes nothing
        self.run_async(self.tool.agent_notes_list())
        self.assertEqual(self.sql("SELECT length(summary_text) FROM note_summary"), [(900,)])
