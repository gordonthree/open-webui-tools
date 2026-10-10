"""
Tests for agent_duo.py. Run with: python -m unittest test_agent_duo -v
(no real Open WebUI server needed - HTTP is faked).
"""

import asyncio
import unittest
import urllib.parse
from unittest.mock import patch

import agent_duo as mod


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeOWUI:
    """One simulated Open WebUI server: model presets, backing chats, and a scripted agent."""

    def __init__(self, replies, tool_ids=("agent_notes",), fail_new_chat=False, never_done=False, outputs=None):
        self.replies = list(replies)  # one scripted reply per turn
        self.outputs = list(outputs or [])  # optional tool-call `output` per turn (same order as replies)
        self.tool_ids = list(tool_ids)
        self.fail_new_chat = fail_new_chat
        self.never_done = never_done
        self.chats = {}  # chat_id -> {"asst_id", "reply", "done"}
        self.completions = []  # bodies sent to /api/chat/completions
        self.deleted = []

    def get(self, url, headers=None, timeout=None, params=None):
        path = urllib.parse.urlparse(url).path
        if path == "/api/v1/models/model":
            return FakeResponse(200, {"id": params["id"], "meta": {"toolIds": self.tool_ids}})
        if path.startswith("/api/v1/chats/"):
            chat = self.chats[path.rsplit("/", 1)[1]]
            reply = None if self.never_done else self.replies[chat["turn"]]
            msg = {"content": reply or "", "done": reply is not None}
            if chat["turn"] < len(self.outputs):
                msg["output"] = self.outputs[chat["turn"]]
            return FakeResponse(200, {"chat": {"history": {"messages": {chat["asst_id"]: msg}}}})
        return FakeResponse(404)

    def post(self, url, headers=None, json=None, timeout=None):
        path = urllib.parse.urlparse(url).path
        if path == "/api/v1/chats/new":
            if self.fail_new_chat:
                return FakeResponse(500, "boom")
            history = json["chat"]["history"]["messages"]
            asst_id = next(k for k, m in history.items() if m["role"] == "assistant")
            chat_id = f"chat{len(self.chats)}"
            self.chats[chat_id] = {"asst_id": asst_id, "turn": len(self.chats)}
            return FakeResponse(200, {"id": chat_id})
        if path == "/api/chat/completions":
            self.completions.append(json)
            return FakeResponse(200, {"status": True})
        return FakeResponse(404)

    def delete(self, url, headers=None, timeout=None):
        self.deleted.append(urllib.parse.urlparse(url).path.rsplit("/", 1)[1])
        return FakeResponse(200, True)


class Router:
    """Sends each request to the fake server whose base URL it starts with."""

    def __init__(self, **servers):
        self.servers = servers  # base url -> FakeOWUI

    def _pick(self, url):
        for base, srv in self.servers.items():
            if url.startswith(base):
                return srv
        raise AssertionError(f"unexpected URL {url}")

    def get(self, url, **kw):
        return self._pick(url).get(url, **kw)

    def post(self, url, **kw):
        return self._pick(url).post(url, **kw)

    def delete(self, url, **kw):
        return self._pick(url).delete(url, **kw)

    RequestException = mod.requests.RequestException


MARA = "http://mara.test"
HANNAH = "http://hannah.test"


def make_pipe(**overrides):
    pipe = mod.Pipe()
    pipe.valves = pipe.Valves(
        MARA_URL=MARA, MARA_KEY="mk", HANNAH_URL=HANNAH, HANNAH_KEY="hk", POLL_SECONDS=0.2, **overrides
    )
    return pipe


def run_pipe(pipe, messages, router, chat_id=None):
    async def go():
        with patch.object(mod, "requests", router):
            gen = await pipe.pipe({"messages": messages}, __metadata__={"chat_id": chat_id} if chat_id else None)
            return "".join([chunk async for chunk in gen])

    return asyncio.run(go())


class HistoryTests(unittest.TestCase):
    NAMES = ["Mara", "Hannah"]

    def test_assistant_message_splits_into_speaker_turns(self):
        turns = mod.parse_history(
            [
                {"role": "user", "content": "hi both"},
                {"role": "assistant", "content": "\n\n**Mara:** hello\nsecond line\n\n\n\n**Hannah:** hi there\n\n"},
                {"role": "user", "content": "thanks"},
            ],
            self.NAMES,
            "Gordon",
        )
        self.assertEqual(
            turns,
            [
                {"speaker": "Gordon", "text": "hi both"},
                {"speaker": "Mara", "text": "hello\nsecond line"},
                {"speaker": "Hannah", "text": "hi there"},
                {"speaker": "Gordon", "text": "thanks"},
            ],
        )

    def test_status_notes_are_not_turns(self):
        turns = mod.parse_history(
            [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "**Mara:** a\n\n*Hannah has nothing to add.*\n\n"},
                {"role": "assistant", "content": "*Mara couldn't answer: timeout*"},
            ],
            self.NAMES,
            "Gordon",
        )
        self.assertEqual([t["speaker"] for t in turns], ["Gordon", "Mara"])
        self.assertEqual(turns[1]["text"], "a")

    def test_build_messages_maps_roles_and_merges(self):
        turns = [
            {"speaker": "Gordon", "text": "q"},
            {"speaker": "Mara", "text": "mine"},
            {"speaker": "Hannah", "text": "theirs"},
            {"speaker": "Gordon", "text": "next"},
        ]
        msgs = mod.build_messages("Mara", "Hannah", "Gordon", turns, "[PASS]")
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[2]["content"], "mine")
        self.assertEqual(msgs[3]["content"], "[Hannah]: theirs\n\n[Gordon]: next")
        self.assertIn("[PASS]", msgs[0]["content"])

    def test_choose_first(self):
        t = lambda text: [{"speaker": "Gordon", "text": text}]
        self.assertEqual(mod.choose_first(t("what do you think, Hannah?"), self.NAMES, "Mara", "Gordon"), "Hannah")
        self.assertEqual(mod.choose_first(t("Mara and Hannah, hi"), self.NAMES, "Mara", "Gordon"), "Mara")
        self.assertEqual(mod.choose_first(t("hello"), self.NAMES, "Hannah", "Gordon"), "Hannah")

    def test_strip_details_and_marker(self):
        self.assertEqual(mod.strip_details('<details type="tool_calls">x\ny</details>\nHello'), "Hello")
        self.assertEqual(mod.split_end_marker("[PASS]", "[PASS]"), ("", True))
        self.assertEqual(mod.split_end_marker("  [PASS]\n", "[PASS]"), ("", True))
        self.assertEqual(mod.split_end_marker("ok [PASS]", "[PASS]"), ("ok", False))  # mixed into text: not a yield
        self.assertEqual(mod.split_end_marker("ok", "[PASS]"), ("ok", False))


def tool_output(name, args, text, call_id="call-1"):
    """A saved message's `output`, in the shape Open WebUI stores tool calls."""
    return [
        {"type": "reasoning", "id": "r1"},
        {"type": "function_call", "id": call_id, "call_id": call_id, "name": name, "arguments": args, "status": "completed"},
        {"type": "function_call_output", "id": "fco-" + call_id, "call_id": call_id, "output": [{"type": "input_text", "text": text}], "status": "completed"},
        {"type": "message", "id": "m1", "role": "assistant", "content": []},
    ]


class CarryTests(unittest.TestCase):
    def test_extract_keeps_only_wanted_tools_and_pairs_by_call_id(self):
        out = tool_output("agent_notes_read_tagged", '{"tags":"x"}', "PERSONA", "c1") + tool_output("get_current_timestamp", "{}", "12:00", "c2")
        got = mod.extract_carry(out, ["agent_notes_read_tagged"])
        self.assertEqual(got, [{"name": "agent_notes_read_tagged", "arguments": '{"tags":"x"}', "text": "PERSONA"}])
        self.assertEqual(mod.extract_carry(None, ["agent_notes_read_tagged"]), [])
        self.assertEqual(mod.extract_carry([{"type": "function_call_output", "call_id": "nope", "output": []}], ["a"]), [])

    def test_format_is_stable_and_empty_when_nothing(self):
        items = [{"name": "t", "arguments": "{}", "text": "BODY"}]
        self.assertEqual(mod.format_carry(items), mod.format_carry(list(items)))
        self.assertIn("BODY", mod.format_carry(items))
        self.assertEqual(mod.format_carry([]), "")

    def test_carried_block_goes_into_system_message_only(self):
        msgs = mod.build_messages("Mara", "Hannah", "Gordon", [{"speaker": "Gordon", "text": "q"}], "[PASS]", "CARRIED-BLOCK")
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn("CARRIED-BLOCK", msgs[0]["content"])
        self.assertNotIn("CARRIED-BLOCK", mod.build_messages("Mara", "Hannah", "Gordon", [{"speaker": "Gordon", "text": "q"}], "[PASS]")[0]["content"])

    def _pair(self, **kw):
        persona = tool_output("agent_notes_read_tagged", '{"tags":"mara-core"}', "MARA-PERSONA-TEXT") + tool_output("get_current_timestamp", "{}", "NOW-TEXT", "c9")
        mara = FakeOWUI(["m1", "m2", "m3"], outputs=[persona], **kw)
        hannah = FakeOWUI(["h1", "h2", "h3"])
        return mara, hannah

    def test_second_turn_of_same_agent_gets_first_turns_results(self):
        mara, hannah = self._pair()
        run_pipe(make_pipe(MAX_TURNS=4), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}), chat_id="front1")
        first = mara.completions[0]["messages"][0]["content"]
        second = mara.completions[1]["messages"][0]["content"]
        self.assertNotIn("MARA-PERSONA-TEXT", first)
        self.assertIn("MARA-PERSONA-TEXT", second)
        self.assertNotIn("NOW-TEXT", second)  # timestamps are redone every turn, not carried
        self.assertNotIn("MARA-PERSONA-TEXT", hannah.completions[0]["messages"][0]["content"])
        self.assertNotIn("MARA-PERSONA-TEXT", hannah.completions[1]["messages"][0]["content"])

    def test_carried_text_is_identical_every_turn(self):
        mara, hannah = self._pair()
        run_pipe(make_pipe(MAX_TURNS=6), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}), chat_id="front1")
        self.assertEqual(mara.completions[1]["messages"][0], mara.completions[2]["messages"][0])

    def test_results_survive_to_the_next_human_message_in_the_same_chat_only(self):
        pipe = make_pipe(MAX_TURNS=1)
        mara, hannah = self._pair()
        router = Router(**{MARA: mara, HANNAH: hannah})
        run_pipe(pipe, [{"role": "user", "content": "hi"}], router, chat_id="front1")
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "\n\n**Mara:** m1\n\n"}, {"role": "user", "content": "again"}]
        run_pipe(pipe, history, router, chat_id="front1")
        self.assertIn("MARA-PERSONA-TEXT", mara.completions[1]["messages"][0]["content"])
        run_pipe(pipe, history, router, chat_id="another-chat")
        self.assertNotIn("MARA-PERSONA-TEXT", mara.completions[2]["messages"][0]["content"])

    def test_off_over_cap_and_ttl_do_not_carry(self):
        for overrides in ({"CARRY_CONTEXT": False}, {"CARRY_MAX_CHARS": 10}):
            mara, hannah = self._pair()
            run_pipe(make_pipe(MAX_TURNS=4, **overrides), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}), chat_id="c")
            self.assertNotIn("MARA-PERSONA-TEXT", mara.completions[1]["messages"][0]["content"], overrides)
        pipe = make_pipe(MAX_TURNS=1, CARRY_TTL_MINUTES=0)
        pipe._carry_put(("c", "Mara"), "BLOCK")
        self.assertEqual(pipe._carry_get(("c", "Mara")), "")

    def test_nothing_carried_means_it_tries_again_next_turn(self):
        mara = FakeOWUI(["m1", "m2", "m3"], outputs=[[], tool_output("agent_notes_read_tagged", "{}", "LATE-PERSONA")])
        run_pipe(make_pipe(MAX_TURNS=6), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: FakeOWUI(["h1", "h2", "h3"])}), chat_id="c")
        self.assertNotIn("LATE-PERSONA", mara.completions[1]["messages"][0]["content"])  # her first turn loaded nothing, so nothing to show yet
        self.assertIn("LATE-PERSONA", mara.completions[2]["messages"][0]["content"])  # caught on the second try and carried from then on

    def test_cache_is_bounded(self):
        pipe = make_pipe()
        for i in range(60):
            pipe._carry_put((f"c{i}", "Mara"), "B")
        self.assertLessEqual(len(pipe._carry), 50)


class PipeTests(unittest.TestCase):
    def test_alternates_and_stops_at_max_turns(self):
        mara = FakeOWUI(["m1", "m2"])
        hannah = FakeOWUI(["h1", "h2"])
        out = run_pipe(make_pipe(MAX_TURNS=4), [{"role": "user", "content": "hello"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertEqual([i for i in ["m1", "h1", "m2", "h2"] if i in out], ["m1", "h1", "m2", "h2"])
        self.assertLess(out.index("m1"), out.index("h1"))
        self.assertLess(out.index("h1"), out.index("m2"))
        self.assertLess(out.index("m2"), out.index("h2"))
        self.assertEqual(len(mara.completions), 2)
        self.assertEqual(len(hannah.completions), 2)

    def test_second_agent_sees_first_agent_as_user_message_and_tools_are_passed(self):
        mara = FakeOWUI(["from mara"], tool_ids=["agent_notes", "server:0"])
        hannah = FakeOWUI(["from hannah"], tool_ids=["agent_notes"])
        run_pipe(make_pipe(MAX_TURNS=2), [{"role": "user", "content": "hello"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertEqual(mara.completions[0]["tool_ids"], ["agent_notes", "server:0"])
        self.assertEqual(hannah.completions[0]["tool_ids"], ["agent_notes"])
        h_msgs = hannah.completions[0]["messages"]
        self.assertEqual(h_msgs[-1]["role"], "user")
        self.assertIn("[Mara]: from mara", h_msgs[-1]["content"])
        self.assertTrue(hannah.completions[0]["chat_id"].startswith("chat"))

    def test_tool_ids_valve_overrides_preset(self):
        mara = FakeOWUI(["x"], tool_ids=["from_preset"])
        hannah = FakeOWUI(["y"])
        run_pipe(make_pipe(MAX_TURNS=1, MARA_TOOL_IDS="a, b"), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertEqual(mara.completions[0]["tool_ids"], ["a", "b"])

    def test_agent_can_yield_the_floor(self):
        mara = FakeOWUI(["m1", "m2"])
        hannah = FakeOWUI(["[PASS]"])
        out = run_pipe(make_pipe(MAX_TURNS=4), [{"role": "user", "content": "hello"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertIn("**Mara:** m1", out)
        self.assertIn("Hannah has nothing to add", out)
        self.assertEqual(len(mara.completions), 1)  # round ended, Mara never got a second turn

    def test_marker_inside_a_real_reply_does_not_end_the_round(self):
        mara = FakeOWUI(["[PASS]\n\nHello Hannah, welcome.", "m2"])
        hannah = FakeOWUI(["h1", "h2"])
        out = run_pipe(make_pipe(MAX_TURNS=2), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertIn("**Mara:** Hello Hannah, welcome.", out)
        self.assertNotIn("[PASS]", out)
        self.assertIn("**Hannah:** h1", out)  # Hannah still got her turn

    def test_named_agent_speaks_first(self):
        mara = FakeOWUI(["m"])
        hannah = FakeOWUI(["h"])
        out = run_pipe(make_pipe(MAX_TURNS=1), [{"role": "user", "content": "Hannah, how are you?"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertIn("**Hannah:** h", out)
        self.assertEqual(mara.completions, [])

    def test_followup_message_carries_earlier_turns(self):
        mara = FakeOWUI(["m"])
        hannah = FakeOWUI(["h"])
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "\n\n**Mara:** earlier mara\n\n\n\n**Hannah:** earlier hannah\n\n"},
            {"role": "user", "content": "second"},
        ]
        run_pipe(make_pipe(MAX_TURNS=1), history, Router(**{MARA: mara, HANNAH: hannah}))
        sent = mara.completions[0]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "user"])
        self.assertEqual(sent[2]["content"], "earlier mara")
        self.assertIn("[Hannah]: earlier hannah", sent[3]["content"])
        self.assertIn("[Gordon]: second", sent[3]["content"])

    def test_backing_chats_deleted_unless_kept(self):
        mara, hannah = FakeOWUI(["m"]), FakeOWUI(["h"])
        run_pipe(make_pipe(MAX_TURNS=2), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertEqual(mara.deleted, ["chat0"])
        self.assertEqual(hannah.deleted, ["chat0"])
        mara, hannah = FakeOWUI(["m"]), FakeOWUI(["h"])
        run_pipe(make_pipe(MAX_TURNS=2, KEEP_BACKING_CHATS=True), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertEqual(mara.deleted + hannah.deleted, [])

    def test_server_error_is_reported_not_raised(self):
        mara = FakeOWUI(["m"])
        hannah = FakeOWUI([], fail_new_chat=True)
        out = run_pipe(make_pipe(MAX_TURNS=4), [{"role": "user", "content": "Hannah?"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertIn("Hannah couldn't answer", out)
        self.assertEqual(mara.completions, [])

    def test_timeout_is_reported_and_chat_cleaned_up(self):
        mara = FakeOWUI([], never_done=True)
        hannah = FakeOWUI([])
        out = run_pipe(make_pipe(MAX_TURNS=2, TURN_TIMEOUT_SECONDS=0), [{"role": "user", "content": "hi"}], Router(**{MARA: mara, HANNAH: hannah}))
        self.assertIn("Mara couldn't answer: no reply within", out)
        self.assertEqual(mara.deleted, ["chat0"])

    def test_missing_key_is_reported(self):
        pipe = mod.Pipe()
        pipe.valves = pipe.Valves(MARA_URL=MARA, MARA_KEY="", HANNAH_URL=HANNAH, HANNAH_KEY="hk")
        out = run_pipe(pipe, [{"role": "user", "content": "hi"}], Router(**{MARA: FakeOWUI([]), HANNAH: FakeOWUI([])}))
        self.assertIn("valves must all be set", out)

    def test_tasks_do_not_wake_the_agents(self):
        mara, hannah = FakeOWUI(["m"]), FakeOWUI(["h"])

        async def go():
            with patch.object(mod, "requests", Router(**{MARA: mara, HANNAH: hannah})):
                return await make_pipe().pipe({"messages": [{"role": "user", "content": "hi"}]}, __task__="title_generation")

        self.assertIn("title", asyncio.run(go()))
        self.assertEqual(mara.completions + hannah.completions, [])

    def test_pipes_lists_one_named_model(self):
        self.assertEqual(make_pipe().pipes(), [{"id": "agent_duo", "name": "Mara + Hannah"}])


if __name__ == "__main__":
    unittest.main()
