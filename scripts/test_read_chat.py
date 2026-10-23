"""
Tests for read_chat.py. Run with:  python -m unittest test_read_chat -v
(no Open WebUI server needed).
"""
import unittest

import read_chat as mod


def make_chat(*messages):
    chain, ids = {}, []
    for i, m in enumerate(messages):
        mid = f"m{i}"
        chain[mid] = {"id": mid, "parentId": ids[-1] if ids else None, "timestamp": 1_800_000_000, **m}
        ids.append(mid)
    return {"id": "c1", "title": "Test", "created_at": 1_800_000_000,
            "chat": {"history": {"currentId": ids[-1], "messages": chain}}}


NEW_STYLE = make_chat(
    {"role": "user", "content": "Did you see the briefing?"},
    {"role": "assistant", "model": "medium-mara", "content": "Yes.", "output": [
        {"type": "reasoning", "content": [{"type": "text", "text": "Gordon asks about the brief."}]},
        {"type": "function_call", "call_id": "x1", "name": "read_file", "arguments": '{"path": "world/world-brief.md"}'},
        {"type": "function_call_output", "call_id": "x1", "output": [{"type": "text", "text": "A" * 50}]},
        {"type": "reasoning", "content": [{"type": "text", "text": "The crows item is lovely."}]},
        {"type": "message", "content": [{"type": "text", "text": "Yes, the crows."}]},
    ]},
)

OLD_STYLE = make_chat(
    {"role": "user", "content": "hi"},
    {"role": "assistant", "model": "medium-mara", "content":
        '<details type="reasoning" done="true"><summary>Thought for 3 seconds</summary>\n> line one\n> line two\n</details>\n'
        '<details type="tool_calls" done="true" name="notify" arguments="{&quot;text&quot;: &quot;hi&quot;}" '
        'result="&quot;ok&quot;"><summary>Tool</summary></details>\nHello Gordon.'},
)


class RenderTests(unittest.TestCase):
    def test_new_style_labels_everything(self):
        out = mod.render(NEW_STYLE, agent="Mara")
        self.assertIn("not Claude's reasoning", out)
        self.assertIn("(medium-mara)", out)
        self.assertIn("[Mara's thinking]\nGordon asks about the brief.\n[end of Mara's thinking]", out)
        self.assertIn('[Mara called read_file({"path": "world/world-brief.md"})]', out)
        self.assertIn("[result] " + "A" * 50, out)
        self.assertIn("[Mara said]\nYes, the crows.", out)
        self.assertIn("[Gordon said]\nDid you see the briefing?", out)
        self.assertNotIn("<", out.split("\n", 2)[2])

    def test_thinking_kept_in_order_and_verbatim(self):
        out = mod.render(NEW_STYLE, agent="Mara")
        self.assertLess(out.index("Gordon asks"), out.index("read_file"))
        self.assertLess(out.index("read_file"), out.index("The crows item"))

    def test_old_style_details_blocks(self):
        out = mod.render(OLD_STYLE, agent="Mara")
        self.assertIn("[Mara's thinking]\nline one\nline two\n[end of Mara's thinking]", out)
        self.assertIn('[Mara called notify({"text": "hi"})]', out)
        self.assertIn('[result] "ok"', out)
        self.assertIn("[Mara said]\nHello Gordon.", out)
        self.assertNotIn("<details", out)

    def test_think_tags(self):
        chat = make_chat({"role": "assistant", "content": "<think>hmm</think>Answer."})
        out = mod.render(chat, agent="Mara")
        self.assertIn("[Mara's thinking]\nhmm", out)
        self.assertIn("[Mara said]\nAnswer.", out)

    def test_thinking_only(self):
        out = mod.render(NEW_STYLE, agent="Mara", thinking_only=True)
        self.assertIn("The crows item", out)
        self.assertNotIn("read_file", out)
        self.assertNotIn("Did you see", out)
        self.assertNotIn("Yes, the crows.", out)

    def test_results_shortened_or_hidden(self):
        self.assertIn("[40 more chars]", mod.render(NEW_STYLE, result_chars=10))
        self.assertNotIn("[result]", mod.render(NEW_STYLE, result_chars=0))

    def test_default_label_is_model(self):
        self.assertIn("[medium-mara's thinking]", mod.render(NEW_STYLE))

    def test_turn_ranges(self):
        self.assertEqual(list(mod.parse_turns(None, 4)), [1, 2, 3, 4])
        self.assertEqual(list(mod.parse_turns("2", 4)), [2])
        self.assertEqual(list(mod.parse_turns("2-3", 4)), [2, 3])
        self.assertEqual(list(mod.parse_turns("3-", 4)), [3, 4])
        self.assertEqual(list(mod.parse_turns("2-9", 4)), [2, 3, 4])


if __name__ == "__main__":
    unittest.main()
