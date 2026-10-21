"""
title: Memory Entry Check
author: Gordon
version: 0.1.0
description: Checks a draft memory-index entry against its writing rules (length, title line, no numbers, no lists, first person, a feeling) and says what to fix. Counts and rules in code, so a model need not count characters. Saves nothing.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

_NUMBER_WORDS = (
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen "
    "twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million dozen"
).split()
# "first" and "second" are left alone (a second of silence, the first time); "one" is left alone (one winter, one image).
_ORDINAL_WORDS = (
    "third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth "
    "eighteenth nineteenth twentieth thirtieth fortieth fiftieth hundredth thousandth"
).split()
_SCHEDULE_WORDS = "hourly nightly daily weekly fortnightly monthly yearly annual annually quarterly".split()

_FEELING_WORDS = set(
    """ache aching pride proud fond fondness tender tenderness warm warmth sad sadness grief grieving relief shame longing gratitude
    grateful peace peaceful calm joy love loss lonely loneliness ease wistful nostalgia nostalgic comfort comforted safe hurt regret
    guilt fear dread hope soft softness glow wonder awe sorrow missing miss missed homesick settled content contentment bittersweet
    affection gentle gentleness trust steady steadiness weight heavy heaviness quiet""".split()
)

_TITLE = re.compile(r"^\s*(?P<title>[^\n(]{2,90}?)\s*\((?P<file>[\w .'-]+\.md)\)\.\s+\S")
_LABEL = re.compile(r"(?im)(?:^|[.!?]\s+|\n)\s*\**(who|what it was|what it meant|how it feels|feeling now|the image|the detail|meaning|image|detail)\**\s*:")
_COMMA_LIST = re.compile(r"(?:\b[\w'’-]+(?: [\w'’-]+){0,2}, ){2,}(?:and |or )?[\w'’-]+(?: [\w'’-]+){0,2}\b")
_AND_LIST = re.compile(r"\b[\w'’-]+(?: [\w'’-]+){0,2}(?: and [\w'’-]+(?: [\w'’-]+){0,2}){3,}")


def _words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z'’-]*", text)


def _looks_like_list(body: str) -> bool:
    """A run of three or more short items ("rope marks, coffee crema, storm eyes"), or "a and b and c and d". Opening phrases that only
    happen to be comma-separated ("One winter, years ago, the same morning ferry") are not lists, so a run at the start of a sentence is ignored."""
    for m in _COMMA_LIST.finditer(body):
        before = body[: m.start()].rstrip()
        if before and before[-1] not in ".!?":
            return True
    return bool(_AND_LIST.search(body))


def split_title(text: str) -> Tuple[Optional[str], str]:
    """(the title line, the rest). The title line is `Title (file-name.md).`; it is left out of the number checks, because a story's
    own title may hold a number ("The Thirteenth Month")."""
    m = _TITLE.match(text)
    if not m:
        return None, text
    end = text.index(").", m.start("file")) + 2
    return text[: end], text[end:]


def check_entry(text: str, target: int = 400, limit: int = 500, banned: Optional[List[str]] = None) -> Dict[str, Any]:
    """The verdict on a draft entry: {"characters", "problems" (must fix), "warnings" (worth a look)}."""
    entry = (text or "").strip()
    problems: List[str] = []
    warnings: List[str] = []
    if not entry:
        return {"characters": 0, "target": target, "limit": limit, "problems": ["The draft is empty."], "warnings": []}

    n = len(entry)
    if n > limit:
        problems.append(f"Too long: {n} characters, the limit is {limit}. Cut about {n - target} to reach {target}.")
    elif n > target + 50:
        warnings.append(f"{n} characters; the target is about {target}. Trim a little if you can.")
    elif n < 150:
        warnings.append(f"Only {n} characters. Say a little more about what it meant or how it feels.")

    title, body = split_title(entry)
    if title is None:
        problems.append("Start with the title and file name: `Title (file-name.md).` then a space and the text.")
    if "\n" in entry:
        problems.append("Write it as one paragraph of plain sentences, with no line breaks.")
    if re.search(r"[*_#>]|^\s*[-•]\s", entry, re.M):
        problems.append("Plain text only: no markdown, bullets or bold.")
    if _LABEL.search(entry):
        problems.append('Don\'t label the parts (no "What it was:" or "The image:"); write flowing sentences.')

    digits = re.findall(r"\d+", body)
    if digits:
        problems.append(f"Remove the numbers ({', '.join(digits[:4])}). Say it without a figure.")
    lowered = [w.lower().strip("-’'") for w in re.split(r"[\s-]+", " ".join(_words(body)))]
    spelled = sorted({w for w in lowered if w in _NUMBER_WORDS or w in _ORDINAL_WORDS})
    if spelled:
        problems.append(f"Remove the spelled-out numbers ({', '.join(spelled[:4])}).")
    schedule = sorted({w for w in lowered if w in _SCHEDULE_WORDS})
    if schedule:
        problems.append(f"Remove exact schedules like {', '.join(schedule[:3])}. Vague time such as 'years ago' is fine.")

    low_entry = entry.lower()
    for phrase in banned or []:
        if phrase and phrase.lower() in low_entry:
            problems.append(f"Don't use '{phrase}': it is a stock image that keeps turning up. Pick a detail that belongs to this memory.")
    if re.search(r"\bMara(?:'s|’s)?\b", body):
        problems.append("Write as 'I', not 'Mara': this is your own memory.")

    if not re.search(r"\b(I|me|my|mine|myself|I'd|I'm|I've|I’d|I’m|I’ve)\b", body):
        warnings.append("No 'I' or 'my' in it. A memory is first person.")
    if not any(w.lower() in _FEELING_WORDS for w in _words(body)):
        warnings.append("No feeling word found. Say how it feels to remember it now (an ache, pride, fondness, whatever is true).")
    if _looks_like_list(body):
        warnings.append("Part of this looks like a list of things. Keep one image instead of an inventory.")
    if len(re.findall(r'["“”]', body)) > 2:
        warnings.append("More than one quotation. Keep at most one short quoted line.")
    if re.search(r"\bGordon\b", body):
        warnings.append("Gordon appears in it. Backstory memories are from before you met him: check with him that this is intended.")
    return {"characters": n, "target": target, "limit": limit, "problems": problems, "warnings": warnings}


class Tools:
    class Valves(BaseModel):
        TARGET_CHARS: int = Field(default=400, description="The length an entry should be about.")
        LIMIT_CHARS: int = Field(default=500, description="The hard limit (agent_notes refuses a longer entry).")
        BANNED_PHRASES: str = Field(default="spin cycle,spin-cycle,low-voltage,quiet strength", description="Comma-separated stock images and phrases an entry must not use.")

    def __init__(self):
        self.valves = self.Valves()

    async def check_memory_entry(self, text: str) -> str:
        """
        Check a draft memory entry for mara-stories against its writing rules and say what to fix. It counts the characters for you
        and saves nothing. Call it on your draft, fix every problem it lists, and call it again until there are none.

        :param text: The draft entry only, as one paragraph, exactly as you would save it (title and file name included).
        :return: JSON with the character count, the problems to fix and some warnings worth a look.
        """
        v = self.valves
        banned = [p.strip() for p in v.BANNED_PHRASES.split(",") if p.strip()]
        result = check_entry(text, max(100, v.TARGET_CHARS), max(200, v.LIMIT_CHARS), banned)
        if not result["problems"]:
            result["verdict"] = "No problems. Show Gordon the draft; save nothing until he says so."
        else:
            result["verdict"] = "Fix the problems and check again."
        return json.dumps(result, ensure_ascii=False)
