"""
Tests for tick_report.py. Run with:  python -m unittest test_tick_report -v
(no Open WebUI server needed - HTTP is faked).
"""
import tempfile
import unittest
from pathlib import Path

import tick_report as mod
from test_cleanup_noop_chats import FOLDER, FakeSession


def call(name, args="{}"):
    return {"type": "function_call", "name": name, "arguments": args}


def make_tick(chat_id, final="NOOP", done=True, calls=(), created=1_800_000_000, extra_user=False):
    messages = {
        "m1": {"id": "m1", "role": "user", "content": "tick", "parentId": None},
        "m2": {"id": "m2", "role": "assistant", "content": final, "done": done, "parentId": "m1",
               "output": list(calls)},
    }
    current = "m2"
    if extra_user:
        messages["m3"] = {"id": "m3", "role": "user", "content": "hi", "parentId": "m2"}
        messages["m4"] = {"id": "m4", "role": "assistant", "content": "hello", "done": True, "parentId": "m3"}
        current = "m4"
    return {"id": chat_id, "title": "Companion tick", "folder_id": FOLDER, "created_at": created,
            "updated_at": created, "chat": {"history": {"currentId": current, "messages": messages}}}


LOGGED = call("agent_notes_append", '{"name": "autonomy-log", "text": "NOOP - x"}')


class AnalyzeTests(unittest.TestCase):
    def test_clean_noop(self):
        t = mod.analyze(make_tick("a", calls=[call("read_file"), LOGGED]), 8)
        self.assertEqual((t["outcome"], t["flags"]), ("NOOP", []))

    def test_silent_tick_is_flagged_twice(self):
        t = mod.analyze(make_tick("a", final="", calls=[call("read_file")]), 8)
        self.assertEqual(t["outcome"], "SILENT")
        self.assertEqual(t["flags"], ["SILENT", "NO-LOG"])

    def test_message_without_log_write(self):
        t = mod.analyze(make_tick("a", final="I did a thing.", calls=[call("read_file")]), 8)
        self.assertEqual((t["outcome"], t["flags"]), ("message", ["NO-LOG"]))

    def test_log_write_to_a_different_note_does_not_count(self):
        other = call("agent_notes_append", '{"name": "internal-life", "text": "x"}')
        t = mod.analyze(make_tick("a", calls=[other]), 8)
        self.assertEqual(t["flags"], ["NO-LOG"])

    def test_repeat_flag_counts_a_tool_regardless_of_arguments(self):
        calls = [LOGGED] + [call("agent_notes_update_summary", f'{{"summary": "{i}"}}') for i in range(8)]
        t = mod.analyze(make_tick("a", final="", calls=calls), 8)
        self.assertIn("REPEAT:agent_notes_update_summaryx8", t["flags"])
        self.assertNotIn("REPEAT:agent_notes_update_summaryx8", mod.analyze(make_tick("a", calls=calls), 9)["flags"])

    def test_running_and_conversation_are_not_flagged_for_missing_log(self):
        running = mod.analyze(make_tick("a", final="", done=False), 8)
        self.assertEqual((running["outcome"], running["flags"]), ("running", []))
        convo = mod.analyze(make_tick("b", final="", extra_user=True), 8)
        self.assertEqual((convo["outcome"], convo["flags"]), ("conversation", []))


class MainTests(unittest.TestCase):
    def run_main(self, chats, *extra):
        d = tempfile.mkdtemp()
        secrets = Path(d) / "secrets.md"
        secrets.write_text("OWUI_BASE_URL: http://owui\nOWUI_API_KEY: k\n")
        lines = []
        mod.main(["--secrets", str(secrets), *extra], session=FakeSession(chats), say=lines.append)
        return "\n".join(lines)

    def chats(self):
        return [
            make_tick("clean001", calls=[LOGGED], created=1_800_000_000),
            make_tick("silent01", final="", calls=[call("read_file")], created=1_800_003_600),
            make_tick("loop0001", final="", calls=[LOGGED] + [call("read_email")] * 9, created=1_800_007_200),
        ]

    def test_summary_and_flagged_only(self):
        out = self.run_main(self.chats())
        self.assertIn("3 tick chat(s)", out)
        self.assertIn("NOOP 1", out)
        self.assertIn("SILENT 2", out)
        self.assertIn("Flagged ticks (2)", out)
        self.assertIn("silent01", out)
        self.assertIn("REPEAT:read_emailx9", out)
        self.assertNotIn("clean001", out)

    def test_all_flag_lists_everything(self):
        out = self.run_main(self.chats(), "--all")
        self.assertIn("clean001", out)

    def test_since_filters_by_date(self):
        out = self.run_main(self.chats(), "--since", "2999-01-01")
        self.assertIn("0 tick chat(s)", out)

    def test_capped_tools_per_day_and_over_marker(self):
        chats = [make_tick("e", calls=[LOGGED] + [call("send_email")] * 11)]
        out = self.run_main(chats)
        self.assertIn("send_email 11 OVER", out)
        self.assertIn("notify 0", out)


if __name__ == "__main__":
    unittest.main()
