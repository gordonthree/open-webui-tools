"""
title: Drum Machine
author: Gordon
version: 0.1.0
description: A step-sequencer drum machine for agents. The model writes a pattern as lanes of steps (kick: x...x...), the tool
    plays it on synthesized drums and returns a short audio preview plus a standard MIDI file (drums on General MIDI channel 10)
    that opens in any music program. Pure Python: no GPU, no soundfont, no extra packages (MP3 conversion uses ffmpeg when present).
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


def parse_pattern(pattern: Any) -> Tuple[Dict[str, List[float]], int]:
    """The lanes (name -> strengths, each stretched to the same length) and that length in steps.

    Accepts lines like `kick: x...x...` (also `kick x...` or `kick=x...`), or a JSON object {"kick": "x...x..."}. Two lines for the
    same drum are merged. A lane that is shorter than the longest repeats to fill it when the lengths divide evenly."""
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
    length = max(len(v) for v in lanes.values())
    for name, hits in lanes.items():
        if len(hits) != length:
            if length % len(hits):
                lengths = ", ".join(f"{n} {len(h)}" for n, h in lanes.items())
                raise BeatError(f"The lanes are different lengths ({lengths}). Make them all the same length, or a length that divides the longest (a 4-step lane repeats across 16).")
            lanes[name] = hits * (length // len(hits))
    return lanes, length


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


def build_midi(lanes: Dict[str, List[float]], steps: int, repeats: int, bpm: float, swing: float, title: str) -> bytes:
    """A format-0 standard MIDI file: one track, drums on channel 10."""
    events: List[Tuple[int, int, bytes]] = []  # (tick, sort order, bytes); note-offs sort before note-ons at the same tick
    swing_ticks = int(round(MIDI_TICKS_PER_STEP * swing / 100))
    for name, hits in lanes.items():
        note = LANES[name][0]
        for step in range(steps * repeats):
            strength = hits[step % steps]
            if not strength:
                continue
            tick = step * MIDI_TICKS_PER_STEP + (swing_ticks if step % 2 else 0)
            velocity = max(1, min(127, int(round(strength * 127))))
            events.append((tick, 1, bytes([0x90 | MIDI_CHANNEL, note, velocity])))
            events.append((tick + MIDI_TICKS_PER_STEP // 2, 0, bytes([0x80 | MIDI_CHANNEL, note, 0])))
    events.sort(key=lambda e: (e[0], e[1]))
    body = bytearray()
    label = (title or "drum beat").encode("ascii", "replace")[:60]
    body += b"\x00\xff\x03" + _vlq(len(label)) + label
    body += b"\x00\xff\x51\x03" + int(round(60_000_000 / bpm)).to_bytes(3, "big")
    body += b"\x00\xff\x58\x04\x04\x02\x18\x08"  # 4/4
    last = 0
    for tick, _order, data in events:
        body += _vlq(tick - last) + data
        last = tick
    end = steps * repeats * MIDI_TICKS_PER_STEP
    body += _vlq(max(0, end - last)) + b"\xff\x2f\x00"
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, MIDI_TICKS_PER_BEAT) + b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


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


def render_wav(lanes: Dict[str, List[float]], steps: int, repeats: int, bpm: float, swing: float, sample_rate: int) -> bytes:
    """The pattern as 16-bit mono WAV bytes."""
    rng = random.Random(1)  # the same pattern always sounds the same
    step_seconds = 60.0 / bpm / STEPS_PER_BEAT
    sounds = {name: synth(name, sample_rate, rng) for name in lanes}
    tail = max(len(s) for s in sounds.values())
    total = int(sample_rate * step_seconds * steps * repeats) + tail
    buf = [0.0] * total
    swing_seconds = step_seconds * swing / 100
    for name, hits in lanes.items():
        level, sound = LANES[name][1], sounds[name]
        for step in range(steps * repeats):
            strength = hits[step % steps]
            if not strength:
                continue
            at = int(sample_rate * (step * step_seconds + (swing_seconds if step % 2 else 0)))
            gain = level * strength
            end = min(total, at + len(sound))
            for i in range(at, end):
                buf[i] += sound[i - at] * gain
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
        MAX_SECONDS: int = Field(default=30, description="Longest beat accepted, in seconds of pattern (before the final cymbal tail).")
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
        self, pattern: str, bpm: Optional[str] = None, repeats: Optional[str] = None, swing: Optional[str] = None,
        title: Optional[str] = None, __event_emitter__: Optional[Callable[[dict], Any]] = None,
    ) -> str:
        """
        Play a drum pattern on a step sequencer and give the user a link to listen to it, plus a MIDI file for their music software.
        Write one line per drum: its name, a colon, then the steps. Each step is a sixteenth note, so 16 steps are one bar of 4/4.
        x = hit, X = accent (loud), g = ghost note (soft), . = rest. Spaces and | are ignored, so  x... x... x... x...  is fine.
        Drums: kick, snare, rim, clap, hat, open_hat, low_tom, mid_tom, high_tom, crash, ride, cowbell. All lines must be the same
        length (a shorter one that divides it evenly, like 4 steps in a 16-step bar, repeats). Example:
        kick: x...x...x...x...
        snare: ....x.......x...
        hat: x.x.x.x.x.x.x.x.

        :param pattern: The drum lines described above, separated by new lines.
        :param bpm: Tempo in beats per minute, 40 to 240. Pass "" (or omit) for 100.
        :param repeats: How many times the pattern plays, default 2. A short sample is the point; the tool limits the total length.
        :param swing: 0 to 60. Delays every second step for a shuffled feel. Pass "" (or omit) for none.
        :param title: A short name for the beat. Pass "" (or omit) to leave it unnamed.
        """
        v = self.valves
        try:
            lanes, steps = parse_pattern(pattern)
            if steps > v.MAX_STEPS:
                raise BeatError(f"The pattern is {steps} steps; the limit is {v.MAX_STEPS}. Shorten it (16 steps is one bar).")
            tempo = _number(bpm, 100, 40, 240, "bpm")
            swung = _number(swing, 0, 0, 60, "swing")
            times = int(_number(repeats, 2, 1, 64, "repeats"))
            seconds = steps * times * 60.0 / tempo / STEPS_PER_BEAT
            if seconds > v.MAX_SECONDS:
                fit = max(1, int(v.MAX_SECONDS // (steps * 60.0 / tempo / STEPS_PER_BEAT)))
                raise BeatError(f"That would be {seconds:.0f} seconds; the limit is {v.MAX_SECONDS}. Use repeats={fit} or fewer, a faster bpm, or a shorter pattern.")
        except BeatError as e:
            return f"Could not make the beat: {e}"

        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Programming the drum machine...", "done": False}})
        label = (title or "").strip() if not is_unset(title) else ""
        wav = await asyncio.to_thread(render_wav, lanes, steps, times, tempo, swung, max(8000, v.SAMPLE_RATE))
        midi = build_midi(lanes, steps, times, tempo, swung, label)
        audio, ext = wav, "wav"
        if v.OUTPUT_FORMAT.strip().lower() == "mp3":
            mp3 = await asyncio.to_thread(wav_to_mp3, wav)
            if mp3:
                audio, ext = mp3, "mp3"

        stamp = datetime.now().astimezone()
        base = f"{stamp:%Y%m%d-%H%M%S}-{slugify(label or next(iter(lanes)) + '-beat')}"
        saved = ""
        try:
            folder = self._beat_dir()
            (folder / f"{base}.{ext}").write_bytes(audio)
            (folder / f"{base}.mid").write_bytes(midi)
            (folder / f"{base}.json").write_text(json.dumps({
                "title": label, "created": stamp.isoformat(timespec="seconds"), "bpm": tempo, "swing": swung, "repeats": times, "steps": steps,
                "pattern": {n: "".join("X" if h >= 1 else "x" if h >= 0.8 else "g" if h else "." for h in hits) for n, hits in lanes.items()},
            }, indent=1))
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
            await _emit(__event_emitter__, {"type": "files", "data": {"files": [
                {"type": "file", "id": u.split("/api/v1/files/")[1].split("/")[0], "url": u, "name": n, "size": size, "status": "uploaded"}
                for n, (u, size) in links.items()]}})
        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Beat ready", "done": True}})

        played = f"{steps} steps x {times} at {tempo:g} BPM" + (f", swing {swung:g}" if swung else "") + f" ({seconds:.1f} s)"
        if f"{base}.{ext}" in links:
            audio_url = links[f"{base}.{ext}"][0]
            reply = f"Made the beat: {played}. Put this link in your reply so the user can play it:\n[Play {label or 'beat'}]({audio_url})"
            if f"{base}.mid" in links:
                reply += f"\n[MIDI file]({links[f'{base}.mid'][0]})"
            if v.PUBLIC_BASE_URL.strip():
                reply += f"\nFull link for an email (opens only when the user is logged in to Open WebUI): {v.PUBLIC_BASE_URL.rstrip('/')}{audio_url}"
            return reply + f"\nSaved on the server as: {saved}"
        why = problem or "uploading to Open WebUI is turned off"
        return f"Made the beat: {played}, saved on the server as: {saved}. No playable link could be made ({why}); tell the user, and that the tool's OPEN_WEBUI_API_KEY valve needs checking."

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
            lanes = "\n".join(f"  {n}: {p}" for n, p in meta.get("pattern", {}).items())
            lines.append(f"{path.stem} - {meta.get('title') or 'untitled'} ({meta.get('bpm'):g} BPM, swing {meta.get('swing', 0):g}, repeats {meta.get('repeats')})\n{lanes}")
        return "\n\n".join(lines) or "No beats could be read."
