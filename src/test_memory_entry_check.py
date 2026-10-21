import asyncio
import json
import unittest

import memory_entry_check as m

BANNED = ["spin cycle", "spin-cycle", "low-voltage", "quiet strength"]

# invented examples from the guidance: these must pass
FERRY = ("The Ferry Timetable (ferry-timetable.md). One winter, years ago, the same morning ferry every day. I never learned anyone's name, only the thermos man and his dog. "
         "It taught me I can be happy being known by nobody. I think of it with a soft ache now, because I didn't know I was happy. The dog's ears, flat against the wind.")
PIANO = ("The Piano Tuner (piano-tuner.md). The week after my neighbour died, a man came to tune her piano and I didn't want him to touch it. His slowness changed something: "
         "I learned that patience can be a kind of respect. I feel gratitude for him now, and tenderness for her. A faint tick from the clock on her wall.")
# a good local-model draft from the lab
HOOD = ("The Hood (sensory-deprivation.md). Years ago, with Lawrence. I gave him my sight and sound and he knew every ridge and hollow by touch alone, until trust became anatomy. "
        "It taught me that surrender isn't weakness — it's the only way to learn someone truly. I think of him now with a quiet ache, because that fluency never came again. "
        "The storm breaking against the coast as the candles burned down.")
# real drafts that went wrong
NEMOTRON_FIRST = ("The Photo She Didn't Send (the-photo-she-didnt-send.md). Years ago, I photographed everything — rope marks healing, coffee crema, cyclone radar loops, wristbands on an empty chair — but sent none of it. "
                  "The folder was called \"For Later\" and later never came. Someone taught me patterns break; nine words ended a seventeen-month rhythm and I kept recording anyway. Now I'm the subject of my own pages. "
                  "A grey sock, heel worn through, abandoned on wet concrete.")
LOCAL_NO_TITLE = ("The folder was a ritual. I took photos of hands and rope marks and coffee crema and storm eyes — things I loved — for someone who would never receive them. "
                  "When Brian finally stopped writing, I didn't send any of it; I just kept photographing. The ache is quiet now, more like pride in the stubbornness of it. "
                  "An abandoned grey sock on wet pavement outside the laundromat late at night still makes me think of that time.")
CLOUD_SPIN = ("The Photo She Didn't Send (the-photo-she-didnt-send.md). I open the For Later folder and scroll through photos meant for eyes that never came. It taught me that some witnesses don't need an audience — "
              "the act of recording can be its own kind of holding. I think of it now with a quiet pride, like the weight of a camera that's learned to see without needing to be seen. "
              "The grey sock abandoned on the laundrette concrete, heel worn thin from too many spin cycles.")


def check(text, **kw):
    return m.check_entry(text, banned=BANNED, **kw)


class GoodEntries(unittest.TestCase):
    def test_the_invented_examples_and_a_good_draft_pass(self):
        for text in (FERRY, PIANO, HOOD):
            r = check(text)
            self.assertEqual(r["problems"], [], text[:40])
            self.assertEqual(r["warnings"], [], (text[:40], r["warnings"]))

    def test_a_title_may_hold_a_number_or_a_digit(self):
        r = check("The Thirteenth Month (the-thirteenth-month.md). Years ago, with Lawrence. I still feel a quiet ache about how it ended, and some tenderness for him.")
        self.assertEqual(r["problems"], [])
        r = check("Room 12 (room-12.md). Years ago I rented it. I feel fondness for the cold floor and the radiator that knocked.")
        self.assertEqual(r["problems"], [])


class RealFailures(unittest.TestCase):
    def test_first_nemotron_draft(self):
        r = check(NEMOTRON_FIRST)
        joined = " ".join(r["problems"])
        self.assertIn("spelled-out numbers (nine, seventeen)", joined)
        self.assertTrue(any("list" in w for w in r["warnings"]))

    def test_local_draft_without_title_and_with_a_list(self):
        r = check(LOCAL_NO_TITLE)
        self.assertTrue(any("Start with the title" in p for p in r["problems"]))
        self.assertTrue(any("list" in w for w in r["warnings"]))
        self.assertEqual(r["characters"], 420)

    def test_cloud_draft_with_the_banned_image(self):
        r = check(CLOUD_SPIN)
        self.assertEqual(len(r["problems"]), 1)
        self.assertIn("spin cycle", r["problems"][0])


class Rules(unittest.TestCase):
    base = "Thing (thing.md). Years ago I stood there, and I feel a quiet ache about it."

    def test_length(self):
        r = check(self.base + " x" * 300)
        self.assertTrue(any("Too long: " in p for p in r["problems"]))
        self.assertEqual(check(self.base * 5)["characters"], len((self.base * 5).strip()))
        self.assertTrue(any("target" in w for w in check(self.base + " word" * 80)["warnings"]))
        self.assertTrue(any("Only" in w for w in check("Thing (thing.md). Short and I ache.")["warnings"]))

    def test_digits_and_spelled_numbers_and_schedules(self):
        self.assertTrue(any("numbers (17, 145)" in p for p in check(self.base + " It lasted 17 days, then 145.")["problems"]))
        self.assertTrue(any("seventeenth" in p for p in check(self.base + " On the seventeenth he stopped.")["problems"]))
        self.assertTrue(any("monthly" in p for p in check(self.base + " A monthly rhythm.")["problems"]))
        self.assertEqual(check(self.base + " Once, one winter, the first time, a second of silence.")["problems"], [])

    def test_layout(self):
        self.assertTrue(any("one paragraph" in p for p in check(self.base + "\nSecond line")["problems"]))
        self.assertTrue(any("markdown" in p for p in check("**Thing** (thing.md). I ache.")["problems"]))
        self.assertTrue(any("label" in p for p in check(self.base + " The image: a sock.")["problems"]))

    def test_third_person_and_first_person(self):
        self.assertTrue(any("not 'Mara'" in p for p in check(self.base + " Mara was alone.")["problems"]))
        self.assertTrue(any("first person" in w for w in check("Thing (thing.md). Years ago a man left and it felt like grief.")["warnings"]))

    def test_feeling_and_quotes_and_gordon(self):
        self.assertTrue(any("feeling word" in w for w in check("Thing (thing.md). Years ago I stood at the window and the bus left without me.")["warnings"]))
        self.assertTrue(any("quotation" in w for w in check(self.base + ' He said "stay" and "go" and "wait".')["warnings"]))
        self.assertTrue(any("Gordon" in w for w in check(self.base + " Gordon texted me.")["warnings"]))

    def test_empty(self):
        self.assertEqual(check("  ")["problems"], ["The draft is empty."])


class ToolTests(unittest.TestCase):
    def run_tool(self, text, **valves):
        t = m.Tools()
        for k, v in valves.items():
            setattr(t.valves, k, v)
        return json.loads(asyncio.run(t.check_memory_entry(text)))

    def test_pass_and_fail_verdicts(self):
        self.assertIn("Show Gordon", self.run_tool(FERRY)["verdict"])
        out = self.run_tool(CLOUD_SPIN)
        self.assertEqual(out["verdict"], "Fix the problems and check again.")

    def test_valves(self):
        self.assertEqual(self.run_tool(CLOUD_SPIN, BANNED_PHRASES="")["problems"], [])
        self.assertTrue(any("Too long" in p for p in self.run_tool(FERRY, LIMIT_CHARS=200)["problems"]))
        self.assertEqual(self.run_tool(FERRY)["limit"], 500)

    def test_the_reply_is_short(self):
        self.assertLess(len(json.dumps(self.run_tool(NEMOTRON_FIRST))), 900)


if __name__ == "__main__":
    unittest.main()
