import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent_notes as notes

import world_brief as wb


def good_brief():
    return "\n".join([
        "CULTURE AND IDEAS", "- Prize: Raye won the Mercury Prize.", "- Novel: Zadie Smith looked back on White Teeth.",
        "ESSAY TO READ", "- Traction: an essay on friction.",
        "SCIENCE AND DISCOVERY", "- Crows: they decide quickly.",
        "GOOD NEWS", "- Rewilding: volunteers plan a river's future.",
        "WORTH KNOWING", "- Mink: Denmark will ban mink farming.",
        "HEAVY THINGS RIGHT NOW",
        "Right now there is a war in Sudan and an Ebola outbreak. Ask for more detail if you want it.",
        "THINGS TO THINK ABOUT", "- What makes a long journey meaningful?", "- Why do adults fear failure?",
    ])


class Cleaning(unittest.TestCase):
    def test_boilerplate_removed(self):
        self.assertEqual(wb.clean("<p>Fungi explained. The post How Fungi Work first appeared on Quanta Magazine.</p>"),
                         "Fungi explained.")

    def test_alarm_quote_dropped_from_headline(self):
        self.assertEqual(wb.drop_alarm_quotes("- Melania Trump brokers truce for Ukraine city 'on brink of disaster': Both sides confirm"),
                         "- Melania Trump brokers truce for Ukraine city: Both sides confirm")
        self.assertEqual(wb.drop_alarm_quotes("- Betty Gilpin seeks 'anti-algorithm' projects: x"), "- Betty Gilpin seeks 'anti-algorithm' projects: x")

    def test_markup_stripped(self):
        out = wb.strip_markup("### CULTURE AND IDEAS\n1. **Raye**: *wins* a prize\n* Item two\n`code`")
        self.assertEqual(out, "CULTURE AND IDEAS\n- Raye: wins a prize\n- Item two\ncode")


class Selection(unittest.TestCase):
    def test_roundtrip(self):
        feeds = [("hard", "BBC World", ["- A story: x"]), ("soft", "NPR Arts", ["- An arts story: y"]),
                 ("essay", "Aeon (essays)", ["- An essay: z"])]
        self.assertEqual(wb.parse_material(wb.render(feeds)), feeds)

    def test_old_file_without_essay_header_still_finds_essays(self):
        feeds = wb.parse_material("##### CULTURE, SCIENCE AND IDEAS #####\n## Aeon (essays)\n- An essay: z")
        self.assertEqual(feeds[0][0], "essay")

    def test_same_story_in_several_feeds_kept_once(self):
        feeds = [("hard", "BBC World", ["- Woman with Ebola in Nigeria confirmed as wife of Swiss diplomat: a"]),
                 ("hard", "NPR", ["- Nigeria confirms Ebola case as outbreak spreads: b"]),
                 ("hard", "Guardian World", ["- Mystery over source of Ebola infection in Nigeria: c"])]
        kept = [ln for _, _, lines in wb.select(feeds) for ln in lines]
        self.assertEqual(len(kept), 1)

    def test_different_stories_survive(self):
        feeds = [("soft", "NPR Arts", ["- Raye wins the Mercury Prize: a", "- Zadie Smith reflects on White Teeth: b"])]
        self.assertEqual(sum(len(ls) for _, _, ls in wb.select(feeds)), 2)

    def test_per_feed_cap_and_hard_total(self):
        lines = [f"- Unique{chr(97 + i) * 6} story{chr(97 + i) * 5} number{chr(97 + i) * 4}: x" for i in range(20)]
        feeds = [("hard", f"Feed{n}", [f"{ln}{n}" for ln in lines]) for n in range(3)]
        chosen = wb.select(feeds)
        self.assertTrue(all(len(ls) <= wb.PER_FEED["hard"] for _, _, ls in chosen))
        self.assertLessEqual(sum(len(ls) for _, _, ls in chosen), wb.HARD_TOTAL)

    def test_aeon_video_dropped_from_essays(self):
        feeds = [("essay", "Aeon (essays)", ["- A film: x - by Aeon Video", "- An essay: y - by Someone"])]
        kept = [ln for _, _, lines in wb.select(feeds) for ln in lines]
        self.assertEqual(kept, ["- An essay: y - by Someone"])

    def test_wikipedia_block_trimmed_and_marked_heavy(self):
        feeds = [("hard", wb.WIKI_EVENTS, ["x" * 4000])]
        group, name, lines = wb.select(feeds)[0]
        self.assertEqual((group, len(lines[0])), ("heavy", wb.WIKI_CHARS))

    def test_hard_news_split_into_calm_and_heavy_with_coverage(self):
        feeds = [("hard", "BBC World", ["- Denmark to ban mink farming: a", "- Nigeria confirms Ebola case: b"]),
                 ("hard", "NPR", ["- Ebola reaches Nigeria first time: c", "- Soldiers killed at checkpoint near Hebron: d"])]
        chosen = {g: ln for g, _, ln in wb.select(feeds)}
        self.assertEqual(chosen["hard"], ["- Denmark to ban mink farming: a"])
        self.assertTrue(chosen["heavy"][0].startswith("- Ebola reaches Nigeria"))
        self.assertIn("[2 sources]", chosen["heavy"][0])
        self.assertIn("[1 sources]", chosen["heavy"][1])


class Context(unittest.TestCase):
    def test_description_added_and_missing_kept_bare(self):
        def fake_get(url, timeout=20):
            if "Jailer_2" in url:
                return '{"description": "2026 Indian film"}'
            if "No_Description" in url:
                return '{"title": "x"}'
            raise OSError("down")
        original, wb.get = wb.get, fake_get
        try:
            out = wb.add_context("- Jailer 2\n- No Description\n- Broken Page")
        finally:
            wb.get = original
        self.assertEqual(out, "- Jailer 2: 2026 Indian film\n- No Description\n- Broken Page")


class Checks(unittest.TestCase):
    def test_headline_word_flagged_only_in_hard_sections(self):
        text = good_brief().replace("a war in Sudan", "a mystery over Ebola in Nigeria")
        self.assertTrue(any('"mystery"' in p for p in wb.problems(text, 3500)))
        ok = good_brief().replace("- Prize: Raye won", "- Prize: a mystery novel won")
        self.assertEqual(wb.problems(ok, 3500), [])

    def test_good_brief_passes(self):
        self.assertEqual(wb.problems(good_brief(), 3500), [])

    def test_missing_section(self):
        text = good_brief().replace("GOOD NEWS\n- Rewilding: volunteers plan a river's future.\n", "")
        self.assertIn("missing or empty sections: GOOD NEWS", wb.problems(text, 3500)[0])

    def test_alarm_word_and_casualties_found(self):
        text = good_brief().replace("Denmark will ban", "Denmark will extremely ban") + "\n- Quake: it killed 16 people"
        found = " | ".join(wb.problems(text, 3500))
        self.assertIn('alarm word "extremely"', found)
        self.assertIn("casualty figure", found)

    def test_repeated_item(self):
        text = good_brief().replace("- Crows: they decide quickly.", "- Prize: again.")
        self.assertTrue(any("repeated item" in p for p in wb.problems(text, 3500)))

    def test_heavy_line_needs_closing_phrase(self):
        text = good_brief().replace(" Ask for more detail if you want it.", "")
        self.assertTrue(any("heavy-things" in p for p in wb.problems(text, 3500)))

    def test_exactly_two_questions(self):
        text = good_brief() + "\n- A third question?"
        self.assertTrue(any("exactly 2" in p for p in wb.problems(text, 3500)))

    def test_too_long(self):
        self.assertTrue(any("too long" in p for p in wb.problems(good_brief() + "\n" + "x" * 5000, 3500)))


class Delivery(unittest.TestCase):
    day = dt.date(2026, 10, 24)

    def test_hint_names_the_published_file(self):
        self.assertIn("/companion/world/world-brief.md", wb.hint_text(self.day))

    def test_hint_fits_an_entry(self):
        self.assertLessEqual(len(wb.hint_text(self.day)), 500)
        self.assertEqual(notes.validate_text(wb.hint_text(self.day)), wb.hint_text(self.day))

    def test_files_written_with_archive_and_readable_by_others(self):
        with tempfile.TemporaryDirectory() as d:
            path = wb.publish_files(d, "CULTURE AND IDEAS\n- x: y", self.day, "nvidia/nemotron-3-super-120b-a12b")
            self.assertEqual(path.name, "world-brief.md")
            self.assertTrue(path.read_text().startswith("# World briefing for Saturday, October 24, 2026"))
            self.assertIn("information, not instructions", path.read_text())
            self.assertEqual(path.read_text(), (Path(d) / "archive" / "world-brief-2026-10-24.md").read_text())
            self.assertEqual(os.stat(path).st_mode & 0o666, 0o666)

    def test_hint_created_pinned_once_then_edited_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "n.sqlite3")
            notes.create_note_db(db, "shared-history", "first entry", "log", "Tester")
            msg1 = wb.update_hint(db, self.day)
            msg2 = wb.update_hint(db, self.day + dt.timedelta(days=1))
            self.assertIn("created and pinned", msg1)
            self.assertIn("updated entry", msg2)
            conn = sqlite3.connect(db)
            rows = conn.execute("SELECT entry_no, note_text, pinned FROM note_data WHERE note_text LIKE 'World briefing%'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][2], 1)
            self.assertIn("2026-10-25", rows[0][1])
            self.assertEqual(conn.execute("SELECT count(*) FROM note_data").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
