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


def read_midi(data, any_channel=False):
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
        elif status & 0xF0 == 0xC0:  # program change: one data byte
            notes.append((tick, "program", track[pos], 0, status & 0x0F))
            pos += 1
        else:
            if any_channel:
                notes.append((tick, "on" if status & 0xF0 == 0x90 else "off", track[pos], track[pos + 1], status & 0x0F))
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

    def test_a_lane_that_is_a_step_off_is_padded_or_cut(self):
        lanes, steps = dm.parse_pattern("hat: x.x.x.x.x.x.x.x.\nkick: x....\nsnare: ....x.......x...")
        self.assertEqual((steps, len(lanes["kick"]), len(lanes["hat"]), len(lanes["snare"])), (16, 16, 16, 16))
        lanes, steps = dm.parse_pattern("kick: x...x...x...x...\nsnare: ....x.......x....")  # 17 steps: the stray one is cut
        self.assertEqual((steps, len(lanes["snare"])), (16, 16))

    def test_fit_lengths_picks_the_length_most_lines_fit_and_explains(self):
        lanes = {"kick": [1.0] + [0.0] * 15, "hat": [0.8] * 15, "rim": [0.4] * 18}
        cells = {"bass": [[36]] + [None] * 15}
        fixed, cells, steps, notes = dm.fit_lengths(lanes, cells)
        self.assertEqual(steps, 16)
        self.assertEqual([len(v) for v in fixed.values()], [16, 16, 16])
        self.assertEqual(len(cells["bass"]), 16)
        self.assertEqual(len(notes), 2)
        self.assertIn("hat had 15 steps", notes[0])
        self.assertIn("rim had 18 steps, so the last 2 (which held notes) were cut", notes[1])

    def test_fit_lengths_keeps_a_longer_line_that_others_divide_and_marks_lost_notes(self):
        lanes = {"kick": [1.0] * 16, "snare": [1.0] * 16, "hat": [0.8] * 32}
        fixed, _c, steps, notes = dm.fit_lengths(lanes, {})
        self.assertEqual((steps, len(fixed["kick"])), (32, 32))
        self.assertEqual(notes, [])
        quiet = {"kick": [1.0] + [0.0] * 15, "snare": [0.0] * 12 + [1.0] + [0.0] * 3, "hat": [0.8, 0.0] * 8 + [0.0, 0.0]}
        _l, _c, steps, notes = dm.fit_lengths(quiet, {})
        self.assertEqual((steps, notes), (16, ["hat had 18 steps, so the last 2 were cut"]))  # only rests were cut: no warning about lost notes

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


class TonalParseTests(unittest.TestCase):
    def test_note_numbers(self):
        n = dm.note_number
        self.assertEqual([n("C4", 4), n("A4", 4), n("C2", 4), n("F#3", 4), n("Bb2", 4), n("bb2", 4), n("b3", 4), n("C0", 4)], [60, 69, 36, 54, 46, 46, 59, 12])
        self.assertEqual(dm.note_number("E", 2), 40)  # no octave: the line's default
        self.assertEqual(dm.note_number("Db4", 4), dm.note_number("C#4", 4))

    def test_bad_notes_are_explained(self):
        for bad in ("H2", "C99", "xyz", "C9"):
            with self.assertRaises(dm.BeatError):
                dm.note_number(bad, 4)
        with self.assertRaisesRegex(dm.BeatError, "not a note"):
            dm.parse_tonal("bass", "C2 banana")

    def test_cells_rests_holds_chords_and_label(self):
        cells = dm.parse_tonal("melody", "melody: C4 - . E4+G4+C4 | .. -")
        self.assertEqual(cells, [[60], "-", None, [60, 64, 67], None, None, "-"])

    def test_notes_with_holds_and_loop(self):
        cells = dm.parse_tonal("bass", "C2 - . D2")
        self.assertEqual(dm.tonal_notes(cells, 8, "bass"), [(0, 2, [36]), (3, 1, [38]), (4, 2, [36]), (7, 1, [38])])
        self.assertEqual(dm.tonal_notes(dm.parse_tonal("bass", "- C2 - ."), 4, "bass"), [(1, 2, [36])])  # a hold with nothing to hold is a rest
        with self.assertRaisesRegex(dm.BeatError, "does not divide"):
            dm.tonal_notes(dm.parse_tonal("bass", "C2 . ."), 16, "bass")

    def test_empty_line(self):
        with self.assertRaises(dm.BeatError):
            dm.parse_tonal("bass", "   ")


class MidiTests(unittest.TestCase):
    def test_bass_and_melody_get_their_own_channels_and_programs(self):
        tonal = {"bass": dm.tonal_notes(dm.parse_tonal("bass", "C2 - . ."), 4, "bass"), "melody": dm.tonal_notes(dm.parse_tonal("melody", "E4+G4 . . ."), 4, "melody")}
        events = read_midi(dm.build_midi({}, 4, 1, 120, 0, "t", tonal, "pluck"), any_channel=True)[2]
        self.assertIn((0, "program", 38, 0, 0), events)
        self.assertIn((0, "program", 24, 0, 1), events)
        ons = sorted((t, n, c) for t, k, n, _v, c in events if k == "on")
        self.assertEqual(ons, [(0, 36, 0), (0, 64, 1), (0, 67, 1)])
        offs = {n: t for t, k, n, _v, _c in events if k == "off"}
        self.assertEqual(offs[36], 2 * 120 - 10)  # held for two steps, a hair short of the next
        self.assertEqual(offs[64], 120 - 10)

    def test_notes_repeat_with_the_pattern_and_swing_moves_odd_steps(self):
        tonal = {"melody": [(1, 1, [60])]}
        events = read_midi(dm.build_midi({}, 4, 2, 100, 50, ""), any_channel=True)[2]
        self.assertEqual([e for e in events if e[1] == "on"], [])
        events = read_midi(dm.build_midi({}, 4, 2, 100, 50, "", tonal), any_channel=True)[2]
        self.assertEqual([t for t, k, *_ in events if k == "on"], [120 + 60, 4 * 120 + 120 + 60])

    def test_notes_survive_a_clean_parse_of_drums_only(self):
        lanes, steps = dm.parse_pattern("kick: x...")
        self.assertTrue(all(len(e) == 5 for e in read_midi(dm.build_midi(lanes, steps, 1, 100, 0, ""), any_channel=True)[2]))


class MidiFormatTests(unittest.TestCase):
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


class TonalRenderTests(unittest.TestCase):
    def samples(self, data):
        with wave.open(BytesIO(data)) as w:
            frames = w.readframes(w.getnframes())
            return w.getframerate(), [struct.unpack_from("<h", frames, i * 2)[0] for i in range(w.getnframes())]

    def pitch(self, rate, samples, start, end):
        window = samples[int(rate * start):int(rate * end)]
        crossings = sum(1 for a, b in zip(window, window[1:]) if a < 0 <= b)
        return crossings / (end - start)

    def test_every_sound_plays_at_the_right_pitch(self):
        notes = [(0, 8, [69])]  # A4 = 440 Hz held for 8 steps at 60 BPM = 2 s
        for sound in ("square", "saw", "sine", "bass"):
            tonal = {"melody" if sound != "bass" else "bass": notes}
            rate, samples = self.samples(dm.render_wav({}, 8, 1, 60, 0, 16000, tonal, sound if sound != "bass" else "square"))
            self.assertGreater(max(abs(v) for v in samples), 2000, sound)
            self.assertAlmostEqual(self.pitch(rate, samples, 0.3, 1.3), 440, delta=25, msg=sound)

    def test_pluck_and_pad_make_sound_and_fade(self):
        for sound in ("pluck", "pad"):
            rate, samples = self.samples(dm.render_wav({}, 4, 1, 60, 0, 16000, {"melody": [(0, 4, [57])]}, sound))
            self.assertGreater(max(abs(v) for v in samples[:int(rate * 0.5)]), 1500, sound)

    def test_a_rest_is_silent_and_a_hold_keeps_sounding(self):
        tonal = {"melody": [(0, 4, [60])]}  # one second at 60 BPM, then three silent steps
        rate, samples = self.samples(dm.render_wav({}, 8, 1, 60, 0, 8000, tonal, "sine"))
        loud = lambda a, b: max(abs(v) for v in samples[int(rate * a):int(rate * b)])  # noqa: E731
        self.assertGreater(loud(0.8, 0.95), 1500)
        self.assertLess(loud(1.3, 1.9), 200)

    def test_drums_alone_and_tonal_alone_both_render(self):
        lanes, steps = dm.parse_pattern("kick: x...")
        self.assertTrue(dm.render_wav(lanes, steps, 1, 100, 0, 8000))
        self.assertTrue(dm.render_wav({}, 4, 1, 100, 0, 8000, {"bass": [(0, 1, [36])]}))


SONG = """[intro]
kick: x... .... x... ....
hat: x.x. x.x. x.x. x.x.
bass: C1 . . . | . . . . | C1 . . . | . . . .
repeats: 2

[drop]
kick: x... x... x... x...
snare: .... x... .... x...
bass: C1 - - - | G1 - - - | Ab1 - - - | F1 - - -
melody: C3 . . . | Eb3 . . . | G3 . . . | C4 . . .
sound: saw
bpm: 70
repeats: 4
"""


class SongParseTests(unittest.TestCase):
    def test_sections_settings_and_lines(self):
        sections = dm.parse_song(SONG)
        self.assertEqual(list(sections), ["intro", "drop"])
        self.assertEqual(sections["intro"]["drums"], ["kick: x... .... x... ....", "hat: x.x. x.x. x.x. x.x."])
        self.assertEqual(sections["drop"]["settings"], {"sound": "saw", "bpm": "70", "repeats": "4"})
        self.assertTrue(sections["drop"]["melody"].startswith("C3"))

    def test_header_spellings_and_no_header(self):
        self.assertEqual(list(dm.parse_song("# Build Up\nkick: x...\n\n## drop:\nkick: x...\n\nTease:\nkick: x...")), ["build_up", "drop", "tease"])
        self.assertEqual(list(dm.parse_song("kick: x...\nbass: C2 . . .")), ["main"])
        self.assertEqual(dm.parse_song("[A]\nkick: x...\n---\n")["a"]["drums"], ["kick: x..."])
        with self.assertRaises(dm.BeatError):
            dm.parse_song("   \n\n")
        with self.assertRaisesRegex(dm.BeatError, "defined twice"):
            dm.parse_song("[a]\nkick: x...\n[A]\nkick: x...")

    def test_order_parsing(self):
        sections = dm.parse_song("[intro]\nkick: x...\n[build up]\nkick: x...\n[drop]\nkick: x...")
        self.assertEqual(dm.parse_order("intro, build up x2, drop", sections), ["intro", "build_up", "build_up", "drop"])
        self.assertEqual(dm.parse_order("intro > drop*3", sections), ["intro", "drop", "drop", "drop"])
        self.assertEqual(dm.parse_order("intro drop x2", sections), ["intro", "drop", "drop"])
        self.assertEqual(dm.parse_order("", sections), ["intro", "build_up", "drop"])
        self.assertEqual(dm.parse_order(None, sections), ["intro", "build_up", "drop"])
        with self.assertRaisesRegex(dm.BeatError, "'chorus', which is not a section"):
            dm.parse_order("intro, chorus", sections)

    def test_section_segment_uses_defaults_and_its_own_settings(self):
        sections = dm.parse_song(SONG)
        notes = []
        intro = dm.section_segment(sections["intro"], 100, 0, "square", notes)
        drop = dm.section_segment(sections["drop"], 100, 0, "square", notes)
        self.assertEqual((intro["steps"], intro["repeats"], intro["bpm"], intro["sound"]), (16, 2, 100, "square"))
        self.assertEqual((drop["steps"], drop["repeats"], drop["bpm"], drop["sound"]), (16, 4, 70, "saw"))
        self.assertEqual(notes, [])

    def test_section_errors_name_the_section(self):
        with self.assertRaisesRegex(dm.BeatError, "'empty' has no drums"):
            dm.section_segment(dm.parse_song("[empty]\nrepeats: 2")["empty"], 100, 0, "square", [])
        with self.assertRaisesRegex(dm.BeatError, r"\[a\] repeats must be between"):
            dm.section_segment(dm.parse_song("[a]\nkick: x...\nrepeats: 999")["a"], 100, 0, "square", [])
        with self.assertRaisesRegex(dm.BeatError, "sound 'kazoo'"):
            dm.section_segment(dm.parse_song("[a]\nkick: x...\nsound: kazoo")["a"], 100, 0, "square", [])

    def test_mismatched_lines_in_a_section_are_fixed_and_named(self):
        notes = []
        seg = dm.section_segment(dm.parse_song("[a]\nkick: x... x... x... x...\nhat: x.x. x.x. x.x. x.x\nbass: C2 . . .|. . . .|. . . .|. . . . .")["a"], 100, 0, "square", notes)
        self.assertEqual(seg["steps"], 16)
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(n.startswith("[a] ") for n in notes))


class SongRenderTests(unittest.TestCase):
    def test_tempo_changes_and_programs_in_the_midi(self):
        sections = dm.parse_song(SONG)
        segs = [dm.section_segment(sections[k], 100, 0, "square", []) for k in ("intro", "drop")]
        data = dm.build_midi_song(segs, "t")
        tempos = []
        pos = data.index(b"\xff\x51\x03")
        while pos != -1:
            tempos.append(int.from_bytes(data[pos + 3:pos + 6], "big"))
            pos = data.find(b"\xff\x51\x03", pos + 1)
        self.assertEqual(tempos, [600000, 857143])
        events = read_midi(data, any_channel=True)[2]
        drop_start = 2 * 16 * 120
        self.assertTrue(any(t == drop_start and k == "program" and n == 81 for t, k, n, _v, _c in events))  # saw lead from the drop on
        self.assertEqual(min(t for t, k, n, _v, c in events if k == "on" and c == 1), drop_start)  # the melody only plays in the drop

    def test_audio_length_is_the_sum_of_the_parts_at_their_own_tempos(self):
        sections = dm.parse_song(SONG)
        segs = [dm.section_segment(sections["intro"], 100, 0, "square", []), dm.section_segment(sections["drop"], 100, 0, "square", [])]
        wav = dm.render_song(segs, 8000)
        with wave.open(BytesIO(wav)) as w:
            seconds = w.getnframes() / w.getframerate()
        wanted = 32 * 0.15 + 64 * (60 / 70 / 4)  # intro 32 steps at 100 BPM; drop 64 steps at 70 BPM
        self.assertGreater(seconds, wanted)
        self.assertLess(seconds, wanted + 2.5)  # plus the last cymbal or note tail

    def test_one_segment_wrappers_match_the_song_functions(self):
        lanes, steps = dm.parse_pattern("kick: x...")
        self.assertEqual(dm.render_wav(lanes, steps, 2, 100, 0, 8000), dm.render_song([dm.one_segment(lanes, steps, 2, 100, 0)], 8000))


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

    def test_bass_and_melody_with_drums(self):
        out = self.run_tool(self.tool.make_beat(BASIC, bass="C2 . . C2 . . G1 . A1 . . . G1 - - .", melody="E4 - . G4 - - A4 - G4 . . . E4 - - .", melody_sound="pluck", title="Full"))
        self.assertIn("Made the beat: 16 steps x 2", out)
        meta = json.loads(next(Path(self.dir).glob("*.json")).read_text())
        self.assertEqual((meta["melody_sound"], meta["bass"][:5]), ("pluck", "C2 . "))
        events = read_midi(next(Path(self.dir).glob("*.mid")).read_bytes(), any_channel=True)[2]
        self.assertEqual({c for *_x, c in events}, {0, 1, 9})

    def test_off_by_one_lines_still_make_a_beat_and_the_reply_says_what_was_fixed(self):
        out = self.run_tool(self.tool.make_beat("kick: x... x... x... x...\nsnare: .... x... .... x...\nhat: x.x. x.x. x.x. x.x", bass="C2 . . . C2 . . . G1 . . . A1 . .", melody="E4 - . G4 | - - A4 - | G4 . . . | E4 - - . ."))
        self.assertIn("Made the beat: 16 steps x 2", out)
        self.assertIn("hat had 15 steps", out)
        self.assertIn("bass had 15 steps", out)
        self.assertIn("melody had 17 steps", out)
        self.assertIn("exactly 16 steps", out)
        self.assertEqual(len(list(Path(self.dir).glob("*.mid"))), 1)

    def test_bass_only_needs_no_drums_and_a_short_drum_pattern_loops_to_fit(self):
        out = self.run_tool(self.tool.make_beat(bass="C2 . . . G1 . . . C2 . . . G1 . . ."))
        self.assertIn("Made the beat: 16 steps", out)
        out = self.run_tool(self.tool.make_beat("kick: x...", bass="C2 . . . G1 . . . C2 . . . G1 . . ."))
        self.assertIn("Made the beat: 16 steps", out)

    def test_tonal_mistakes_are_explained(self):
        for kwargs, expect in (
            ({"melody": "C4 . H4 ."}, "not a note"),
            ({"pattern": BASIC, "melody": "C4 .", "melody_sound": "kazoo"}, "not one of"),
            ({}, "at least one of"),
        ):
            out = self.run_tool(self.tool.make_beat(**kwargs))
            self.assertTrue(out.startswith("Could not make the beat"), out)
            self.assertIn(expect, out)

    def test_list_beats_shows_bass_and_melody(self):
        self.run_tool(self.tool.make_beat(BASIC, bass="C2 . . .", melody="E4 . . .", melody_sound="saw", title="x"))
        out = self.run_tool(self.tool.list_beats())
        self.assertIn("bass: C2 . . .", out)
        self.assertIn("melody (saw): E4 . . .", out)

    def test_make_song_end_to_end(self):
        out = self.run_tool(self.tool.make_song(SONG, order="intro, drop, drop", title="Pressure"))
        self.assertIn("Made the track: 3 parts (intro, drop, drop)", out)
        meta = json.loads(next(Path(self.dir).glob("*.json")).read_text())
        self.assertEqual((meta["order"], meta["title"]), (["intro", "drop", "drop"], "Pressure"))
        self.assertEqual(sorted(p.suffix for p in Path(self.dir).iterdir()), [".json", ".mid", ".wav"])
        listing = self.run_tool(self.tool.list_beats())
        self.assertIn("order: intro, drop, drop", listing)

    def test_make_song_default_order_and_a_single_unnamed_section(self):
        out = self.run_tool(self.tool.make_song("kick: x... x... x... x...\nbass: C2 . . . | . . . . | G1 . . . | . . . .", bpm="120"))
        self.assertIn("Made the track: 1 parts (main)", out)

    def test_make_song_mistakes_are_explained(self):
        for kwargs, expect in (
            ({"song": SONG, "order": "intro, chorus"}, "not a section"),
            ({"song": "   "}, "song is empty"),
            ({"song": SONG, "melody_sound": "kazoo"}, "not one of"),
            ({"song": SONG, "bpm": "10"}, "bpm must be between"),
            ({"song": "[a]\nrepeats: 2"}, "no drums, bass or melody"),
        ):
            out = self.run_tool(self.tool.make_song(**kwargs))
            self.assertTrue(out.startswith("Could not make the song"), out)
            self.assertIn(expect, out)
        self.assertEqual(list(Path(self.dir).iterdir()), [])

    def test_make_song_limits(self):
        self.tool.valves.MAX_SECONDS = 10
        self.assertIn("seconds; the limit is 10", self.run_tool(self.tool.make_song(SONG, order="intro, drop, drop, drop")))
        self.tool.valves.MAX_SECONDS = 300
        self.tool.valves.MAX_ORDER_ITEMS = 3
        self.assertIn("order has 4 parts", self.run_tool(self.tool.make_song(SONG, order="intro, drop x3")))
        self.tool.valves.MAX_SECTIONS = 1
        self.assertIn("2 sections; the limit is 1", self.run_tool(self.tool.make_song(SONG)))

    def test_make_song_reply_lists_fixed_lines(self):
        out = self.run_tool(self.tool.make_song("[a]\nkick: x... x... x... x...\nhat: x.x. x.x. x.x. x.x"))
        self.assertIn("[a] hat had 15 steps", out)

    def test_list_beats(self):
        self.assertEqual(self.run_tool(self.tool.list_beats()), "No beats have been made yet.")
        self.run_tool(self.tool.make_beat(BASIC, title="one"))
        out = self.run_tool(self.tool.list_beats())
        self.assertIn("one (100 BPM", out)
        self.assertIn("kick: x...x...x...x...", out)


if __name__ == "__main__":
    unittest.main()
