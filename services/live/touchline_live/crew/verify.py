"""The Verifier: nothing goes to air unless the record supports it.

Three gates, all deterministic:
1. Claims: every typed claim is resolved through the match MCP server (verify_numbers).
2. Coverage: every number and every capitalised name in the text must be backed by a claim,
   the moment's facts, or the roster and club names. A number the record cannot account for
   fails the line.
3. Style: broadcast length, plain punctuation, no invented superlatives about history.
"""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import Claim

NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(%?)")
NAME = re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b")
BANNED = ["—", "–", "ever ", "all-time", "record-breaking", "history", "career-best", "best in the league"]
COMMON = {"The", "A", "An", "He", "His", "They", "Their", "That", "This", "What", "Now", "And", "But", "It", "Its",
          "In", "On", "At", "From", "With", "After", "Before", "Goal", "Save", "Chance", "Half", "Full", "Time", "Big",
          "One", "Two", "Three", "First", "Second", "Home", "Away", "Who", "How", "Why", "So", "Still", "Just", "Off",
          "Over", "Under", "Into", "Out", "Left", "Right", "Back", "Up", "Down", "No", "Yes", "If", "When", "Then",
          "Meanwhile", "Pressure", "Chaos", "Control", "Corner", "Penalty", "Header", "Keeper", "Shot", "Pass",
          "Ball", "Box", "Minute", "Minutes", "Score", "Lead", "Level", "Tempo", "Rhythm", "Look", "Watch", "Here",
          "There", "Brace", "Hat", "Trick", "Expected", "Goals", "Moments", "Story", "Recap", "Studio", "For", "Of",
          "To", "By", "As", "Is", "Was", "Are", "Were", "Be", "Not", "All", "Every", "Each", "Both", "Long", "Short"}


class MatchRecord:
    """A long-lived MCP client session to the match server."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = str(data_dir)
        self.stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self.calls = 0

    async def __aenter__(self):
        import sys
        params = StdioServerParameters(command=sys.executable, args=["-m", "touchline_live.mcp_server"],
                                       env={"TOUCHLINE_DATA": self.data_dir, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
                                       cwd=str(Path(__file__).resolve().parents[2]))
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    async def call(self, tool: str, **args):
        self.calls += 1
        res = await self.session.call_tool(tool, args)
        if res.structuredContent is not None:
            out = res.structuredContent
            return out.get("result", out) if isinstance(out, dict) else out
        text = "".join(getattr(c, "text", "") for c in res.content)
        return json.loads(text) if text else None


def _numbers_in(obj) -> set[str]:
    out = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= _numbers_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= _numbers_in(v)
    elif isinstance(obj, bool) or obj is None:
        pass
    elif isinstance(obj, (int, float)):
        out |= _forms(obj)
    elif isinstance(obj, str):
        for n, _ in NUM.findall(obj):
            out |= _forms(float(n))
    return out


def _forms(v: float) -> set[str]:
    """Every way a broadcaster might say this number: 0.21 xG may air as 0.21, 21 (%) or 0.2."""
    forms = {f"{v:g}", f"{round(v)}", f"{v:.1f}", f"{v:.2f}"}
    if 0 < abs(v) < 1:
        forms |= {f"{round(100 * v)}", f"{100 * v:.0f}"}
    return {f.rstrip("0").rstrip(".") if "." in f else f for f in forms}


def _norm(n: str) -> str:
    return n.rstrip("0").rstrip(".") if "." in n else n


async def verify_text(record: MatchRecord, match_id: str, match_s: float, text: str, claims: list[Claim],
                      facts: dict, roster: list[str], clubs: list[str], *, max_words: int = 40) -> dict:
    problems: list[str] = []
    # Gate 1: claims against the record.
    checked = []
    if claims:
        payload = [c.model_dump(exclude_none=True) for c in claims]
        checked = await record.call("verify_numbers", match_id=match_id, match_s=match_s, claims=payload)
        for c in checked or []:
            if not c.get("ok"):
                problems.append(f"claim failed: {json.dumps({k: c.get(k) for k in ('kind', 'stat', 'field', 'value', 'actual', 'why') if c.get(k) is not None})}")
    # Gate 2: coverage of numbers and names.
    backed = _numbers_in(facts) | _numbers_in([c.model_dump() for c in claims]) | _numbers_in([c.get("actual") for c in checked or []])
    for n, pct in NUM.findall(text):
        if _norm(n) not in backed and not (len(n) == 4 and n.startswith("20")):
            problems.append(f"number {n}{pct} is not in the facts or a verified claim")
    names_ok = set()
    for full in roster + clubs:
        names_ok |= set(full.split()) | {full}
    for mt in NAME.finditer(text):
        nm = mt.group(1)
        parts = nm.split()
        before = text[: mt.start()].rstrip()
        if len(parts) == 1 and (not before or before[-1] in ".!?:"):
            continue                     # a capital at the start of a sentence is grammar, not a name
        if all(p in names_ok or p in COMMON for p in parts):
            continue
        if any(p not in names_ok and p not in COMMON for p in parts):
            unknown = [p for p in parts if p not in names_ok and p not in COMMON]
            problems.append(f"name {' '.join(unknown)} is not a player or club in this match")
    # Gate 3: style.
    low = text.lower()
    for b in BANNED:
        hit = (b in text) if b in ("—", "–") else (b in low)
        if hit:
            problems.append(f"banned wording or punctuation: {b.strip()!r}")
    if len(text.split()) > max_words:
        problems.append(f"too long: {len(text.split())} words, limit {max_words}")
    return {"ok": not problems, "problems": problems, "claims_checked": len(checked or [])}
