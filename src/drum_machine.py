"""
title: Drum Machine
author: Gordon
version: 0.4.0
description: A step-sequencer for agents: drums, a bass line and a melody, one pattern (make_beat) or a whole track of named sections (make_song). The model writes a pattern as lanes of steps (kick: x...x...,
    bass: C2 . . . G1 . . .), the tool plays it on synthesized instruments and returns a short audio preview plus a standard MIDI file
    (drums on General MIDI channel 10) that opens in any music program. Pure Python: no GPU, no soundfont, no extra packages (MP3
    conversion uses ffmpeg when present).
requirements: requests
"""

import asyncio
import json
import math
import mimetypes
import random
import re
import shutil
import struct
import subprocess
import sys
import wave
from array import array
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel, Field

_UNSET_STRINGS = {"", "none", "null", "default", "n/a", "undefined"}


def is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


class BeatError(ValueError):
    """A problem with what the model asked for; the message says how to fix it."""


# --------------------------------------------------------------------------- #
# Lanes: name, General MIDI drum note, mixer level, accepted spellings
# --------------------------------------------------------------------------- #

LANES: Dict[str, Tuple[int, float]] = {
    "kick": (36, 1.0),
    "snare": (38, 0.8),
    "rim": (37, 0.6),
    "clap": (39, 0.75),
    "hat": (42, 0.35),
    "open_hat": (46, 0.4),
    "low_tom": (45, 0.8),
    "mid_tom": (47, 0.8),
    "high_tom": (50, 0.8),
    "crash": (49, 0.45),
    "ride": (51, 0.4),
    "cowbell": (56, 0.55),
}

ALIASES = {
    "bd": "kick", "bassdrum": "kick", "bass_drum": "kick", "kick_drum": "kick", "bass": "kick",
    "sd": "snare", "snaredrum": "snare", "snare_drum": "snare",
    "rimshot": "rim", "sidestick": "rim", "side_stick": "rim", "rm": "rim",
    "cp": "clap", "handclap": "clap", "hand_clap": "clap", "claps": "clap",
    "hh": "hat", "hihat": "hat", "hi_hat": "hat", "closed_hat": "hat", "closed_hihat": "hat", "closedhat": "hat", "ch": "hat", "chh": "hat", "hats": "hat",
    "oh": "open_hat", "openhat": "open_hat", "open_hihat": "open_hat", "open_hi_hat": "open_hat", "ohh": "open_hat",
    "tom": "mid_tom", "toms": "mid_tom", "tom_low": "low_tom", "lowtom": "low_tom", "floor_tom": "low_tom", "tom_floor": "low_tom",
    "tom_mid": "mid_tom", "midtom": "mid_tom", "tom_high": "high_tom", "hightom": "high_tom", "hi_tom": "high_tom",
    "cr": "crash", "crash_cymbal": "crash", "cymbal": "crash",
    "rd": "ride", "ride_cymbal": "ride",
    "cb": "cowbell", "cow_bell": "cowbell",
}

HIT_CHARS = {"x": 0.8, "X": 1.0, "g": 0.4, "1": 0.8}  # normal, accent, ghost, (1 = a 0/1 pattern's hit)
REST_CHARS = set(".-_0~")
IGNORED_CHARS = set(" |\t,")
STEPS_PER_BEAT = 4  # sixteenth notes; a 16-step lane is one 4/4 bar
MIDI_TICKS_PER_BEAT = 480
MIDI_TICKS_PER_STEP = MIDI_TICKS_PER_BEAT // STEPS_PER_BEAT
MIDI_CHANNEL = 9  # channel 10 in musicians' numbering: General MIDI's drum channel


# Bass and melody: one token per step, separated by spaces: a note name (C2, F#3, Bb4; C4 is middle C), a chord of notes joined by +
# (C4+E4+G4), . for a rest, - to keep holding the previous note.
SEMITONES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d)?$")
TONAL_DEFAULT_OCTAVE = {"bass": 2, "melody": 4}
BASS_PROGRAM = 38  # General MIDI "Synth Bass 1"
MELODY_SOUNDS = {"square": 80, "saw": 81, "sine": 11, "pluck": 24, "pad": 89}  # name -> General MIDI program
MIDI_LOW, MIDI_HIGH = 12, 108  # C0 to C8
TONAL_LEVEL = {"bass": 0.55, "melody": 0.3}
TONAL_CHANNEL = {"bass": 0, "melody": 1}
RELEASE_SECONDS = 0.06


def note_number(token: str, default_octave: int) -> int:
    m = NOTE_RE.match(token)
    if not m:
        raise BeatError(f"{token!r} is not a note. Write notes like C2, F#3 or Bb4, a chord as C4+E4+G4, . for a rest and - to hold the last note.")
    letter, accidental, octave = m.groups()
    midi = 12 * ((int(octave) if octave is not None else default_octave) + 1) + SEMITONES[letter.lower()] + (1 if accidental == "#" else -1 if accidental == "b" else 0)
    if not MIDI_LOW <= midi <= MIDI_HIGH:
        raise BeatError(f"{token!r} is outside the range C0 to C8.")
    return midi


def parse_tonal(kind: str, text: Any) -> List[Any]:
    """One step per entry: None (rest), "-" (hold the previous note) or a list of MIDI note numbers."""
    cleaned = re.sub(rf"^\s*(?:{kind}|lead)\s*[:=]\s*", "", str(text or ""), flags=re.I)
    tokens = [t for t in re.split(r"[\s|,]+", cleaned) if t]
    if not tokens:
        raise BeatError(f"{kind}: no steps. Write one token per step, e.g.  C2 . . . G1 . . .")
    cells: List[Any] = []
    for token in tokens:
        if set(token) <= set(".~_"):
            cells.extend([None] * len(token) if len(token) > 1 and token[0] == "." else [None])
        elif token == "-":
            cells.append("-")
        else:
            cells.append(sorted({note_number(part, TONAL_DEFAULT_OCTAVE[kind]) for part in token.split("+") if part}))
    return cells


def tonal_notes(cells: List[Any], steps: int, kind: str) -> List[Tuple[int, int, List[int]]]:
    """The lane (looped to fill `steps`) as notes: (first step, length in steps, [MIDI notes]). A hold with nothing to hold is a rest."""
    if steps % len(cells):
        raise BeatError(f"The {kind} line has {len(cells)} steps, which does not divide the other lanes' {steps}. Make it {steps} steps, or a length that divides {steps}.")
    full = cells * (steps // len(cells))
    notes: List[Tuple[int, int, List[int]]] = []
    current: Optional[List[Any]] = None
    for step, cell in enumerate(full):
        if isinstance(cell, list):
            current = [step, 1, cell]
            notes.append(current)  # type: ignore[arg-type]
        elif cell == "-" and current is not None:
            current[1] += 1
        else:
            current = None
    return [(a, b, c) for a, b, c in notes]


def lane_name(raw: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")
    key = ALIASES.get(key, key)
    if key not in LANES:
        raise BeatError(f"Unknown drum {raw!r}. Use: {', '.join(LANES)}.")
    return key


def parse_lane(name: str, steps: str) -> List[float]:
    """One lane's text as a list of hit strengths (0 = rest)."""
    out: List[float] = []
    for ch in steps:
        if ch in IGNORED_CHARS:
            continue
        if ch in HIT_CHARS:
            out.append(HIT_CHARS[ch])
        elif ch in REST_CHARS:
            out.append(0.0)
        else:
            raise BeatError(f"{name}: {ch!r} is not a step. Use x for a hit, X for an accent, g for a ghost note, . for a rest.")
    if not out:
        raise BeatError(f"{name}: no steps. Write the lane like  {name}: x...x...x...x...")
    return out


def parse_lanes(pattern: Any) -> Dict[str, List[float]]:
    """The drum lanes (name -> hit strengths, each as long as it was written; fit_lengths makes them match).

    Accepts lines like `kick: x...x...` (also `kick x...` or `kick=x...`), or a JSON object {"kick": "x...x..."}. Two lines for the
    same drum are merged."""
    if isinstance(pattern, dict):
        pairs = [(str(k), str(v)) for k, v in pattern.items()]
    else:
        text = str(pattern or "").strip()
        if text.startswith("{"):
            try:
                pairs = [(str(k), str(v)) for k, v in json.loads(text).items()]
            except (ValueError, AttributeError):
                raise BeatError("pattern looks like JSON but could not be read. Use lines like  kick: x...x...x...x...") from None
        else:
            pairs = []
            for line in re.split(r"[\n;]+", text):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^([A-Za-z][A-Za-z0-9 _-]*?)\s*[:=]\s*(.+)$", line) or re.match(r"^([A-Za-z][A-Za-z0-9_-]*\s+[A-Za-z][A-Za-z0-9_-]*)\s+(\S.*)$", line) \
                    or re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s+(\S.*)$", line)
                if not m:
                    raise BeatError(f"Could not read the line {line!r}. Write each drum on its own line:  kick: x...x...x...x...")
                pairs.append((m.group(1), m.group(2)))
    if not pairs:
        raise BeatError("pattern is empty. Write one line per drum, e.g.\nkick: x...x...x...x...\nsnare: ....x.......x...\nhat: x.x.x.x.x.x.x.x.")
    lanes: Dict[str, List[float]] = {}
    for raw, steps in pairs:
        name = lane_name(raw)
        parsed = parse_lane(name, steps)
        if name in lanes:  # the same drum on two lines: both sets of hits play
            old, size = lanes[name], max(len(lanes[name]), len(parsed))
            parsed = [max(a, b) for a, b in zip(old + [0.0] * (size - len(old)), parsed + [0.0] * (size - len(parsed)))]
        lanes[name] = parsed
    return lanes


def fit_lengths(lanes: Dict[str, List[float]], cells: Dict[str, List[Any]]) -> Tuple[Dict[str, List[float]], Dict[str, List[Any]], int, List[str]]:
    """Make every drum lane and every bass/melody line the same number of steps, and say what had to change.

    Lines whose length divides the pattern length simply repeat (a 4-step kick across 16). The pattern length is the one the
    most lines fit (preferring the one most lines have, then a multiple of 4, then the longer). A line that does not fit is padded
    with rests, or cut, and the returned notes say so: models are poor at counting characters, and a one-step slip should not
    cost them the whole beat."""
    sizes = [len(h) for h in lanes.values()] + [len(c) for c in cells.values()]
    if not sizes:
        return lanes, cells, 0, []
    counts = {n: sizes.count(n) for n in set(sizes)}
    target = max(counts, key=lambda t: (sum(1 for n in sizes if t % n == 0), counts[t], t % 4 == 0, t))
    notes: List[str] = []

    def fit(label: str, line: List[Any], rest: Any) -> List[Any]:
        n = len(line)
        if n == target:
            return line
        if target % n == 0:
            return line * (target // n)
        if n < target:
            notes.append(f"{label} had {n} steps, so {target - n} rest{'s' if target - n > 1 else ''} {'were' if target - n > 1 else 'was'} added at the end")
            return line + [rest] * (target - n)
        dropped = line[target:]
        lost = any(x not in (0.0, None, "-") for x in dropped)
        notes.append(f"{label} had {n} steps, so the last {n - target} {'(which held notes) ' if lost else ''}{'were' if n - target > 1 else 'was'} cut")
        return line[:target]

    return (
        {name: fit(name, hits, 0.0) for name, hits in lanes.items()},
        {kind: fit(kind, c, None) for kind, c in cells.items()},
        target,
        notes,
    )


def parse_pattern(pattern: Any) -> Tuple[Dict[str, List[float]], int]:
    """The drum lanes of a pattern, all fitted to the same length, and that length in steps."""
    lanes, _cells, steps, _notes = fit_lengths(parse_lanes(pattern), {})
    return lanes, steps


# --------------------------------------------------------------------------- #
# MIDI file
# --------------------------------------------------------------------------- #


def _vlq(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


Segment = Dict[str, Any]  # one stretch of music: lanes, steps, repeats, bpm, swing, tonal, sound


def one_segment(lanes: Dict[str, List[float]], steps: int, repeats: int, bpm: float, swing: float,
                tonal: Optional[Dict[str, List[Tuple[int, int, List[int]]]]] = None, melody_sound: str = "square") -> Segment:
    return {"lanes": lanes, "steps": steps, "repeats": repeats, "bpm": bpm, "swing": swing, "tonal": tonal or {}, "sound": melody_sound}


def build_midi_song(segments: List[Segment], title: str) -> bytes:
    """A format-0 standard MIDI file of the segments one after another: drums on channel 10, bass on channel 1 and melody on
    channel 2 when present, a tempo change wherever the tempo changes."""
    events: List[Tuple[int, int, bytes]] = []  # (tick, sort order, bytes); note-offs sort before note-ons at the same tick
    offset, programs = 0, {}
    for seg in segments:
        steps, repeats = seg["steps"], seg["repeats"]
        swing_ticks = int(round(MIDI_TICKS_PER_STEP * seg["swing"] / 100))
        events.append((offset, -2, b"\xff\x51\x03" + int(round(60_000_000 / seg["bpm"])).to_bytes(3, "big")))
        for name, hits in seg["lanes"].items():
            note = LANES[name][0]
            for step in range(steps * repeats):
                strength = hits[step % steps]
                if not strength:
                    continue
                tick = offset + step * MIDI_TICKS_PER_STEP + (swing_ticks if step % 2 else 0)
                velocity = max(1, min(127, int(round(strength * 127))))
                events.append((tick, 1, bytes([0x90 | MIDI_CHANNEL, note, velocity])))
                events.append((tick + MIDI_TICKS_PER_STEP // 2, 0, bytes([0x80 | MIDI_CHANNEL, note, 0])))
        for kind, notes in seg["tonal"].items():
            channel = TONAL_CHANNEL[kind]
            program = BASS_PROGRAM if kind == "bass" else MELODY_SOUNDS.get(seg["sound"], 80)
            if programs.get(channel) != program:
                programs[channel] = program
                events.append((offset, -1, bytes([0xC0 | channel, program])))
            for repeat in range(repeats):
                for start, length, midis in notes:
                    step = repeat * steps + start
                    tick = offset + step * MIDI_TICKS_PER_STEP + (swing_ticks if step % 2 else 0)
                    off = offset + (step + length) * MIDI_TICKS_PER_STEP - 10
                    for midi in midis:
                        events.append((tick, 1, bytes([0x90 | channel, midi, 96])))
                        events.append((max(tick + 1, off), 0, bytes([0x80 | channel, midi, 0])))
        offset += steps * repeats * MIDI_TICKS_PER_STEP
    events.sort(key=lambda e: (e[0], e[1]))
    body = bytearray()
    label = (title or "drum beat").encode("ascii", "replace")[:60]
    body += b"\x00\xff\x03" + _vlq(len(label)) + label
    body += b"\x00\xff\x58\x04\x04\x02\x18\x08"  # 4/4
    last = 0
    for tick, _order, data in events:
        body += _vlq(tick - last) + data
        last = tick
    body += _vlq(max(0, offset - last)) + b"\xff\x2f\x00"
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, MIDI_TICKS_PER_BEAT) + b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def build_midi(lanes: Dict[str, List[float]], steps: int, repeats: int, bpm: float, swing: float, title: str,
               tonal: Optional[Dict[str, List[Tuple[int, int, List[int]]]]] = None, melody_sound: str = "square") -> bytes:
    """A format-0 standard MIDI file of one pattern played `repeats` times."""
    return build_midi_song([one_segment(lanes, steps, repeats, bpm, swing, tonal, melody_sound)], title)


# --------------------------------------------------------------------------- #
# Synthesized drums (pure Python; each sound is built once per render)
# --------------------------------------------------------------------------- #


def _noise(rng: random.Random, n: int) -> List[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(n)]


def _highpass(x: List[float], k: float = 1.0) -> List[float]:
    return [x[i] - (k * x[i - 1] if i else 0.0) for i in range(len(x))]


def _sweep(sr: int, seconds: float, f_start: float, f_end: float, sweep_rate: float, decay: float) -> List[float]:
    out, phase = [], 0.0
    for i in range(int(sr * seconds)):
        t = i / sr
        phase += 2 * math.pi * (f_end + (f_start - f_end) * math.exp(-t * sweep_rate)) / sr
        out.append(math.sin(phase) * math.exp(-t * decay))
    return out


def _mix(*parts: List[float]) -> List[float]:
    n = max(len(p) for p in parts)
    return [sum(p[i] for p in parts if i < len(p)) for i in range(n)]


def synth(name: str, sr: int, rng: random.Random) -> List[float]:
    """One drum hit at full strength, as floats roughly in -1..1."""
    def env(n: int, decay: float) -> List[float]:
        return [math.exp(-(i / sr) * decay) for i in range(n)]

    if name == "kick":
        body = _sweep(sr, 0.45, 160, 48, 28, 8)
        click = [v * math.exp(-i / sr * 600) * 0.5 for i, v in enumerate(_noise(rng, int(sr * 0.01)))]
        return _mix(body, click)
    if name == "snare":
        n = int(sr * 0.28)
        tone = [math.sin(2 * math.pi * 185 * i / sr) * math.exp(-i / sr * 28) * 0.55 for i in range(int(sr * 0.2))]
        hiss = [a * b * 0.85 for a, b in zip(_highpass(_noise(rng, n), 0.55), env(n, 17))]
        return _mix(tone, hiss)
    if name == "rim":
        n = int(sr * 0.06)
        ping = [math.sin(2 * math.pi * 1700 * i / sr) * math.exp(-i / sr * 140) for i in range(n)]
        tick = [v * e * 0.5 for v, e in zip(_noise(rng, n), env(n, 200))]
        return _mix(ping, tick)
    if name == "clap":
        n = int(sr * 0.3)
        raw = _highpass(_noise(rng, n), 0.4)
        out = [0.0] * n
        for burst in (0.0, 0.011, 0.022):
            start = int(sr * burst)
            for i in range(start, min(n, start + int(sr * 0.015))):
                out[i] += raw[i] * math.exp(-(i - start) / sr * 150)
        tail = [raw[i] * math.exp(-i / sr * 22) * 0.6 for i in range(n)]
        return [a + (b if i > sr * 0.03 else 0) for i, (a, b) in enumerate(zip(out, tail))]
    if name == "hat":
        n = int(sr * 0.09)
        return [a * b * 0.9 for a, b in zip(_highpass(_noise(rng, n)), env(n, 75))]
    if name == "open_hat":
        n = int(sr * 0.55)
        return [a * b * 0.8 for a, b in zip(_highpass(_noise(rng, n)), env(n, 9))]
    if name in ("low_tom", "mid_tom", "high_tom"):
        base = {"low_tom": 95, "mid_tom": 135, "high_tom": 190}[name]
        return _sweep(sr, 0.45, base * 1.5, base, 18, 8)
    if name == "crash":
        n = int(sr * 1.6)
        return [a * b * 0.8 for a, b in zip(_highpass(_noise(rng, n)), env(n, 2.6))]
    if name == "ride":
        n = int(sr * 0.9)
        ring = [sum(math.sin(2 * math.pi * f * i / sr) for f in (2980, 4130, 5360, 6720)) * 0.18 * math.exp(-i / sr * 5) for i in range(n)]
        wash = [a * b * 0.3 for a, b in zip(_highpass(_noise(rng, n)), env(n, 7))]
        return _mix(ring, wash)
    if name == "cowbell":
        n = int(sr * 0.35)
        out = []
        for i in range(n):
            a = 1.0 if math.sin(2 * math.pi * 560 * i / sr) >= 0 else -1.0
            b = 1.0 if math.sin(2 * math.pi * 845 * i / sr) >= 0 else -1.0
            out.append((a + b) * 0.35 * math.exp(-i / sr * 13))
        return out
    raise BeatError(f"No sound for {name!r}.")


def _lowpass(x: List[float], alpha: float) -> List[float]:
    out, acc = [], 0.0
    for v in x:
        acc += alpha * (v - acc)
        out.append(acc)
    return out


def synth_note(sound: str, midi: int, seconds: float, sr: int, rng: random.Random) -> List[float]:
    """One pitched note held for `seconds`, then a short release. `sound` is bass, square, saw, sine, pluck or pad."""
    freq = 440.0 * 2 ** ((midi - 69) / 12)
    held = max(1, int(sr * seconds))
    total = held + int(sr * RELEASE_SECONDS)
    two_pi = 2 * math.pi
    if sound == "pluck":  # Karplus-Strong: a burst of noise circulating in a tiny, slightly damped delay line
        period = max(2, int(sr / freq))
        line = [rng.uniform(-1, 1) for _ in range(period)]
        raw = []
        for i in range(total):
            k = i % period
            nxt = line[(k + 1) % period]
            raw.append(line[k])
            line[k] = 0.4985 * (line[k] + nxt)
    elif sound == "bass":
        raw = [0.7 * math.sin(two_pi * freq * i / sr) + 0.25 * math.sin(two_pi * 2 * freq * i / sr) + 0.12 * math.sin(two_pi * 3 * freq * i / sr) for i in range(total)]
    elif sound == "sine":
        raw = [math.sin(two_pi * freq * i / sr) for i in range(total)]
    elif sound == "square":
        raw = _lowpass([1.0 if (freq * i / sr) % 1.0 < 0.5 else -1.0 for i in range(total)], 0.3)
    elif sound == "saw":
        raw = _lowpass([2.0 * ((freq * i / sr) % 1.0) - 1.0 for i in range(total)], 0.25)
    elif sound == "pad":
        raw = _lowpass([sum(2.0 * ((freq * d * i / sr) % 1.0) - 1.0 for d in (0.996, 1.0, 1.004)) / 3 for i in range(total)], 0.12)
    else:
        raise BeatError(f"No sound called {sound!r}. Use: {', '.join(MELODY_SOUNDS)}.")
    attack = max(1, int(sr * (0.08 if sound == "pad" else 0.006)))
    release = total - held
    out = []
    for i, v in enumerate(raw):
        if sound == "pluck":
            shape = 1.0
        else:
            shape = 0.65 + 0.35 * math.exp(-i / sr * 8)  # a small bite at the start that settles into the sustain
        gain = min(1.0, (i + 1) / attack) * shape
        if i >= held:
            gain *= max(0.0, 1.0 - (i - held) / release)
        out.append(v * gain)
    return out


def render_song(segments: List[Segment], sample_rate: int) -> bytes:
    """The segments one after another as 16-bit mono WAV bytes."""
    rng = random.Random(1)  # the same music always sounds the same
    drum_names = sorted({name for seg in segments for name in seg["lanes"]})
    sounds = {name: synth(name, sample_rate, rng) for name in drum_names}
    tail = max([len(x) for x in sounds.values()] + [int(sample_rate * RELEASE_SECONDS)])
    spans = [seg["steps"] * seg["repeats"] * 60.0 / seg["bpm"] / STEPS_PER_BEAT for seg in segments]
    total = int(sample_rate * sum(spans)) + tail
    buf = array("d", bytes(8 * total))  # zeros; a plain list of floats would take four times the memory on a long track
    cache: Dict[tuple, List[float]] = {}
    clock = 0.0
    for seg, span in zip(segments, spans):
        steps, repeats = seg["steps"], seg["repeats"]
        step_seconds = 60.0 / seg["bpm"] / STEPS_PER_BEAT
        swing_seconds = step_seconds * seg["swing"] / 100
        for name, hits in seg["lanes"].items():
            level, sound = LANES[name][1], sounds[name]
            for step in range(steps * repeats):
                strength = hits[step % steps]
                if not strength:
                    continue
                at = int(sample_rate * (clock + step * step_seconds + (swing_seconds if step % 2 else 0)))
                gain = level * strength
                for i in range(at, min(total, at + len(sound))):
                    buf[i] += sound[i - at] * gain
        for kind, notes in seg["tonal"].items():
            voice = "bass" if kind == "bass" else seg["sound"]
            level = TONAL_LEVEL[kind]
            for repeat in range(repeats):
                for start, length, midis in notes:
                    step = repeat * steps + start
                    at = int(sample_rate * (clock + step * step_seconds + (swing_seconds if step % 2 else 0)))
                    for midi in midis:
                        key = (voice, midi, round(length * step_seconds, 4))
                        if key not in cache:
                            cache[key] = synth_note(voice, midi, length * step_seconds, sample_rate, rng)
                        note_wave = cache[key]
                        for i in range(at, min(total, at + len(note_wave))):
                            buf[i] += note_wave[i - at] * level / math.sqrt(len(midis))
        clock += span
    peak = max((abs(v) for v in buf), default=0.0) or 1.0
    scale = 0.9 / peak if peak > 0.9 else 1.0
    pcm = array("h", (int(max(-1.0, min(1.0, v * scale)) * 32767) for v in buf))
    out = BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        if sys.byteorder == "big":
            pcm.byteswap()
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def render_wav(lanes: Dict[str, List[float]], steps: int, repeats: int, bpm: float, swing: float, sample_rate: int,
               tonal: Optional[Dict[str, List[Tuple[int, int, List[int]]]]] = None, melody_sound: str = "square") -> bytes:
    """One pattern played `repeats` times, as 16-bit mono WAV bytes."""
    return render_song([one_segment(lanes, steps, repeats, bpm, swing, tonal, melody_sound)], sample_rate)


# --------------------------------------------------------------------------- #
# Songs: named sections played in an order
# --------------------------------------------------------------------------- #

SECTION_SETTINGS = {"repeats", "bpm", "swing", "sound", "melody_sound"}
MAX_SECTION_REPEATS = 64


def section_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def parse_song(text: Any) -> Dict[str, Dict[str, Any]]:
    """The sections of a song text: `[name]` (also `# name` or `name:` alone) starts one; under it come drum lines, `bass:` and
    `melody:` lines, and the settings `repeats:`, `bpm:`, `swing:` and `sound:`. Text with no header at all is one section, "main"."""
    sections: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None

    def start(name: str) -> Dict[str, Any]:
        key = section_key(name) or "main"
        if key in sections:
            raise BeatError(f"The section {name!r} is defined twice. Give each section its own name.")
        sections[key] = {"name": name.strip() or "main", "drums": [], "bass": None, "melody": None, "settings": {}}
        return sections[key]

    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or set(line) <= set("-=_* "):
            continue
        header = re.match(r"^\[(.+?)\]\s*$", line) or re.match(r"^#{1,6}\s*(.+?)\s*:?\s*$", line) or re.match(r"^([A-Za-z][A-Za-z0-9 _-]*?)\s*:\s*$", line)
        if header:
            current = start(header.group(1))
            continue
        if current is None:
            current = start("main")
        m = re.match(r"^([A-Za-z_ -]+?)\s*[:=]\s*(.*)$", line)
        key = section_key(m.group(1)) if m else ""
        if m and key in SECTION_SETTINGS:
            current["settings"]["sound" if key == "melody_sound" else key] = m.group(2).strip()
        elif m and key in ("bass", "melody", "lead"):
            current["melody" if key == "lead" else key] = m.group(2)
        else:
            current["drums"].append(line)
    if not sections:
        raise BeatError("The song is empty. Write sections like  [intro]  then lines such as  kick: x... x... x... x...")
    return sections


def parse_order(text: Any, sections: Dict[str, Dict[str, Any]]) -> List[str]:
    """Section keys in playing order from "intro, build x2, drop". Blank = every section once, in the order written."""
    raw = str(text or "").strip()
    if is_unset(text) or not raw:
        return list(sections)
    if re.search(r"[,;>|\n]", raw):
        pieces = [p for p in re.split(r"\s*(?:,|;|->|=>|>|\||\n)\s*", raw) if p.strip()]
    else:  # no separators: names separated by spaces, with a trailing x3 or *3 belonging to the name before it
        pieces = []
        for token in raw.split():
            if re.fullmatch(r"[x*×]\d+", token, re.I) and pieces:
                pieces[-1] += " " + token
            else:
                pieces.append(token)
    order: List[str] = []
    for piece in pieces:
        m = re.match(r"^(.*?)\s*[x*×]\s*(\d+)$", piece.strip(), re.I)
        name, times = (m.group(1), int(m.group(2))) if m and section_key(m.group(1)) in sections else (piece, 1)
        key = section_key(name)
        if key not in sections:
            raise BeatError(f"The order names {name.strip()!r}, which is not a section. The sections are: {', '.join(v['name'] for v in sections.values())}.")
        order.extend([key] * max(1, min(times, 16)))
    return order


def section_segment(section: Dict[str, Any], default_bpm: float, default_swing: float, default_sound: str, notes: List[str]) -> Segment:
    """One section as a segment (its lines fitted to one length; what had to be fixed is added to `notes`)."""
    name = section["name"]
    settings = section["settings"]
    lanes = parse_lanes("\n".join(section["drums"])) if section["drums"] else {}
    cells = {kind: parse_tonal(kind, section[kind]) for kind in ("bass", "melody") if section[kind] and section[kind].strip()}
    if not lanes and not cells:
        raise BeatError(f"The section {name!r} has no drums, bass or melody.")
    lanes, cells, steps, fixes = fit_lengths(lanes, cells)
    notes.extend(f"[{name}] {f}" for f in fixes)
    sound = str(settings.get("sound") or default_sound).strip().lower()
    if sound not in MELODY_SOUNDS:
        raise BeatError(f"In [{name}], sound {sound!r} is not one of: {', '.join(MELODY_SOUNDS)}.")
    return one_segment(
        lanes, steps,
        int(_number(settings.get("repeats"), 1, 1, MAX_SECTION_REPEATS, f"[{name}] repeats")),
        _number(settings.get("bpm"), default_bpm, 40, 240, f"[{name}] bpm"),
        _number(settings.get("swing"), default_swing, 0, 60, f"[{name}] swing"),
        {kind: tonal_notes(c, steps, kind) for kind, c in cells.items()}, sound,
    )


def wav_to_mp3(wav: bytes) -> Optional[bytes]:
    """The audio as MP3 via ffmpeg, or None when ffmpeg is missing or fails (the caller keeps the WAV)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "-v", "error", "-f", "wav", "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1"],
                              input=wav, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 and done.stdout else None


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-") or "beat"


def upload_to_open_webui(base_url: str, api_key: str, data: bytes, filename: str, timeout: int) -> Tuple[Optional[str], Optional[str]]:
    """Store a file in Open WebUI's file storage. Returns (relative_url, None) or (None, error_text)."""
    if not api_key:
        return None, "OPEN_WEBUI_API_KEY is empty in the tool's valves"
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/v1/files/",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, data, mimetypes.guess_type(filename)[0] or "application/octet-stream")},
            params={"process": "false"},
            timeout=timeout,
        )
        resp.raise_for_status()
        file_id = resp.json().get("id")
        if not file_id:
            return None, f"upload response had no 'id' field: {str(resp.text)[:200]}"
        return f"/api/v1/files/{file_id}/content", None
    except requests.RequestException as e:
        response = getattr(e, "response", None)
        detail = f" (HTTP {response.status_code}: {str(response.text)[:200]})" if response is not None else ""
        return None, f"{e}{detail}"
    except ValueError as e:
        return None, f"unexpected response from Open WebUI: {e}"


def _number(value: Any, default: float, low: float, high: float, label: str) -> float:
    if is_unset(value):
        return default
    try:
        number = float(str(value).strip().rstrip("%"))
    except ValueError:
        raise BeatError(f"{label} must be a number, got {value!r}.") from None
    if not low <= number <= high:
        raise BeatError(f"{label} must be between {low:g} and {high:g}, got {number:g}.")
    return number


async def _emit(emitter: Optional[Callable], payload: Dict[str, Any]) -> None:
    if emitter:
        await emitter(payload)


class Tools:
    class Valves(BaseModel):
        BEAT_DIR: str = Field(default="/app/backend/data/drum_beats", description="Folder (inside the Open WebUI container) where each beat's audio, MIDI file and a small .json sidecar are kept.")
        MAX_SECONDS: int = Field(default=300, description="Longest beat accepted, in seconds of pattern (before the final cymbal tail). A 150-second track renders in about 5 seconds and needs about 140 MB while it does.")
        MAX_SECTIONS: int = Field(default=12, description="Most different sections one make_song call may define.")
        MAX_ORDER_ITEMS: int = Field(default=64, description="Most parts one make_song order may list (a section listed twice counts twice).")
        MAX_STEPS: int = Field(default=128, description="Longest single pattern accepted, in steps (16 steps = one 4/4 bar).")
        SAMPLE_RATE: int = Field(default=44100, description="Audio sample rate. Lower (e.g. 22050) renders faster and smaller but dulls the hats.")
        OUTPUT_FORMAT: str = Field(default="mp3", description="'mp3' (needs ffmpeg in the container; falls back to wav if it is missing) or 'wav'.")
        UPLOAD_TO_OPEN_WEBUI: bool = Field(default=True, description="Upload each beat to Open WebUI's file storage so the user can play it from the chat. Needs OPEN_WEBUI_API_KEY.")
        OPEN_WEBUI_BASE_URL: str = Field(default="http://localhost:8080", description="Address the tool uses to call Open WebUI's own API, from the backend's point of view.")
        OPEN_WEBUI_API_KEY: str = Field(default="", description="Personal API key from Open WebUI (Settings -> Account -> API Keys). Set it here in the valve, not in the code.")
        ATTACH_TO_CHAT: bool = Field(default=True, description="Also attach each uploaded file to the assistant's message, so it is listed in the chat's Files tab.")
        PUBLIC_BASE_URL: str = Field(default="https://open-webui.dimension-x.net", description="Address the user's browser uses for Open WebUI, to build links that also work in an email. Empty = only the in-chat relative link.")
        REQUEST_TIMEOUT_SECONDS: int = Field(default=90, description="Per-HTTP-request timeout.")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    def _beat_dir(self) -> Path:
        path = Path(self.valves.BEAT_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def make_beat(
        self, pattern: Optional[str] = None, bpm: Optional[str] = None, repeats: Optional[str] = None, swing: Optional[str] = None,
        title: Optional[str] = None, bass: Optional[str] = None, melody: Optional[str] = None, melody_sound: Optional[str] = None,
        __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> str:
        """
        Play drums, a bass line and a melody on a step sequencer and give the user a link to listen to it, plus a MIDI file for their music software.
        Everything is steps: each step is a sixteenth note, so 16 steps are one bar of 4/4. Give every line the same number of steps (16 per bar; a shorter one that divides it evenly repeats, and a line that is a step or two off is padded or trimmed for you).
        DRUMS (pattern): one line per drum, its name, a colon, then x = hit, X = accent, g = ghost note, . = rest. Write the steps in groups of four so they are easy to count. Drums: kick, snare, rim, clap, hat, open_hat, low_tom, mid_tom, high_tom, crash, ride, cowbell.
        BASS and MELODY: one token per step, separated by spaces (use | between bars if you like). A note name (C2, F#3, Bb4; C4 is middle C), a chord joined with + (C4+E4+G4), . = rest, - = keep holding the last note. Bass is usually octaves 1-3, melody 4-5.
        Example (one bar):
        pattern = "kick: x... x... x... x...\nsnare: .... x... .... x...\nhat: x.x. x.x. x.x. x.x."
        bass = "C2 . . . | C2 . . . | G1 . . . | A1 . . ."
        melody = "E4 - . G4 | - - A4 - | G4 . . . | E4 - - ."

        :param pattern: The drum lines described above, separated by new lines. Pass "" (or omit) for no drums if you give a bass or melody.
        :param bpm: Tempo in beats per minute, 40 to 240. Pass "" (or omit) for 100.
        :param repeats: How many times the pattern plays, default 2. A short sample is the point; the tool limits the total length.
        :param swing: 0 to 60. Delays every second step for a shuffled feel. Pass "" (or omit) for none.
        :param title: A short name for the beat. Pass "" (or omit) to leave it unnamed.
        :param bass: A bass line, one token per step as described above. Pass "" (or omit) for none.
        :param melody: A melody, one token per step as described above. Pass "" (or omit) for none.
        :param melody_sound: square (default, chiptune), saw, sine, pluck or pad. Pass "" (or omit) for square.
        """
        v = self.valves
        try:
            lanes: Dict[str, List[float]] = parse_lanes(pattern) if not is_unset(pattern) else {}
            cells = {kind: parse_tonal(kind, text) for kind, text in (("bass", bass), ("melody", melody)) if not is_unset(text)}
            if not lanes and not cells:
                raise BeatError("Give at least one of pattern (drums), bass or melody. Example drums:\nkick: x... x... x... x...\nsnare: .... x... .... x...")
            lanes, cells, steps, fixes = fit_lengths(lanes, cells)
            sound = "square" if is_unset(melody_sound) else str(melody_sound).strip().lower()
            if sound not in MELODY_SOUNDS:
                raise BeatError(f"melody_sound {melody_sound!r} is not one of: {', '.join(MELODY_SOUNDS)}.")
            tonal = {kind: tonal_notes(c, steps, kind) for kind, c in cells.items()}
            if steps > v.MAX_STEPS:
                raise BeatError(f"The pattern is {steps} steps; the limit is {v.MAX_STEPS}. Shorten it (16 steps is one bar).")
            tempo = _number(bpm, 100, 40, 240, "bpm")
            swung = _number(swing, 0, 0, 60, "swing")
            times = int(_number(repeats, 2, 1, 128, "repeats"))
            seconds = steps * times * 60.0 / tempo / STEPS_PER_BEAT
            if seconds > v.MAX_SECONDS:
                fit = max(1, int(v.MAX_SECONDS // (steps * 60.0 / tempo / STEPS_PER_BEAT)))
                raise BeatError(f"That would be {seconds:.0f} seconds; the limit is {v.MAX_SECONDS}. Use repeats={fit} or fewer, a faster bpm, or a shorter pattern.")
        except BeatError as e:
            return f"Could not make the beat: {e}"

        segment = one_segment(lanes, steps, times, tempo, swung, tonal, sound)
        meta = {"bpm": tempo, "swing": swung, "repeats": times, "steps": steps,
                "pattern": {n: "".join("X" if h >= 1 else "x" if h >= 0.8 else "g" if h else "." for h in hits) for n, hits in lanes.items()},
                **({"bass": str(bass).strip()} if "bass" in cells else {}), **({"melody": str(melody).strip(), "melody_sound": sound} if "melody" in cells else {})}
        played = f"{steps} steps x {times} at {tempo:g} BPM" + (f", swing {swung:g}" if swung else "") + f" ({seconds:.1f} s)"
        if fixes:  # the beat was made anyway; say what was fixed so the next one is written right
            played += ". Note: your lines were not all the same length, so I fixed them: " + "; ".join(fixes) + f". Next time give every line exactly {steps} steps (write drum steps in groups of four, like x... x... x... x...)"
        label = (title or "").strip() if not is_unset(title) else ""
        return await self._publish([segment], label, label or (next(iter(lanes)) if lanes else next(iter(cells))) + "-beat", meta, played, "beat", __event_emitter__)

    async def make_song(
        self, song: str, order: Optional[str] = None, bpm: Optional[str] = None, swing: Optional[str] = None, title: Optional[str] = None,
        melody_sound: Optional[str] = None, __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> str:
        """
        Build a whole track from named sections and play them in an order, as one audio file plus a MIDI file. Use it for an intro, a build-up, a drop and so on, instead of making each part separately.
        Each section starts with its name in [brackets], then lines in the same style as make_beat: drum lines (kick: x... x... x... x...), a bass: line, a melody: line (one token per step, 16 steps per bar), and optional settings lines:
        repeats: how many times the section's pattern plays each time it comes up (default 1), bpm: a tempo for this section only (for a half-time drop), swing:, sound: (square, saw, sine, pluck, pad).
        Example:
        song = "[intro]\nkick: x... .... x... ....\nhat: x.x. x.x. x.x. x.x.\nbass: C1 . . . | . . . . | C1 . . . | . . . .\nrepeats: 2\n\n[drop]\nkick: x... x... x... x...\nsnare: .... x... .... x...\nbass: C1 - - - | G1 - - - | Ab1 - - - | F1 - - -\nmelody: C3 . . . | Eb3 . . . | G3 . . . | C4 . . .\nsound: saw\nrepeats: 4"
        order = "intro, drop, drop"

        :param song: The sections as described above, each starting with [name]. A section may leave out drums, bass or melody. Text with no [name] at all is treated as one section.
        :param order: Section names in the order they play, separated by commas; add x2 to repeat one, e.g. "intro, build x2, drop". Pass "" (or omit) to play every section once in the order written.
        :param bpm: Tempo for the sections that do not set their own, 40 to 240. Pass "" (or omit) for 100.
        :param swing: 0 to 60 for sections that do not set their own. Pass "" (or omit) for none.
        :param title: A short name for the track. Pass "" (or omit) to leave it unnamed.
        :param melody_sound: square (default), saw, sine, pluck or pad, for sections that do not set their own. Pass "" (or omit) for square.
        """
        v = self.valves
        try:
            sections = parse_song(song)
            if len(sections) > v.MAX_SECTIONS:
                raise BeatError(f"The song has {len(sections)} sections; the limit is {v.MAX_SECTIONS}.")
            sequence = parse_order(order, sections)
            if len(sequence) > v.MAX_ORDER_ITEMS:
                raise BeatError(f"The order has {len(sequence)} parts; the limit is {v.MAX_ORDER_ITEMS}. Use a section's repeats: setting instead of listing it again and again.")
            tempo = _number(bpm, 100, 40, 240, "bpm")
            swung = _number(swing, 0, 0, 60, "swing")
            sound = "square" if is_unset(melody_sound) else str(melody_sound).strip().lower()
            if sound not in MELODY_SOUNDS:
                raise BeatError(f"melody_sound {melody_sound!r} is not one of: {', '.join(MELODY_SOUNDS)}.")
            fixes: List[str] = []
            built = {key: section_segment(sections[key], tempo, swung, sound, fixes) for key in dict.fromkeys(sequence)}
            for key, seg in built.items():
                if seg["steps"] > v.MAX_STEPS:
                    raise BeatError(f"The section {sections[key]['name']!r} is {seg['steps']} steps; the limit is {v.MAX_STEPS}. Split it into two sections.")
            segments = [dict(built[key]) for key in sequence]
            seconds = sum(seg["steps"] * seg["repeats"] * 60.0 / seg["bpm"] / STEPS_PER_BEAT for seg in segments)
            if seconds > v.MAX_SECONDS:
                raise BeatError(f"That song would be {seconds:.0f} seconds; the limit is {v.MAX_SECONDS}. Lower the repeats, or leave out some of the order.")
        except BeatError as e:
            return f"Could not make the song: {e}"

        names = [sections[key]["name"] for key in sequence]
        label = (title or "").strip() if not is_unset(title) else ""
        meta = {"song": str(song).strip(), "order": names, "bpm": tempo, "swing": swung}
        played = f"{len(segments)} parts ({', '.join(names)}), {seconds:.0f} s"
        if fixes:
            played += ". Note: some lines were not the same length as the rest of their section, so I fixed them: " + "; ".join(fixes) + ". Next time give every line in a section the same number of steps (16 per bar; write drum steps in groups of four)"
        return await self._publish(segments, label, label or "song", meta, played, "track", __event_emitter__)

    async def _publish(self, segments: List[Segment], label: str, name_hint: str, meta: Dict[str, Any], played: str, noun: str,
                       emitter: Optional[Callable[[dict], Any]]) -> str:
        """Render, save, upload and attach; the text the model gets back is the links (or why there are none)."""
        v = self.valves
        await _emit(emitter, {"type": "status", "data": {"description": "Programming the sequencer...", "done": False}})
        wav = await asyncio.to_thread(render_song, segments, max(8000, v.SAMPLE_RATE))
        midi = build_midi_song(segments, label)
        audio, ext = wav, "wav"
        if v.OUTPUT_FORMAT.strip().lower() == "mp3":
            mp3 = await asyncio.to_thread(wav_to_mp3, wav)
            if mp3:
                audio, ext = mp3, "mp3"

        stamp = datetime.now().astimezone()
        base = f"{stamp:%Y%m%d-%H%M%S}-{slugify(name_hint)}"
        saved = ""
        try:
            folder = self._beat_dir()
            (folder / f"{base}.{ext}").write_bytes(audio)
            (folder / f"{base}.mid").write_bytes(midi)
            (folder / f"{base}.json").write_text(json.dumps({"title": label, "created": stamp.isoformat(timespec="seconds"), **meta}, indent=1))
            saved = str(folder / f"{base}.{ext}")
        except OSError as e:
            saved = f"(not saved to disk: {e})"

        links, problem = {}, None
        if v.UPLOAD_TO_OPEN_WEBUI:
            for data, name in ((audio, f"{base}.{ext}"), (midi, f"{base}.mid")):
                url, err = await asyncio.to_thread(upload_to_open_webui, v.OPEN_WEBUI_BASE_URL, v.OPEN_WEBUI_API_KEY, data, name, v.REQUEST_TIMEOUT_SECONDS)
                if url:
                    links[name] = (url, len(data))
                else:
                    problem = err
        if links and v.ATTACH_TO_CHAT:
            await _emit(emitter, {"type": "files", "data": {"files": [
                {"type": "file", "id": u.split("/api/v1/files/")[1].split("/")[0], "url": u, "name": n, "size": size, "status": "uploaded"}
                for n, (u, size) in links.items()]}})
        await _emit(emitter, {"type": "status", "data": {"description": "Ready", "done": True}})

        made = f"Made the {noun}: {played}"
        if f"{base}.{ext}" in links:
            audio_url = links[f"{base}.{ext}"][0]
            reply = f"{made}. Put this link in your reply so the user can play it:\n[Play {label or noun}]({audio_url})"
            if f"{base}.mid" in links:
                reply += f"\n[MIDI file]({links[f'{base}.mid'][0]})"
            if v.PUBLIC_BASE_URL.strip():
                reply += f"\nFull link for an email (opens only when the user is logged in to Open WebUI): {v.PUBLIC_BASE_URL.rstrip('/')}{audio_url}"
            return reply + f"\nSaved on the server as: {saved}"
        why = problem or "uploading to Open WebUI is turned off"
        return f"{made}. Saved on the server as: {saved}. No playable link could be made ({why}); tell the user, and that the tool's OPEN_WEBUI_API_KEY valve needs checking."

    async def list_beats(self, limit: Optional[str] = None) -> str:
        """
        List the most recent beats made with make_beat, newest first, each with its title, tempo and pattern.

        :param limit: How many to list, default 5, at most 20. Pass "" (or omit) for the default.
        """
        try:
            count = int(_number(limit, 5, 1, 20, "limit"))
            sidecars = sorted(self._beat_dir().glob("*.json"), reverse=True)[:count]
        except (BeatError, OSError) as e:
            return f"Could not list beats: {e}"
        if not sidecars:
            return "No beats have been made yet."
        lines = []
        for path in sidecars:
            try:
                meta = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            shown = [f"  order: {', '.join(meta['order'])}"] if meta.get("order") else []
            shown += [f"  {n}: {p}" for n, p in meta.get("pattern", {}).items()]
            shown += [f"  bass: {meta['bass']}"] if meta.get("bass") else []
            shown += [f"  melody ({meta.get('melody_sound', 'square')}): {meta['melody']}"] if meta.get("melody") else []
            lines.append(f"{path.stem} - {meta.get('title') or 'untitled'} ({meta.get('bpm'):g} BPM, swing {meta.get('swing', 0):g}" + (f", repeats {meta['repeats']}" if meta.get("repeats") else "") + ")\n" + "\n".join(shown))
        return "\n\n".join(lines) or "No beats could be read."
