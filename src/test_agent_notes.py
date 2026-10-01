"""
Tests for agent_notes.py. Run with: python -m unittest test_agent_notes -v
(no Open WebUI server needed - it's just SQLite in a temp directory).
"""

import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import agent_notes as mod


class NotesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "nested" / "agent_notes.sqlite3")  # parent dir doesn't exist yet
        self.tool = mod.Tools()
        self.tool.valves.NOTES_DB_PATH = self.db

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
        return self.run_async(self.tool.create_note(name, text, comment))


class CreateAndReadTests(NotesTestCase):
    def test_create_then_read_round_trips(self):
        res = self.create("Character Bios", "Ada is a cartographer.", "who's who")
        self.assertTrue(res["success"])
        self.assertEqual(res["entry_no"], 1)
        read = self.run_async(self.tool.read_note("character bios"))  # case-insensitive lookup
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
        self.assertIn("append_note", res["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_id"), [(1,)])

    def test_name_whitespace_is_normalized(self):
        self.create("  my   note \n")
        read = self.run_async(self.tool.read_note("my note"))
        self.assertEqual(read["note_name"], "my note")

    def test_blank_comment_sentinels_mean_no_comment(self):
        for sentinel in ("", None, "none"):
            with self.subTest(sentinel=sentinel):
                self.create(f"n-{sentinel}", "x", sentinel)
        self.assertEqual({r[0] for r in self.sql("SELECT note_comment FROM note_id")}, {""})

    def test_read_missing_note_suggests_close_names(self):
        self.create("character-bios")
        res = self.run_async(self.tool.read_note("character-bio"))
        self.assertFalse(res["success"])
        self.assertIn("character-bios", res["error"])

    def test_read_missing_note_when_none_exist(self):
        res = self.run_async(self.tool.read_note("anything"))
        self.assertFalse(res["success"])
        self.assertIn("create_note", res["error"])


class AppendTests(NotesTestCase):
    def test_append_adds_numbered_entries_in_order(self):
        self.create("log", "one")
        r2 = self.run_async(self.tool.append_note("log", "two"))
        r3 = self.run_async(self.tool.append_note("LOG", "three"))
        self.assertEqual((r2["entry_no"], r3["entry_no"]), (2, 3))
        read = self.run_async(self.tool.read_note("log"))
        self.assertEqual([(e["entry_no"], e["text"]) for e in read["entries"]], [(1, "one"), (2, "two"), (3, "three")])
        self.assertEqual(read["total_entries"], 3)

    def test_append_to_missing_note_fails_without_creating_it(self):
        res = self.run_async(self.tool.append_note("ghost", "boo"))
        self.assertFalse(res["success"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_id"), [(0,)])

    def test_read_limit_returns_most_recent_in_chronological_order(self):
        self.create("log", "e1")
        for i in range(2, 6):
            self.run_async(self.tool.append_note("log", f"e{i}"))
        read = self.run_async(self.tool.read_note("log", 2))
        self.assertEqual([e["text"] for e in read["entries"]], ["e4", "e5"])
        self.assertEqual(read["total_entries"], 5)
        self.assertIn("most recent", read["note"])

    def test_read_limit_is_capped_by_valve(self):
        self.tool.valves.MAX_READ_ENTRIES = 2
        self.create("log", "e1")
        for i in range(2, 5):
            self.run_async(self.tool.append_note("log", f"e{i}"))
        read = self.run_async(self.tool.read_note("log", 99))
        self.assertEqual(len(read["entries"]), 2)


class EditAndDeleteEntryTests(NotesTestCase):
    def setUp(self):
        super().setUp()
        self.create("log", "one")
        self.run_async(self.tool.append_note("log", "two"))
        self.run_async(self.tool.append_note("log", "three"))

    def test_edit_replaces_text_keeps_number_and_marks_edited(self):
        res = self.run_async(self.tool.edit_entry("log", 2, "TWO"))
        self.assertTrue(res["success"])
        read = self.run_async(self.tool.read_note("log"))
        by_no = {e["entry_no"]: e for e in read["entries"]}
        self.assertEqual(by_no[2]["text"], "TWO")
        self.assertIn("edited_at", by_no[2])
        self.assertNotIn("edited_at", by_no[1])
        self.assertEqual([e["entry_no"] for e in read["entries"]], [1, 2, 3])

    def test_entry_no_accepts_hash_and_string_forms(self):
        self.assertTrue(self.run_async(self.tool.edit_entry("log", "#2", "a"))["success"])
        self.assertTrue(self.run_async(self.tool.edit_entry("log", "3", "b"))["success"])

    def test_edit_missing_entry_lists_what_exists(self):
        res = self.run_async(self.tool.edit_entry("log", 9, "x"))
        self.assertFalse(res["success"])
        self.assertIn("1-3", res["error"])

    def test_entry_no_required_and_numeric(self):
        self.assertFalse(self.run_async(self.tool.edit_entry("log", "", "x"))["success"])
        self.assertFalse(self.run_async(self.tool.delete_entry("log", "abc"))["success"])

    def test_delete_entry_removes_only_that_row_and_numbers_are_not_reused(self):
        res = self.run_async(self.tool.delete_entry("log", 3))
        self.assertEqual(res["remaining"], 2)
        appended = self.run_async(self.tool.append_note("log", "four"))
        self.assertEqual(appended["entry_no"], 4)  # not 3 again
        read = self.run_async(self.tool.read_note("log"))
        self.assertEqual([e["entry_no"] for e in read["entries"]], [1, 2, 4])

    def test_editing_bumps_note_to_top_of_list(self):
        self.create("other", "x")
        self.run_async(self.tool.edit_entry("log", 1, "touched"))
        table = self.run_async(self.tool.list_notes())["table"].splitlines()
        self.assertTrue(table[2].startswith("| log |"))


class UpdateNoteTests(NotesTestCase):
    def test_rename_and_comment(self):
        self.create("old", "x", "c1")
        res = self.run_async(self.tool.update_note("old", "new", "c2"))
        self.assertTrue(res["success"])
        read = self.run_async(self.tool.read_note("new"))
        self.assertEqual(read["note_comment"], "c2")
        self.assertFalse(self.run_async(self.tool.read_note("old"))["success"])
        self.assertEqual(len(read["entries"]), 1)  # entries came along

    def test_rename_onto_another_note_is_refused(self):
        self.create("a", "x")
        self.create("b", "y")
        res = self.run_async(self.tool.update_note("a", "B"))
        self.assertFalse(res["success"])

    def test_case_only_rename_of_own_note_is_allowed(self):
        self.create("notes", "x")
        self.assertTrue(self.run_async(self.tool.update_note("notes", "Notes"))["success"])
        self.assertEqual(self.run_async(self.tool.read_note("notes"))["note_name"], "Notes")

    def test_comment_only_update_keeps_name(self):
        self.create("a", "x", "old")
        self.run_async(self.tool.update_note("a", "", "new"))
        read = self.run_async(self.tool.read_note("a"))
        self.assertEqual((read["note_name"], read["note_comment"]), ("a", "new"))

    def test_clear_comment(self):
        self.create("a", "x", "old")
        self.run_async(self.tool.update_note("a", "", "", True))
        self.assertEqual(self.run_async(self.tool.read_note("a"))["note_comment"], "")

    def test_nothing_to_update_and_conflicting_args(self):
        self.create("a", "x")
        self.assertFalse(self.run_async(self.tool.update_note("a", "", "", False))["success"])
        self.assertFalse(self.run_async(self.tool.update_note("a", "", "c", True))["success"])


class DeleteNoteTests(NotesTestCase):
    def test_delete_note_removes_note_and_all_entries(self):
        self.create("doomed", "one")
        self.run_async(self.tool.append_note("doomed", "two"))
        self.create("keeper", "safe")
        res = self.run_async(self.tool.delete_note("DOOMED"))
        self.assertEqual(res["entries_removed"], 2)
        self.assertEqual(self.sql("SELECT note_name FROM note_id"), [("keeper",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM note_data"), [(1,)])  # no orphaned rows

    def test_name_is_reusable_after_delete(self):
        self.create("again", "one")
        self.run_async(self.tool.delete_note("again"))
        self.assertTrue(self.create("again", "fresh")["success"])
        read = self.run_async(self.tool.read_note("again"))
        self.assertEqual([(e["entry_no"], e["text"]) for e in read["entries"]], [(1, "fresh")])

    def test_delete_missing_note_fails(self):
        self.assertFalse(self.run_async(self.tool.delete_note("nope"))["success"])


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
        self.assertFalse(self.run_async(self.tool.append_note("a", "t" * 501))["success"])
        self.assertFalse(self.run_async(self.tool.edit_entry("a", 1, "t" * 501))["success"])
        self.assertIn("split", self.run_async(self.tool.append_note("a", "t" * 501))["error"])

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
        res = self.run_async(self.tool.list_notes())
        self.assertEqual(res["count"], 0)
        self.assertIn("create_note", res["table"])

    def test_list_shows_entry_counts_and_comments_most_recent_first(self):
        self.create("first", "x", "the first")
        self.create("second", "y")
        self.run_async(self.tool.append_note("first", "z"))
        res = self.run_async(self.tool.list_notes(""))  # "" means default
        rows = res["table"].splitlines()[2:]
        self.assertTrue(rows[0].startswith("| first | 2 |"))
        self.assertIn("the first", rows[0])
        self.assertTrue(rows[1].startswith("| second | 1 |"))

    def test_list_limit_reports_truncation(self):
        for i in range(3):
            self.create(f"n{i}", "x")
        res = self.run_async(self.tool.list_notes(2))
        self.assertEqual((res["count"], res["total_notes"]), (2, 3))
        self.assertIn("note", res)

    def test_pipe_in_name_does_not_break_table(self):
        self.create("a|b", "x")
        row = self.run_async(self.tool.list_notes())["table"].splitlines()[2]
        self.assertIn("a\\|b", row)

    def test_search_finds_entries_names_and_comments_case_insensitively(self):
        self.create("dragons", "Wyrmling hatched at dawn", "lore")
        self.create("misc", "buy milk", "dragon-adjacent")
        res = self.run_async(self.tool.search_notes("DRAGON"))
        self.assertEqual({n["note_name"] for n in res["matching_notes"]}, {"dragons", "misc"})
        res = self.run_async(self.tool.search_notes("wyrmling"))
        self.assertEqual([(e["note_name"], e["entry_no"]) for e in res["matching_entries"]], [("dragons", 1)])

    def test_search_treats_like_wildcards_literally(self):
        self.create("a", "100% done")
        self.create("b", "nothing here")
        res = self.run_async(self.tool.search_notes("%"))
        self.assertEqual([e["note_name"] for e in res["matching_entries"]], ["a"])

    def test_search_requires_a_query(self):
        self.assertFalse(self.run_async(self.tool.search_notes(""))["success"])


if __name__ == "__main__":
    unittest.main()
