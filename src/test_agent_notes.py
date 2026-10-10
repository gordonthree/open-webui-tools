"""
Tests for agent_notes.py. Run with: python -m unittest test_agent_notes -v
(no Open WebUI server needed - it's just SQLite in a temp directory).
"""

import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from typing import Optional
from pathlib import Path

import agent_notes as mod


class NotesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "nested" / "agent_notes.sqlite3")  # parent dir doesn't exist yet
        self.tool = mod.Tools()
        self.tool.valves.NOTES_DB_PATH = self.db
        self.tool.valves.MIN_SUMMARY_CHARS = 0  # tests use tiny notes; the short-note rule has its own tests

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
        self.assertIn("most recent", read["note"])

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
        self.assertEqual([(e["note_name"], e["entry_no"]) for e in res["matching_entries"]], [("dragons", 1)])

    def test_search_lists_newest_entries_first(self):
        self.create("a", "needle one")
        self.run_async(self.tool.agent_notes_append("a", "needle two"))
        self.create("b", "needle three")
        self.run_async(self.tool.agent_notes_append("a", "needle four", created_at="2020-01-01"))
        out = self.run_async(self.tool.agent_notes_search("needle"))
        self.assertEqual([e["text"] for e in out["matching_entries"]], ["needle three", "needle two", "needle one", "needle four"])

    def test_continues_splits_long_text_verbatim_into_a_linked_chain(self):
        text = " ".join(f"word{i}" for i in range(300))  # ~2000 characters
        made = self.run_async(self.tool.agent_notes_create("long", text, continues=True))
        self.assertTrue(made["success"], made)
        self.assertEqual(made["entry_numbers"], "1-5")
        more = self.run_async(self.tool.agent_notes_append("long", "tail " * 150, continues=True))
        self.assertEqual(more["entry_numbers"], "6-7")
        entries = self.run_async(self.tool.agent_notes_read("long"))["entries"]
        self.assertTrue(all(len(e["text"]) <= mod.NOTE_TEXT_MAX for e in entries))
        self.assertEqual(" ".join(e["text"] for e in entries[:5]), text)  # the stored text is exactly the words given
        self.assertEqual([e.get("continues_at") for e in entries], [2, 3, 4, 5, None, 7, None])
        self.assertEqual([e.get("continues_from") for e in entries], [None, 1, 2, 3, 4, None, 6])
        self.assertEqual(self.sql("SELECT DISTINCT chain_id FROM note_data WHERE entry_no > 5"), [(6,)])

    def test_ordinary_entries_carry_no_chain_fields_and_search_shows_links(self):
        self.create("n", "plain")
        self.run_async(self.tool.agent_notes_append("n", "needle " * 100, continues=True))
        read = self.run_async(self.tool.agent_notes_read("n"))["entries"]
        self.assertEqual(set(read[0]), {"entry_no", "text"})
        hits = self.run_async(self.tool.agent_notes_search("needle"))["matching_entries"]
        self.assertEqual({h["entry_no"]: (h.get("continues_from"), h.get("continues_at")) for h in hits}, {2: (None, 3), 3: (2, None)})

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

    def test_search_counts_entries_not_notes_and_leaves_updated_at_alone(self):
        self.create("n", "needle")
        before = self.sql("SELECT updated_at FROM note_id")
        self.run_async(self.tool.agent_notes_search("needle"))
        self.assertEqual(self.hits(), (0, [1]))
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
        import agent_notes as m
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

    def test_weight_defaults_to_50_and_can_be_set_or_nudged_within_0_to_100(self):
        self.create("n", "one")
        self.assertEqual(self.sql("SELECT weight FROM note_data")[0][0], 50)
        self.assertEqual(self.run_async(self.tool.agent_notes_set_weight("n", "1", weight="80"))["weight"], 80)
        out = self.run_async(self.tool.agent_notes_set_weight("n", "#1", change="-30"))
        self.assertEqual((out["old_weight"], out["weight"]), (80, 50))
        out = self.run_async(self.tool.agent_notes_set_weight("n", "1", change="+70"))
        self.assertEqual(out["weight"], 100)
        self.assertIn("note", out)  # said it clamped
        for bad in ({"weight": "101"}, {"weight": "-1"}, {}, {"weight": "5", "change": "5"}, {"weight": "high"}):
            self.assertFalse(self.run_async(self.tool.agent_notes_set_weight("n", "1", **bad))["success"], bad)
        self.assertFalse(self.run_async(self.tool.agent_notes_set_weight("n", "7", weight="5"))["success"])

    def test_weight_is_shown_plain_only_when_not_neutral_and_does_not_make_summaries_stale(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_append("n", "two"))
        self.run_async(self.tool.agent_notes_update_summary("n", "about stuff"))
        self.run_async(self.tool.agent_notes_set_weight("n", "2", weight="20"))
        out = self.run_async(self.tool.agent_notes_read("n"))
        self.assertEqual([e.get("weight") for e in out["entries"]], [None, 20])
        self.assertFalse(out["summary"]["stale"])
        self.assertEqual(self.run_async(self.tool.agent_notes_read("n", verbose=True))["entries"][0]["weight"], 50)

    def test_edit_keeps_weight_and_hits(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_set_weight("n", "1", weight="10"))
        self.run_async(self.tool.agent_notes_read("n"))
        self.run_async(self.tool.agent_notes_edit_entry("n", "1", "changed"))
        self.assertEqual(self.sql("SELECT weight, hits FROM note_data"), [(10, 1)])

    def make_ranked(self):
        self.create("n", "e1")
        for t in ("e2", "e3", "e4"):
            self.run_async(self.tool.agent_notes_append("n", t))
        for no, w in (("1", 90), ("2", 20), ("3", 60)):  # e4 stays 50
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
        self.assertEqual(self.rank(by="above_weight", weight="50")[0], [1, 3])
        self.assertEqual(self.rank(by="below_weight", weight="50")[0], [2])
        self.assertEqual(self.rank(by="above_weight", weight="50", limit="1")[0], [1])
        ids, out = self.rank(by="above_weight", weight="50", limit="1")
        self.assertEqual(out["total_matching"], 2)
        self.assertIn("note", out)
        self.assertEqual(self.rank(by="Below Weight", weight="100")[0], [2, 4, 3, 1])
        self.assertEqual(self.rank(by="above_weight", weight="95")[0], [])
        entry = self.run_async(self.tool.agent_notes_rank_entries("n", "most_popular", limit="1"))["entries"][0]
        self.assertEqual((entry["weight"], entry["hits"]), (90, 3))

    def test_rank_arguments_are_checked(self):
        self.make_ranked()
        for kw in ({"by": "newest"}, {"by": "above_weight"}, {"by": "below_weight", "weight": "101"}, {"by": "most_popular", "limit": "0"}):
            self.assertFalse(self.run_async(self.tool.agent_notes_rank_entries("n", **kw))["success"], kw)
        self.assertFalse(self.run_async(self.tool.agent_notes_rank_entries("nope", "most_popular"))["success"])
        self.assertTrue(self.run_async(self.tool.agent_notes_rank_entries("n", "most_popular", weight="999"))["success"])  # weight ignored

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
        self.assertTrue(self.run_async(self.tool.agent_notes_set_weight("old", "1", weight="70"))["success"])

    def test_database_rejects_out_of_range_weight(self):
        self.create("n", "one")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE note_data SET weight = 101")

    def test_list_verbose_shows_note_reads(self):
        self.create("n", "one")
        self.run_async(self.tool.agent_notes_read("n"))
        self.assertIn("| Reads |", self.run_async(self.tool.agent_notes_list(verbose=True))["table"])

    def test_create_append_and_edit_can_set_the_weight(self):
        made = self.run_async(self.tool.agent_notes_create("n", "one", weight="80"))
        self.assertEqual(made["weight"], 80)
        self.run_async(self.tool.agent_notes_append("n", "two"))  # no weight: neutral
        self.assertEqual(self.run_async(self.tool.agent_notes_append("n", "three", weight=" 10 "))["weight"], 10)
        self.run_async(self.tool.agent_notes_append("n", "word " * 300, continues=True, weight="30"))  # 3 pieces
        self.assertEqual([r[0] for r in self.sql("SELECT weight FROM note_data ORDER BY entry_no")], [80, 50, 10, 30, 30, 30])
        out = self.run_async(self.tool.agent_notes_edit_entry("n", "2", "two, revised", weight="95"))
        self.assertEqual(out["weight"], 95)
        self.run_async(self.tool.agent_notes_edit_entry("n", "2", "two, again"))  # no weight: kept
        self.run_async(self.tool.agent_notes_edit_entry("n", "2", "two, again", weight=""))
        self.assertEqual(self.sql("SELECT weight, note_text FROM note_data WHERE entry_no = 2"), [(95, "two, again")])

    def test_bad_weight_on_write_saves_nothing(self):
        self.create("n", "one")
        for call in (
            lambda: self.tool.agent_notes_append("n", "x", weight="101"),
            lambda: self.tool.agent_notes_append("n", "x", weight="-5"),
            lambda: self.tool.agent_notes_append("n", "x", weight="heavy"),
            lambda: self.tool.agent_notes_create("m", "x", weight="500"),
            lambda: self.tool.agent_notes_edit_entry("n", "1", "changed", weight="101"),
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
        self.assertEqual([e["note_name"] for e in res["matching_entries"]], ["a"])

    def test_search_requires_a_query(self):
        self.assertFalse(self.run_async(self.tool.agent_notes_search(""))["success"])


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
        self.assertEqual(self.run_async(self.tool.agent_notes_search("needle", verbose=True))["matching_entries"][0]["author"], "Claude")


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

    def test_summary_whitespace_is_collapsed_and_database_enforces_limit(self):
        self.create("n", "x")
        self.summarize(text="line one\n\nline   two")
        self.assertEqual(self.sql("SELECT summary_text FROM note_summary"), [("line one line two",)])
        with self.assertRaises(sqlite3.IntegrityError):
            conn = sqlite3.connect(self.db)
            try:
                conn.execute("UPDATE note_summary SET summary_text = ?", ("z" * 501,))
            finally:
                conn.close()

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
        plain = self.run_async(self.tool.agent_notes_search("needle"))["matching_entries"][0]
        self.assertEqual(set(plain), {"note_name", "entry_no", "text"})
        self.assertIn("author", self.run_async(self.tool.agent_notes_search("needle", verbose=True))["matching_entries"][0])

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


if __name__ == "__main__":
    unittest.main()
