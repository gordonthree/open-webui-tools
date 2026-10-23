import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class RelativeTime(unittest.TestCase):
    TODAY = dt.date(2026, 10, 23)

    def phrase(self, y, m, d):
        return wb.age_phrase(dt.date(y, m, d), self.TODAY)

    def test_age_words(self):
        self.assertEqual(self.phrase(2026, 10, 23), "today")
        self.assertEqual(self.phrase(2026, 10, 22), "yesterday")
        self.assertEqual(self.phrase(2026, 10, 24), "tomorrow")
        self.assertEqual(self.phrase(2026, 10, 20), "3 days ago")
        self.assertEqual(self.phrase(2026, 10, 14), "about a week ago")
        self.assertEqual(self.phrase(2026, 10, 5), "a few weeks ago")
        self.assertEqual(self.phrase(2026, 8, 20), "a couple of months ago")
        self.assertEqual(self.phrase(2026, 5, 1), "6 months ago")
        self.assertEqual(self.phrase(2025, 6, 1), "over a year ago")
        self.assertEqual(self.phrase(2020, 1, 1), "6 years ago")
        self.assertEqual(self.phrase(2026, 10, 27), "4 days from now")
        self.assertEqual(self.phrase(2026, 11, 20), "a few weeks from now")

    def rel(self, text):
        return wb.relativize_text(text, self.TODAY)

    def test_day_month_and_month_day_forms(self):
        self.assertEqual(self.rel("He finished on 5 October."), ("He finished a few weeks ago.", 1))
        self.assertEqual(self.rel("Finished Oct. 5, 2026."), ("Finished a few weeks ago.", 1))
        self.assertEqual(self.rel("Opens on November 12th."), ("Opens a few weeks from now.", 1))
        self.assertEqual(self.rel("Votes were cast on Tuesday 21 October."), ("Votes were cast 2 days ago.", 1))
        self.assertEqual(self.rel("Since 22 October the line is shut."), ("Since yesterday the line is shut.", 1))

    def test_numeric_forms(self):
        self.assertEqual(self.rel("Born 2026-10-22."), ("Born yesterday.", 1))
        self.assertEqual(self.rel("Filed 10/22/2026."), ("Filed yesterday.", 1))
        self.assertEqual(self.rel("Filed 22/10/2026."), ("Filed yesterday.", 1))
        self.assertEqual(self.rel("Filed 3/4/2026."), ("Filed 3/4/2026.", 0))  # day-first or month-first: not guessed

    def test_year_picks_the_nearest_when_missing(self):
        self.assertEqual(self.rel("Due 2 January.")[0], "Due a couple of months from now.")
        self.assertEqual(self.rel("Born 1 December 1990.")[0], "Born 35 years ago.")

    def test_things_that_are_not_day_dates_are_left_alone(self):
        for text in ["Booker Prize 2026 shortlist", "Since June 2026 there is a pause", "In 1998 he left Chile", "May is a good month",
                     "He may 5 times", "Room 12 on floor 5", "31 February is not a day", "Feb 30 is not either"]:
            self.assertEqual(self.rel(text), (text, 0), text)

    def test_bracket_tags_become_parentheses(self):
        self.assertEqual(wb.tidy_age_tags("- Crows: they count [3 days ago]"), "- Crows: they count (3 days ago)")
        self.assertEqual(wb.tidy_age_tags("- Wire: news [3 days ago] [2 sources]"), "- Wire: news (3 days ago) [2 sources]")

    def test_ages_under_three_days_are_dropped(self):
        text = "- A: one (today)\n- B: two (yesterday)\n- C: three (2 days ago) \n- D: four (3 days ago)\n- E: five [yesterday]"
        self.assertEqual(wb.tidy_age_tags(text), "- A: one\n- B: two\n- C: three\n- D: four (3 days ago)\n- E: five")

    def test_clean_output_rewrites_dates_and_tags(self):
        out = wb.clean_output("- Walk: ended on 5 October [a few weeks ago]", self.TODAY)
        self.assertEqual(out, "- Walk: ended a few weeks ago (a few weeks ago)")
        self.assertEqual(wb.clean_output("- Walk: ended on 22 October (yesterday)", self.TODAY), "- Walk: ended yesterday")

    def test_feed_items_carry_their_age(self):
        rss = ("<rss><channel>"
               "<item><title>Fresh</title><description>d</description><pubDate>Thu, 23 Oct 2026 08:00:00 +0000</pubDate></item>"
               "<item><title>Older</title><pubDate>Mon, 20 Oct 2026 12:00:00 +0000</pubDate></item>"
               "<item><title>Undated</title></item>"
               "<item><title>Stale</title><pubDate>Mon, 01 Sep 2026 12:00:00 +0000</pubDate></item>"
               "</channel></rss>")
        now = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(wb, "get", return_value=rss):
            lines = wb.feed_items("http://x", 14, now=now, today=self.TODAY)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("- Fresh: d [") and lines[0].endswith("]"))
        self.assertIn("[3 days ago]", lines[1])
        self.assertEqual(lines[2], "- Undated")

    def test_age_tag_does_not_make_unrelated_stories_look_repeated(self):
        a = wb.words("- Gold prices climb [yesterday]")
        b = wb.words("- Storm hits coast [yesterday]")
        self.assertFalse(wb.is_repeat(a, b, 2))
        self.assertNotIn("yesterday", a)


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

    def test_entries_written_by_an_agent_are_never_taken_for_the_pinned_ones(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "n.sqlite3")
            notes.create_note_db(db, "shared-history", "World briefing is a phrase an agent might write itself", "log", "Tester")
            self.assertIn("created and pinned", wb.update_hint(db, self.day))

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
