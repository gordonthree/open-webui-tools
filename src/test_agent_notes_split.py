"""
Tests for the two deployable tools generated from agent_notes_full.py by scripts/build_agent_notes.py.
Run from src/ with: python3 -m unittest test_agent_notes_split -v
"""

import asyncio
import importlib.util
import inspect
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import agent_notes as core
import agent_notes_curation as curation
import agent_notes_full as full

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("build_agent_notes", SCRIPTS / "build_agent_notes.py")
build = importlib.util.module_from_spec(spec)
sys.modules["build_agent_notes"] = build
spec.loader.exec_module(build)


def methods(tool) -> set:
    return {n for n in dir(tool) if n.startswith("agent_notes_")}


def docs(tool) -> dict:
    return {n: inspect.getdoc(getattr(tool, n)) or "" for n in methods(tool)}


class SplitTests(unittest.TestCase):
    def test_generated_files_are_up_to_date(self):
        source = build.SOURCE.read_text(encoding="utf-8")
        for path, name in build.targets().items():
            self.assertEqual(path.read_text(encoding="utf-8"), build.build(source, name), f"{path.name} is stale: run python3 scripts/build_agent_notes.py")

    def test_every_method_lives_in_exactly_one_tool_and_help_is_with_the_everyday_one(self):
        everything, a, b = methods(full.Tools()), methods(core.Tools()), methods(curation.Tools())
        self.assertEqual(a | b, everything)
        self.assertEqual(a & b, set())
        self.assertIn("agent_notes_help", a)
        self.assertEqual(a, set(build.CORE_METHODS))
        self.assertEqual(b, set(build.CURATION_METHODS))
        for must in ("agent_notes_read", "agent_notes_read_tagged", "agent_notes_create", "agent_notes_append"):  # agent_duo, the autonomy charter
            self.assertIn(must, a)
        self.assertIn("agent_notes_update_summary", b)  # the nightly summarizer needs this tool as well as the other

    def test_the_everyday_tool_never_points_at_a_tool_it_may_not_have(self):
        for name, doc in docs(core.Tools()).items():
            for other in methods(curation.Tools()):
                self.assertNotIn(other, doc, f"{name}'s description mentions {other}")

    def test_valves_headers_and_versions(self):
        self.assertEqual(set(curation.Tools().valves.model_dump()), {"NOTES_DB_PATH", "DEFAULT_AUTHOR"})
        self.assertEqual(set(core.Tools().valves.model_dump()), set(full.Tools().valves.model_dump()))
        self.assertEqual(core.Tools().valves.NOTES_DB_PATH, curation.Tools().valves.NOTES_DB_PATH)  # both default to the same file
        version = re.search(r"^version: (.+)$", Path(full.__file__).read_text(), re.M).group(1)
        for module, title in ((core, "Agent Notes"), (curation, "Agent Notes Curation")):
            head = module.__doc__
            self.assertIn(f"title: {title}\n", head)
            self.assertIn(f"version: {version}\n", head)
            self.assertIn("author:", head)
        self.assertLess(len(" ".join(docs(core.Tools()).values())), len(" ".join(docs(full.Tools()).values())))

    def test_the_curation_tool_carries_no_dead_everyday_code(self):
        self.assertFalse(hasattr(curation.Tools(), "_count_reads"))
        self.assertTrue(hasattr(core.Tools(), "_count_reads"))


class SharedDatabaseTests(unittest.TestCase):
    """The two tools are two Open WebUI tools over one database file: what one writes the other sees."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "notes.sqlite3")
        self.a, self.b = core.Tools(), curation.Tools()
        self.a.valves.NOTES_DB_PATH = self.b.valves.NOTES_DB_PATH = self.db
        self.a.valves.MIN_SUMMARY_CHARS = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_curation_changes_show_up_in_the_everyday_tool(self):
        self.assertTrue(self.run_async(self.a.agent_notes_create("pets", "the cat is called Mo", weight="7"))["success"])
        self.assertTrue(self.run_async(self.a.agent_notes_append("pets", "word " * 200, continues=True))["success"])
        self.assertTrue(self.run_async(self.b.agent_notes_update_summary("pets", "about the pets"))["success"])
        self.assertTrue(self.run_async(self.b.agent_notes_add_tags("pets", "animals", create=True))["success"])
        read = self.run_async(self.a.agent_notes_read("pets"))
        self.assertEqual(read["summary"]["text"], "about the pets")
        self.assertEqual(read["tags"], ["animals"])
        self.assertEqual(read["entries"][0]["weight"], 7)
        self.assertEqual(read["entries"][1]["continues_at"], 3)
        found = self.run_async(self.a.agent_notes_search_summary("pets"))
        self.assertEqual(found["results"][0]["note_name"], "pets")
        self.assertEqual(self.run_async(self.a.agent_notes_read_tagged("animals"))["notes"][0]["note_name"], "pets")

    def test_everyday_writes_and_curation_deletes_and_resets(self):
        self.run_async(self.a.agent_notes_create("n", "one"))
        self.run_async(self.a.agent_notes_append("n", "two"))
        self.run_async(self.a.agent_notes_read("n"))  # counts reads
        self.assertTrue(self.run_async(self.b.agent_notes_reset_hits("n"))["success"])
        self.assertTrue(self.run_async(self.b.agent_notes_delete_entry("n", "1"))["success"])
        entries = self.run_async(self.a.agent_notes_read("n", verbose=True))["entries"]
        self.assertEqual([(e["entry_no"], e["hits"]) for e in entries], [(2, 0)])
        self.assertTrue(self.run_async(self.b.agent_notes_delete("n"))["success"])
        self.assertEqual(self.run_async(self.a.agent_notes_list())["total_notes"], 0)

    def test_the_everyday_help_still_explains_curation_but_says_where_it_lives(self):
        organize = self.run_async(self.a.agent_notes_help("organize"))["guide"]
        self.assertIn("Agent Notes Curation tool", organize)
        self.assertIn("agent_notes_add_tags", organize)


if __name__ == "__main__":
    unittest.main()
