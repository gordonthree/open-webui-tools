import asyncio
import json
import struct
import tempfile
import unittest
import wave
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import drum_machine as dm

BASIC = "kick: x...x...x...x...\nsnare: ....x.......x...\nhat: x.x.x.x.x.x.x.x."


def read_midi(data):
    """(ticks per beat, tempo us/beat, [(tick, kind, note, velocity)]) from a format-0 file."""
    assert data[:4] == b"MThd"
    division = struct.unpack(">H", data[12:14])[0]
    assert data[14:18] == b"MTrk"
    length = struct.unpack(">I", data[18:22])[0]
    track, pos, tick, tempo, notes = data[22:22 + length], 0, 0, None, []

    def vlq():
        nonlocal pos
        value = 0
        while True:
            b = track[pos]; pos += 1
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                return value
    while pos < len(track):
        tick += vlq()
        status = track[pos]; pos += 1
        if status == 0xFF:
            kind = track[pos]; size = track[pos + 1]; pos += 2
            if kind == 0x51:
                tempo = int.from_bytes(track[pos:pos + 3], "big")
            pos += size
        else:
            notes.append((tick, "on" if status & 0xF0 == 0x90 else "off", track[pos], track[pos + 1]))
            assert status & 0x0F == 9
            pos += 2
    return division, tempo, notes


class ParseTests(unittest.TestCase):
    def test_basic_lines(self):
        lanes, steps = dm.parse_pattern(BASIC)
        self.assertEqual(steps, 16)
        self.assertEqual(set(lanes), {"kick", "snare", "hat"})
        self.assertEqual([i for i, h in enumerate(lanes["kick"]) if h], [0, 4, 8, 12])

    def test_accents_ghosts_and_separators(self):
        lanes, steps = dm.parse_pattern("snare: X..g | x... | ....| ....")
        self.assertEqual(steps, 16)
        self.assertEqual(lanes["snare"][0], 1.0)
        self.assertLess(lanes["snare"][3], lanes["snare"][4])

    def test_spellings_and_json(self):
        a, _ = dm.parse_pattern("BD = x...\nHi-Hat x.x.\nOpen Hat ..x.")
        self.assertEqual(set(a), {"kick", "hat", "open_hat"})
        b, _ = dm.parse_pattern('{"kick": "x...", "cp": "..x."}')
        self.assertEqual(set(b), {"kick", "clap"})
        c, _ = dm.parse_pattern("kick: 1000\nhat: 1010")
        self.assertEqual([bool(h) for h in c["hat"]], [True, False, True, False])

    def test_short_lane_repeats_when_it_divides(self):
        lanes, steps = dm.parse_pattern("hat: x.x.x.x.x.x.x.x.\nkick: x...")
        self.assertEqual((steps, [i for i, h in enumerate(lanes["kick"]) if h]), (16, [0, 4, 8, 12]))

    def test_uneven_lanes_are_refused_with_the_lengths(self):
        with self.assertRaisesRegex(dm.BeatError, "kick 5"):
            dm.parse_pattern("hat: x.x.x.x.x.x.x.x.\nkick: x....")

    def test_unknown_drum_and_unknown_step(self):
        with self.assertRaisesRegex(dm.BeatError, "Unknown drum"):
            dm.parse_pattern("banjo: x...")
        with self.assertRaisesRegex(dm.BeatError, "not a step"):
            dm.parse_pattern("kick: x?..")
        with self.assertRaises(dm.BeatError):
            dm.parse_pattern("   ")

    def test_same_drum_twice_merges(self):
        lanes, _ = dm.parse_pattern("kick: x...\nkick: ..x.")
        self.assertEqual([i for i, h in enumerate(lanes["kick"]) if h], [0, 2])


class MidiTests(unittest.TestCase):
    def test_notes_ticks_tempo_and_channel(self):
        lanes, steps = dm.parse_pattern("kick: X...\nsnare: ..x.")
        division, tempo, notes = read_midi(dm.build_midi(lanes, steps, 2, 120, 0, "t"))
        self.assertEqual((division, tempo), (480, 500000))
        ons = [(t, n, v) for t, k, n, v in notes if k == "on"]
        self.assertEqual(ons, [(0, 36, 127), (240, 38, 102), (480, 36, 127), (720, 38, 102)])
        self.assertEqual(len([1 for _t, k, *_ in notes if k == "off"]), 4)

    def test_swing_delays_only_odd_steps(self):
        lanes, steps = dm.parse_pattern("hat: xxxx")
        ons = [t for t, k, *_ in read_midi(dm.build_midi(lanes, steps, 1, 100, 50, ""))[2] if k == "on"]
        self.assertEqual(ons, [0, 120 + 60, 240, 360 + 60])


class RenderTests(unittest.TestCase):
    def wav_info(self, data):
        with wave.open(BytesIO(data)) as w:
            frames = w.readframes(w.getnframes())
            return w.getframerate(), w.getnframes(), [struct.unpack_from("<h", frames, i * 2)[0] for i in range(w.getnframes())]

    def test_length_and_hits_land_on_the_beat(self):
        lanes, steps = dm.parse_pattern("kick: x...")
        rate, frames, samples = self.wav_info(dm.render_wav(lanes, steps, 2, 60, 0, 8000))  # a step is 0.25 s; the kick rings for 0.45 s
        self.assertGreater(frames, int(rate * 2.0))  # two 1-second loops plus the drum's tail
        loud = lambda at: max(abs(v) for v in samples[int(rate * at):int(rate * (at + 0.03))])  # noqa: E731
        self.assertGreater(loud(0), 3000)
        self.assertGreater(loud(1.0), 3000)  # the second loop's hit
        self.assertLess(loud(0.6), 600)  # between hits, nothing is playing

    def test_same_pattern_renders_identically(self):
        lanes, steps = dm.parse_pattern(BASIC)
        self.assertEqual(dm.render_wav(lanes, steps, 1, 100, 0, 8000), dm.render_wav(lanes, steps, 1, 100, 0, 8000))

    def test_every_drum_makes_sound_without_clipping(self):
        for name in dm.LANES:
            lanes = {name: [1.0, 0.0, 0.0, 0.0]}
            _rate, _frames, samples = self.wav_info(dm.render_wav(lanes, 4, 1, 100, 0, 8000))
            self.assertGreater(max(abs(v) for v in samples), 500, name)
            self.assertLessEqual(max(abs(v) for v in samples), 32767, name)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tool = dm.Tools()
        self.tool.valves.BEAT_DIR = self.dir
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = False
        self.tool.valves.SAMPLE_RATE = 8000
        self.tool.valves.OUTPUT_FORMAT = "wav"

    def run_tool(self, coro):
        return asyncio.run(coro)

    def test_makes_files_and_a_sidecar_without_an_upload(self):
        out = self.run_tool(self.tool.make_beat(BASIC, bpm="110", repeats="2", title="Boom Bap"))
        self.assertIn("Made the beat: 16 steps x 2 at 110 BPM", out)
        self.assertIn("No playable link", out)
        names = sorted(p.suffix for p in Path(self.dir).iterdir())
        self.assertEqual(names, [".json", ".mid", ".wav"])
        meta = json.loads(next(Path(self.dir).glob("*.json")).read_text())
        self.assertEqual((meta["bpm"], meta["title"], meta["pattern"]["kick"]), (110, "Boom Bap", "x...x...x...x..."))

    def test_bad_input_is_explained_not_raised(self):
        for kwargs, expect in (({"pattern": "banjo: x"}, "Unknown drum"), ({"pattern": BASIC, "bpm": "999"}, "bpm must be between"),
                               ({"pattern": BASIC, "bpm": "fast"}, "bpm must be a number"), ({"pattern": BASIC, "swing": "90"}, "swing must be between")):
            out = self.run_tool(self.tool.make_beat(**kwargs))
            self.assertTrue(out.startswith("Could not make the beat"), out)
            self.assertIn(expect, out)
        self.assertEqual(list(Path(self.dir).iterdir()), [])

    def test_length_limit_names_a_repeats_value_that_fits(self):
        self.tool.valves.MAX_SECONDS = 10
        out = self.run_tool(self.tool.make_beat(BASIC, bpm="120", repeats="20"))
        self.assertIn("repeats=5", out)  # 16 steps at 120 BPM is 2 s

    def test_unset_placeholders_use_defaults(self):
        out = self.run_tool(self.tool.make_beat(BASIC, bpm="", repeats="none", swing="null", title="default"))
        self.assertIn("at 100 BPM", out)
        self.assertIn("x 2 ", out)

    def test_upload_links_and_attachment_event(self):
        self.tool.valves.UPLOAD_TO_OPEN_WEBUI = True
        events = []

        async def emit(e):
            events.append(e)

        uploads = []

        def fake_upload(base, key, data, name, timeout):
            uploads.append(name)
            return f"/api/v1/files/id{len(uploads)}/content", None

        with patch.object(dm, "upload_to_open_webui", fake_upload):
            out = self.run_tool(self.tool.make_beat(BASIC, title="x", __event_emitter__=emit))
        self.assertIn("[Play x](/api/v1/files/id1/content)", out)
        self.assertIn("[MIDI file](/api/v1/files/id2/content)", out)
        self.assertEqual([n.rsplit(".", 1)[1] for n in uploads], ["wav", "mid"])
        files = next(e for e in events if e["type"] == "files")
        self.assertEqual(len(files["data"]["files"]), 2)

    def test_mp3_falls_back_to_wav_when_ffmpeg_is_missing(self):
        self.tool.valves.OUTPUT_FORMAT = "mp3"
        with patch.object(dm.shutil, "which", return_value=None):
            self.run_tool(self.tool.make_beat(BASIC))
        self.assertEqual(sorted(p.suffix for p in Path(self.dir).iterdir()), [".json", ".mid", ".wav"])

    def test_list_beats(self):
        self.assertEqual(self.run_tool(self.tool.list_beats()), "No beats have been made yet.")
        self.run_tool(self.tool.make_beat(BASIC, title="one"))
        out = self.run_tool(self.tool.list_beats())
        self.assertIn("one (100 BPM", out)
        self.assertIn("kick: x...x...x...x...", out)


if __name__ == "__main__":
    unittest.main()
