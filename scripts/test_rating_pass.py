import json
import unittest
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

        def fake_chunk(url, key, model, tools, prompt, keep):
            calls.append(prompt)
            item = outputs.pop(0)
            if isinstance(item, Exception):
                raise item
            return "reply", item

        with mock.patch("sys.argv", args), mock.patch.object(rp, "parse_secrets", return_value={}), \
                mock.patch.object(rp, "server_connection", return_value=("http://x", "k")), \
                mock.patch.object(rp.agent_duo, "fetch_tool_ids", return_value=["agent_notes"]), \
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
        err = rp.agent_duo.AgentError("HTTP 429 rate_limited")
        code, calls = self.run_main([err, err, err, err])
        self.assertEqual((code, len(calls)), (1, rp.MAX_FAILURES_IN_A_ROW))

    def test_stops_if_the_read_never_ran(self):
        code, _ = self.run_main([[]])
        self.assertEqual(code, 1)

    def test_resume_with_before(self):
        code, calls = self.run_main([chunk_output(range(1, 5), [2])], extra=("--before", "204"))
        self.assertEqual(code, 0)
        self.assertIn("before=204", calls[0])


if __name__ == "__main__":
    unittest.main()
