"""Write the match you want.

A fan or a producer describes a match in plain words. The Match Director (gpt-5.4-mini) turns the
story into engine settings and checkable story beats. The Gate keeps every setting inside the
engine's ranges and refuses beats that cannot all hold together. The engine plays candidate matches
in a process pool; a candidate that breaks a beat for good (a clean sheet conceded, a goal cap passed,
the wrong side scoring first) stops at that goal and frees its budget for another seed. A deterministic
validator checks every beat against the event log, and the best match goes to the data dir with the
story, the settings, the beat report and the trace.

    python -m touchline_live.scenario write "an underdog parks the bus and nicks it late"
    python -m touchline_live.scenario direct "a wild end-to-end draw with a red card"
    python -m touchline_live.scenario replay TL-912345 --out C:/tmp/check
    python -m touchline_live.eval.scenario_bench --out reports/scenario_bench.json

The story can move dials only inside the engine's ranges and can only choose beats from a fixed
vocabulary, so nothing in it reaches the engine or the screen except through typed fields.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import itertools
import json
import os
import queue
import random
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from touchline_engine import export
from touchline_engine.sim import Match as EngineMatch
from touchline_engine.squads import FORMATIONS, ROLE_GROUP, Style, Team, make_fixture

from .match_io import FPS, minute as minute_label

ROOT = Path(__file__).resolve().parents[3]
STYLE_DIALS = ("press", "directness", "width", "tempo", "risk")
STYLE_RANGE = (0.15, 0.9)       # squads.make_team draws every style dial from this range
STRENGTH_RANGE = (0.3, 0.7)     # and every club's strength from this one
SEED_FLOOR, SEED_CEIL = 900_000, 999_999   # written matches live at TL-900000 and up
FULL_MATCH_S = 5700.0           # about 95 minutes of play: the unit of the search budget
MAX_BEATS = 6
STORY_MAX = 300

# The example stories the fan app offers (web/js/write.js shows the same four; a test keeps them equal).
EXAMPLES = (
    "An underdog parks the bus and nicks it late",
    "A wild end-to-end draw with a red card",
    "The home side fall behind and come back to win",
    "A tight, cagey game settled by a single goal",
)

Side = Literal["home", "away"]
BeatKind = Literal["winner", "total_goals", "late_goal", "red_card", "comeback", "first_goal", "clean_sheet", "shots_diff"]


# ----- the spec the Director returns ---------------------------------------------------------

class StyleDials(BaseModel):
    press: float | None = Field(None, description="0.15 sits in a deep block .. 0.9 presses high; null keeps the club's own")
    directness: float | None = Field(None, description="0.15 patient build up .. 0.9 long and direct; null keeps the club's own")
    width: float | None = Field(None, description="0.15 narrow .. 0.9 wide; null keeps the club's own")
    tempo: float | None = Field(None, description="0.15 slow circulation .. 0.9 fast; null keeps the club's own")
    risk: float | None = Field(None, description="0.15 safe in the final third .. 0.9 brave, more shots at both ends; null keeps the club's own")


class SideSettings(BaseModel):
    strength: float | None = Field(None, description="overall quality, 0.3 weak .. 0.7 strong; null keeps the club's own")
    style: StyleDials = Field(default_factory=StyleDials)
    formation: Literal["4-3-3", "4-2-3-1", "3-5-2"] | None = Field(None, description="null keeps the club's own")


class Beat(BaseModel):
    kind: BeatKind
    side: Literal["home", "away", "draw"] | None = Field(None, description="winner: home, away or draw; otherwise home, away or null for either")
    minute: int | None = Field(None, description="late_goal only: the goal comes at or after this minute, 1 to 89")
    at_least: int | None = Field(None, description="total_goals, red_card, shots_diff: the lower bound")
    at_most: int | None = Field(None, description="total_goals only: the upper bound")
    says: str = Field("", description="the words of the story this beat checks")


class ScenarioSpec(BaseModel):
    home: SideSettings = Field(default_factory=SideSettings)
    away: SideSettings = Field(default_factory=SideSettings)
    beats: list[Beat] = Field(default_factory=list)


DIRECTOR = f"""You are the Match Director for Touchline, a synthetic football match engine. A fan or a producer
describes the match they want in plain words. You turn the story into engine settings and checkable story beats.

The engine plays a full match between two fictional clubs, home and away, and the story only tilts the odds:
the engine stays random, so set the dials that make the story likely and list the beats that would prove it.
The engine gets only a few tries, so every beat must stay likely: when the weaker side has to win, keep the
strength gap small (0.1 to 0.15) and let the style dials tell the rest of the story.

Settings for each side (null keeps the club's own value):
- strength: overall quality, {STRENGTH_RANGE[0]} weak to {STRENGTH_RANGE[1]} strong. An underdog sits near 0.3, a favourite near 0.7.
  Home already has a small edge.
- style dials, each from {STYLE_RANGE[0]} to {STYLE_RANGE[1]}:
  press: low sits in a deep block (parking the bus), high presses all over the pitch.
  directness: low builds patiently, high goes long and direct (counter attacks).
  width: low narrow, high wide.
  tempo: low slow circulation, high fast.
  risk: low safe in the final third, high brave: more shots and more goals at both ends.
- formation: "4-3-3", "4-2-3-1" or "3-5-2" (five at the back without the ball), or null.
Built into the engine: a side trailing after the hour presses and goes direct; a side leading late sits deeper.
A typical engine match has about 3 to 4 goals, 13 to 17 shots a side and a red card in about one match in four.
Measured on this engine: mild dials (0.6 to 0.75) barely move the result. Both sides at tempo and risk 0.85 with
press and directness 0.75 gave about 7 goals a match; both sides at tempo 0.3 and risk 0.2 gave about 2.5. To
change how many goals a match has, push both sides' dials near the ends of their range.

Beats are what the finished match must show. Use only these kinds, and only for what the story asks:
- winner: side "home", "away" or "draw".
- total_goals: at_least and/or at_most goals in the match.
- late_goal: a goal at or after `minute` (1 to 89), by side "home" or "away", or null for either.
- red_card: at least `at_least` red cards (default 1), for side or null for either.
- comeback: side was behind at some point and won; side null for either.
- first_goal: side scored first.
- clean_sheet: side conceded no goal.
- shots_diff: side had at least `at_least` more shots than the other. Only when the story says one side dominates;
  an open, end-to-end game is a matter of dials and goals, never of a shots gap.
Put the words of the story each beat checks in `says`. If the story names no side, choose one and stay
consistent: an underdog is usually the away side. At most {MAX_BEATS} beats. Never name real clubs, players
or competitions: the engine generates its own clubs."""


# ----- the gate ------------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return float(min(max(v, lo), hi))


def _abstract_ok(b: Beat, seq: tuple[str, ...]) -> bool:
    """Can this beat hold for a match whose goals came in this order (shots, cards and timing left free)?"""
    h, a = seq.count("home"), seq.count("away")
    other = lambda s: "away" if s == "home" else "home"
    if b.kind == "winner":
        return {"home": h > a, "away": a > h, "draw": h == a}[b.side]
    if b.kind == "total_goals":
        return (b.at_least is None or h + a >= b.at_least) and (b.at_most is None or h + a <= b.at_most)
    if b.kind == "late_goal":
        return (h + a > 0) if b.side is None else seq.count(b.side) > 0
    if b.kind == "first_goal":
        return bool(seq) and seq[0] == b.side
    if b.kind == "clean_sheet":
        return seq.count(other(b.side)) == 0
    if b.kind == "comeback":
        return any(_comeback(seq, s) for s in ([b.side] if b.side else ["home", "away"]))
    return True                                         # red_card, shots_diff: free of the goal order


def _comeback(seq, side: str) -> bool:
    mine = theirs = 0
    behind = False
    for g in seq:
        mine, theirs = mine + (g == side), theirs + (g != side)
        behind = behind or mine < theirs
    return behind and mine > theirs


_SEQS = [s for n in range(0, 11) for s in itertools.product(("home", "away"), repeat=n)]


def satisfiable(beats: list[Beat]) -> bool:
    return any(all(_abstract_ok(b, s) for b in beats) for s in _SEQS)


def describe(b: Beat) -> str:
    """A beat in plain words, built only from its typed fields."""
    side = {"home": "home", "away": "away", "draw": "a draw", None: "either side"}[b.side]
    if b.kind == "winner":
        return "a draw" if b.side == "draw" else f"{side} win"
    if b.kind == "total_goals":
        if b.at_most == 0:
            return "no goals"
        g = lambda n: "1 goal" if n == 1 else f"{n} goals"
        if b.at_least is not None and b.at_most is not None:
            return g(b.at_least) if b.at_least == b.at_most else f"{b.at_least} to {g(b.at_most)}"
        return f"at least {g(b.at_least)}" if b.at_least is not None else f"at most {g(b.at_most)}"
    if b.kind == "late_goal":
        return f"a goal for {side} from minute {b.minute}" if b.side else f"a goal from minute {b.minute}"
    if b.kind == "red_card":
        n = b.at_least or 1
        return f"{'a red card' if n == 1 else f'{n} red cards'}{'' if b.side is None else f' for {side}'}"
    if b.kind == "comeback":
        return f"{_a(side)} comeback" if b.side else "a comeback"
    if b.kind == "first_goal":
        return f"{side} score first"
    if b.kind == "clean_sheet":
        return f"{_a(side)} clean sheet" if b.side else "a clean sheet"
    if b.side is None:
        return f"a shots gap of {b.at_least}"
    return f"{side} outshoot {_other(b.side)} by {b.at_least}"


def _a(word: str) -> str:
    return ("an " if word[0] in "aeiou" else "a ") + word


def headline(spec: ScenarioSpec) -> str:
    """The match's public label: built from the beats, never from the story's own words."""
    if not spec.beats:
        return "Written match"
    text = ", ".join(describe(b) for b in spec.beats[:4])
    return text[0].upper() + text[1:]


def gate(spec: ScenarioSpec) -> tuple[ScenarioSpec, list[dict]]:
    """Clamp settings into the engine's ranges and refuse beats that are malformed or cannot all hold.

    Returns the cleaned spec and the gate's notes: each says what it did (clamp, fix, refuse) and why.
    """
    notes: list[dict] = []
    out = spec.model_copy(deep=True)
    for side in ("home", "away"):
        s = getattr(out, side)
        if s.strength is not None:
            v = _clamp(s.strength, *STRENGTH_RANGE)
            if v != s.strength:
                notes.append({"action": "clamp", "what": f"{side} strength {s.strength:g}",
                              "why": f"the engine's clubs run from {STRENGTH_RANGE[0]} to {STRENGTH_RANGE[1]}; set to {v:g}"})
            s.strength = v
        for dial in STYLE_DIALS:
            raw = getattr(s.style, dial)
            if raw is None:
                continue
            v = _clamp(raw, *STYLE_RANGE)
            if v != raw:
                notes.append({"action": "clamp", "what": f"{side} {dial} {raw:g}",
                              "why": f"style dials run from {STYLE_RANGE[0]} to {STYLE_RANGE[1]}; set to {v:g}"})
            setattr(s.style, dial, v)

    kept: list[Beat] = []
    for b in out.beats:
        b = b.model_copy()
        why = None
        if b.kind == "winner":
            if b.side not in ("home", "away", "draw"):
                why = "a winner needs home, away or draw"
        elif b.side == "draw":
            b.side = None
        if b.kind in ("first_goal", "clean_sheet", "shots_diff") and b.side not in ("home", "away"):
            why = f"{b.kind.replace('_', ' ')} needs a side"
        if b.kind == "total_goals":
            if b.at_least is None and b.at_most is None:
                why = "total goals needs a lower or an upper bound"
            else:
                if b.at_least is not None:
                    b.at_least = int(_clamp(b.at_least, 0, 10))
                if b.at_most is not None:
                    b.at_most = int(_clamp(b.at_most, 0, 10))
                if b.at_least is not None and b.at_most is not None and b.at_least > b.at_most:
                    notes.append({"action": "fix", "what": f"total goals {b.at_least} to {b.at_most}", "why": "bounds swapped"})
                    b.at_least, b.at_most = b.at_most, b.at_least
        if b.kind == "late_goal":
            m = 75 if b.minute is None else int(_clamp(b.minute, 1, 89))
            if b.minute is not None and m != b.minute:
                notes.append({"action": "clamp", "what": f"late goal minute {b.minute}", "why": f"a match minute runs 1 to 89 here; set to {m}"})
            b.minute = m
        if b.kind == "red_card":
            b.at_least = int(_clamp(b.at_least or 1, 1, 3))
        if b.kind == "shots_diff":
            b.at_least = int(_clamp(b.at_least if b.at_least is not None else 5, 1, 25))
        if b.kind not in ("late_goal",):
            b.minute = None
        if why:
            notes.append({"action": "refuse", "what": describe_safe(b), "says": b.says, "why": why})
            continue
        twin = next((k for k in kept if _same(b, k)), None)
        if twin is not None and b.kind == "total_goals":
            twin.at_least = b.at_least if twin.at_least is None else twin.at_least
            twin.at_most = b.at_most if twin.at_most is None else twin.at_most
            notes.append({"action": "fix", "what": describe(b), "why": "merged into one goals beat"})
            continue
        if twin is not None:
            notes.append({"action": "refuse", "what": describe(b), "says": b.says, "why": "the story already has this beat"})
            continue
        if len(kept) >= MAX_BEATS:
            notes.append({"action": "refuse", "what": describe(b), "says": b.says, "why": f"at most {MAX_BEATS} beats"})
            continue
        kept.append(b)

    # Beats that cannot all hold: drop the latest beat whose removal makes the rest possible.
    while kept and not satisfiable(kept):
        drop = next((i for i in range(len(kept) - 1, -1, -1) if satisfiable(kept[:i] + kept[i + 1:])), len(kept) - 1)
        b = kept.pop(drop)
        notes.append({"action": "refuse", "what": describe(b), "says": b.says,
                      "why": "it cannot hold together with " + ", ".join(describe(k) for k in kept)})
    out.beats = kept
    return out, notes


def describe_safe(b: Beat) -> str:
    try:
        return describe(b)
    except KeyError:
        return b.kind.replace("_", " ")


def _same(a: Beat, b: Beat) -> bool:
    return a.kind == b.kind and (a.side == b.side or a.kind in ("winner", "total_goals", "first_goal"))


# ----- the rules Director: the fallback when the model is out of reach ------------------------

def rules_director(story: str) -> ScenarioSpec:
    """A keyword reading of the story. It keeps the service working with no model, and it is the baseline."""
    s = story.lower()
    has = lambda *ws: any(w in s for w in ws)
    named = ("home" if has("home side", "home team", "the hosts", "at home") else
             "away" if has("away side", "away team", "the visitors", "on the road") else None)
    under = "home" if has("home underdog", "underdogs at home", "home side are underdogs") else "away"
    fav = "away" if under == "home" else "home"
    spec = ScenarioSpec()
    side = lambda k: getattr(spec, k)
    beats: list[Beat] = []
    if has("underdog", "giant kill", "minnow", "against the odds", "upset"):
        side(under).strength, side(fav).strength = 0.32, 0.68
        if not has("draw", "lose", "beaten", "hammer"):
            beats.append(Beat(kind="winner", side=under, says="underdog"))
    if has("park the bus", "parks the bus", "parked the bus", "deep block", "low block", "defend deep", "sit deep"):
        who = under if has("underdog") else "away"
        st = side(who).style
        st.press, st.risk, st.tempo, st.directness = 0.15, 0.2, 0.3, 0.8
        side(who).formation = "3-5-2"
        other = "home" if who == "away" else "away"
        side(other).style.press, side(other).style.risk = 0.85, 0.8
        beats.append(Beat(kind="shots_diff", side=other, at_least=6, says="parks the bus"))
    if has("end-to-end", "end to end", "wild", "goal fest", "goalfest", "open game", "thriller", "chaotic", "chaos"):
        for k in ("home", "away"):
            st = side(k).style
            st.tempo, st.directness, st.risk, st.press = 0.85, 0.75, 0.85, 0.75
        beats.append(Beat(kind="total_goals", at_least=4, says="end to end"))
    if has("tight", "cagey", "low scoring", "low-scoring", "chess"):
        for k in ("home", "away"):
            side(k).style.tempo, side(k).style.risk = 0.3, 0.2
        beats.append(Beat(kind="total_goals", at_most=2, says="tight"))
    if has("goalless", "0-0", "nil-nil", "no goals"):
        beats.append(Beat(kind="total_goals", at_most=0, says="goalless"))
    if has("draw", "stalemate", "share the points", "all square"):
        beats.append(Beat(kind="winner", side="draw", says="draw"))
    elif has("home win", "home side win", "home side wins"):
        beats.append(Beat(kind="winner", side="home", says="home win"))
    elif has("away win", "away side win", "away side wins"):
        beats.append(Beat(kind="winner", side="away", says="away win"))
    elif named and has("win", "wins", "won", "victory") and not any(b.kind == "winner" for b in beats):
        beats.append(Beat(kind="winner", side=named, says="win"))
    if has("comeback", "come back", "comes back", "fight back", "fights back", "from behind"):
        who = under if has("underdog") else named
        beats.append(Beat(kind="comeback", side=who, says="comeback"))
    m = re.search(r"\b(?:after|in) (?:the )?(\d{1,2})(?:th|st|nd|rd)? ?(?:minute|min|')", s)
    if has("late", "last minute", "stoppage", "injury time", "dying minutes") or m:
        minute = int(m.group(1)) if m else (88 if has("last minute", "stoppage", "injury time", "dying") else 80)
        who = under if has("underdog", "nick") else None
        beats.append(Beat(kind="late_goal", side=who, minute=minute, says="late"))
    if has("red card", "sent off", "sending off", "ten men", "10 men"):
        beats.append(Beat(kind="red_card", at_least=1, says="red card"))
    if has("clean sheet", "shut out", "shutout", "keep it tight at the back"):
        who = under if has("underdog", "park") else "home"
        beats.append(Beat(kind="clean_sheet", side=who, says="clean sheet"))
    if has("dominate", "dominates", "battered", "laid siege", "outshot", "one-way traffic", "one way traffic"):
        if not any(b.kind == "shots_diff" for b in beats):
            beats.append(Beat(kind="shots_diff", side=fav, at_least=8, says="dominate"))
    if has("score first", "scores first", "opens the scoring", "take the lead", "takes the lead"):
        who = fav if has("comeback", "come back", "fight back") else under if has("underdog") else "home"
        beats.append(Beat(kind="first_goal", side=who, says="score first"))
    spec.beats = beats
    return spec


# ----- the model Director ---------------------------------------------------------------------

def _gate_report(notes: list[dict]) -> str:
    return "\n".join(f"- {n['action']} {n['what']}: {n['why']}" for n in notes if n["action"] == "refuse")


async def direct(story: str, *, use_llm: bool = True, timeout: float = 40.0) -> tuple[ScenarioSpec, list[dict]]:
    """Story in, gated spec out, with the trace of who did what.

    The Director proposes; the Gate clamps and refuses. When the Gate refuses a beat that the
    story asked for, the Director gets one repair round with the Gate's reasons. With no model in
    reach, the rules Director reads the story instead and the trace says so.
    """
    trace: list[dict] = []
    spec = None
    if use_llm:
        t0 = time.perf_counter()
        chat = None
        try:
            from agent_framework import Agent

            from .crew import llm
            # A fresh client, never the cached one: each job runs its own event loop (asyncio.run in a worker
            # thread), and a client whose connections belong to a closed loop fails with "Event loop is closed".
            # It is closed in this loop when the Director is done.
            chat = llm.client.__wrapped__(llm.FAST)
            agent = Agent(client=chat, name="director", instructions=DIRECTOR)
            opts = {"response_format": ScenarioSpec, "reasoning": {"effort": "none"}}
            r = await asyncio.wait_for(agent.run(story, options=opts), timeout)
            spec = r.value
            trace.append({"role": "director", "model": llm.FAST, "ms": round((time.perf_counter() - t0) * 1000),
                          "beats": len(spec.beats)})
            clean, notes = gate(spec)
            trace.append({"role": "gate", "notes": notes, "beats": len(clean.beats)})
            if not any(n["action"] == "refuse" for n in notes):
                return clean, trace
            t1 = time.perf_counter()
            ask = (f"Story: {story}\n\nYour spec: {spec.model_dump_json()}\n\nThe gate refused these beats:\n"
                   f"{_gate_report(notes)}\n\nReturn a corrected spec whose beats can all hold together and still tell the story. "
                   "Fix a refused beat only when the story asks for exactly that beat; otherwise drop it. Never add a beat "
                   "the story does not ask for.")
            try:
                r2 = await asyncio.wait_for(agent.run(ask, options=opts), timeout)
                trace.append({"role": "director", "model": llm.FAST, "ms": round((time.perf_counter() - t1) * 1000),
                              "repair": True, "beats": len(r2.value.beats)})
                spec = r2.value
            except Exception as exc:                  # the repair failed: the gated first spec stands
                trace.append({"role": "director", "repair": True, "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                              "ms": round((time.perf_counter() - t1) * 1000)})
                return clean, trace
        except Exception as exc:                      # no login, no network, a malformed reply
            trace.append({"role": "director", "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                          "ms": round((time.perf_counter() - t0) * 1000)})
            spec = None
        finally:
            if chat is not None:
                with contextlib.suppress(Exception):
                    await chat.client.close()
    if spec is None:
        spec = rules_director(story)
        trace.append({"role": "director", "model": "rules", "fallback": use_llm, "beats": len(spec.beats)})
    clean, notes = gate(spec)
    trace.append({"role": "gate", "notes": notes, "beats": len(clean.beats)})
    return clean, trace


# ----- the clubs a spec asks for ------------------------------------------------------------------

def _retune(team: Team, s: SideSettings) -> Team:
    """The fixture's club with the spec's settings. Players move with the club's strength the way
    squads._player builds them: skills 0.25 and pace 0.3 per unit of strength, held inside the range
    squads._player can produce (skills 0 to 1.1, pace 6.8 to 9.9 m/s)."""
    players = list(team.players)
    strength = team.strength
    if s.strength is not None and s.strength != team.strength:
        d = s.strength - team.strength
        skills = ("passing", "finishing", "dribbling", "tackling", "stamina", "heading")
        players = [dataclasses.replace(p, pace=float(min(max(p.pace + 0.3 * d, 6.8), 9.9)),
                                       **{k: float(min(max(getattr(p, k) + 0.25 * d, 0.0), 1.1)) for k in skills})
                   for p in players]
        strength = s.strength
    formation = team.formation
    if s.formation and s.formation != team.formation:
        formation = s.formation
        players = _reshape(players[:11], formation) + players[11:]
    style = Style(**{k: (getattr(s.style, k) if getattr(s.style, k) is not None else getattr(team.style, k)) for k in STYLE_DIALS})
    return dataclasses.replace(team, formation=formation, style=style, strength=strength, players=players)


def _reshape(starters: list, formation: str) -> list:
    """Starters in the new formation's slot order: same role first, then the same line, then anyone."""
    pool = list(starters)
    out = []
    for role, _, _ in FORMATIONS[formation]:
        pick = (next((p for p in pool if p.role == role), None)
                or next((p for p in pool if ROLE_GROUP[p.role] == ROLE_GROUP[role]), None)
                or next(p for p in pool if (p.role == "GK") == (role == "GK")))
        pool.remove(pick)
        out.append(dataclasses.replace(pick, role=role))
    return out


def teams_for(seed: int, spec: ScenarioSpec) -> tuple[Team, Team]:
    home, away = make_fixture(seed)
    return _retune(home, spec.home), _retune(away, spec.away)


# ----- the validator ------------------------------------------------------------------------------

def match_minute(period: int, t: float) -> float:
    """Minutes on the match clock; first-half stoppage counts as minute 45."""
    return min(t / 60.0, 45.0) if period == 1 else 45.0 + t / 60.0


def _goals(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "goal"]


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


def check_beat(b: Beat, events: list[dict]) -> dict:
    """One beat against the event log: ok, a plain detail, partial credit for ranking, and the event ids."""
    goals = _goals(events)
    h = sum(g["team"] == "home" for g in goals)
    a = sum(g["team"] == "away" for g in goals)
    score = f"{h}-{a}"
    res = {"kind": b.kind, "beat": describe(b), "says": b.says}
    if b.kind == "winner":
        actual = "home" if h > a else "away" if a > h else "draw"
        ok = actual == b.side
        partial = 1.0 if ok else 0.5 if "draw" in (actual, b.side) else 0.0
        word = {"home": "home won", "away": "away won", "draw": "drawn"}[actual]
        return {**res, "ok": ok, "partial": partial, "detail": f"{word} {score}", "sources": [g["id"] for g in goals]}
    if b.kind == "total_goals":
        n = h + a
        lo, hi = b.at_least if b.at_least is not None else 0, b.at_most if b.at_most is not None else 99
        ok = lo <= n <= hi
        gap = 0 if ok else (lo - n if n < lo else n - hi)
        return {**res, "ok": ok, "partial": max(0.0, 1 - gap / 3), "detail": f"{n} goal{'' if n == 1 else 's'} ({score})", "sources": [g["id"] for g in goals]}
    if b.kind == "late_goal":
        mine = [g for g in goals if b.side is None or g["team"] == b.side]
        late = [g for g in mine if match_minute(g["period"], g["t"]) >= b.minute]
        if late:
            g = late[-1]
            return {**res, "ok": True, "partial": 1.0, "detail": f"{g['team']} scored at {minute_label(g['period'], g['t'])}",
                    "sources": [g["id"] for g in late]}
        if mine:
            g = mine[-1]
            m = match_minute(g["period"], g["t"])
            return {**res, "ok": False, "partial": 0.5 * m / b.minute,
                    "detail": f"last goal for {b.side or 'either side'} at {minute_label(g['period'], g['t'])}", "sources": [g["id"]]}
        return {**res, "ok": False, "partial": 0.0, "detail": f"no goal for {b.side or 'either side'}", "sources": []}
    if b.kind == "red_card":
        reds = [e for e in events if e["type"] == "card" and e.get("outcome") == "red" and (b.side is None or e["team"] == b.side)]
        need = b.at_least or 1
        ok = len(reds) >= need
        detail = (", ".join(f"{e['team']} red at {minute_label(e['period'], e['t'])}" for e in reds) or "no red card")
        return {**res, "ok": ok, "partial": min(1.0, len(reds) / need), "detail": detail, "sources": [e["id"] for e in reds]}
    if b.kind == "comeback":
        best = 0.0
        for side in ([b.side] if b.side else ["home", "away"]):
            mine = theirs = 0
            behind_at = None
            for g in goals:
                mine, theirs = mine + (g["team"] == side), theirs + (g["team"] != side)
                if mine < theirs and behind_at is None:
                    behind_at = g
            if behind_at is not None and mine > theirs:
                return {**res, "ok": True, "partial": 1.0,
                        "detail": f"{side} behind at {minute_label(behind_at['period'], behind_at['t'])}, won {score}",
                        "sources": [g["id"] for g in goals]}
            if behind_at is not None:
                best = max(best, 0.5 if mine == theirs else 0.25)
            elif mine > theirs:
                best = max(best, 0.5)
        return {**res, "ok": False, "partial": best, "detail": f"no comeback, {score}", "sources": [g["id"] for g in goals]}
    if b.kind == "first_goal":
        if not goals:
            return {**res, "ok": False, "partial": 0.0, "detail": "no goals", "sources": []}
        g = goals[0]
        return {**res, "ok": g["team"] == b.side, "partial": float(g["team"] == b.side),
                "detail": f"{g['team']} scored first at {minute_label(g['period'], g['t'])}", "sources": [g["id"]]}
    if b.kind == "clean_sheet":
        against = [g for g in goals if g["team"] == _other(b.side)]
        return {**res, "ok": not against, "partial": max(0.0, 1 - len(against) / 3),
                "detail": f"{b.side} conceded {len(against)}", "sources": [g["id"] for g in against]}
    if b.kind == "shots_diff":
        sh = {s: sum(1 for e in events if e["type"] == "shot" and e["team"] == s) for s in ("home", "away")}
        diff = sh[b.side] - sh[_other(b.side)]
        need = b.at_least or 1
        return {**res, "ok": diff >= need, "partial": float(min(max(diff / need, 0.0), 1.0)),
                "detail": f"shots {sh['home']}-{sh['away']}", "sources": []}
    raise ValueError(b.kind)


def validate(events: list[dict], beats: list[Beat]) -> dict:
    """Every beat checked against the event log. Ranking key: beats met, then partial credit."""
    rows = [check_beat(b, events) for b in beats]
    met = sum(r["ok"] for r in rows)
    return {"met": met, "total": len(rows), "all": met == len(rows),
            "partial": round(sum(r["partial"] for r in rows), 3), "beats": rows}


def broken(b: Beat, goals: list[dict]) -> bool:
    """True once the goals so far rule the beat out whatever happens next."""
    if b.kind == "total_goals" and b.at_most is not None:
        return len(goals) > b.at_most
    if b.kind == "clean_sheet":
        return any(g["team"] == _other(b.side) for g in goals)
    if b.kind == "first_goal":
        return bool(goals) and goals[0]["team"] != b.side
    return False


# ----- candidates ------------------------------------------------------------------------------

class Pruned(Exception):
    pass


class _Watched(EngineMatch):
    """The engine's Match, watched from outside: a goal that breaks a beat for good ends the candidate.
    The engine's random stream is untouched, so an unbroken candidate plays exactly as Match.play would."""

    def __init__(self, seed, home, away, beats, slot=None):
        super().__init__(seed, home, away)
        self._beats = list(beats)
        self._slot = slot

    def emit(self, type_, *args, **kw):
        ev = super().emit(type_, *args, **kw)
        if self._slot is not None and _PROGRESS is not None and (len(self.events) % 200 == 0 or type_ == "goal"):
            _PROGRESS[self._slot] = match_minute(self.period, self.clock())
        if type_ == "goal" and self._beats:
            goals = _goals(self.events)
            if any(broken(b, goals) for b in self._beats):
                raise Pruned(ev["id"])
        return ev


_PROGRESS = None    # a shared array of match minutes, one cell per candidate, set in each worker


def _init_worker(progress):
    global _PROGRESS
    _PROGRESS = progress


def play_candidate(seed: int, spec_json: str, prune: bool, stage: str | None, slot: int | None = None,
                   period_s: float | None = None) -> dict:
    """One candidate in a worker process: play, validate, and stage the files when it finished."""
    spec = ScenarioSpec.model_validate_json(spec_json)
    home, away = teams_for(seed, spec)
    m = _Watched(seed, home, away, spec.beats if prune else [], slot)
    t0 = time.perf_counter()
    complete, pruned_at = True, None
    try:
        if period_s:
            m.play_period(1, period_s, 0)
            m.play_period(2, period_s, 1)
        else:
            m.play()
    except Pruned:
        complete = False
        g = _goals(m.events)[-1]
        pruned_at = minute_label(g["period"], g["t"])
    rep = validate(m.events, spec.beats)
    out = {"seed": seed, "match_id": export.match_id(seed), "complete": complete, "pruned_at": pruned_at,
           "score": list(m.score), "played_s": round(m.gf / FPS, 1), "wall_s": round(time.perf_counter() - t0, 1),
           **rep}
    if complete and stage:
        t1 = time.perf_counter()
        export.write(m, Path(stage) / export.match_id(seed))
        out["write_s"] = round(time.perf_counter() - t1, 1)
    if _PROGRESS is not None and slot is not None:
        _PROGRESS[slot] = -1.0
    return out


def pool_size(n: int) -> int:
    """Workers for the search: TOUCHLINE_SCENARIO_WORKERS, else 1 in a container, else up to 4 on a laptop."""
    env = os.environ.get("TOUCHLINE_SCENARIO_WORKERS")
    if env:
        return max(1, int(env))
    if os.environ.get("CONTAINER_APP_NAME") or os.environ.get("KUBERNETES_SERVICE_HOST"):
        return 1
    return max(1, min(n, 4, (os.cpu_count() or 2) - 1))


def _rank(c: dict) -> tuple:
    return (c["complete"], c["met"], c["partial"], -c["seed"])


def search(spec: ScenarioSpec, *, stage: Path, seeds: int = 3, workers: int | None = None, prune: bool = True,
           base_seed: int, period_s: float | None = None, progress: Callable[[dict], None] | None = None,
           wait_s: float = 900.0) -> dict:
    """Play candidates until every beat holds or the budget is spent.

    The budget is `seeds` full matches of play. Without pruning that is `seeds` candidates. With
    pruning a broken candidate costs only the minutes it played, so the same budget tries more seeds.
    The search always ends with at least one finished match to offer.
    """
    import multiprocessing as mp

    w = workers or pool_size(seeds)
    cap = seeds * 4 if prune else seeds
    full = (2 * period_s) if period_s else FULL_MATCH_S
    spec_json = spec.model_dump_json()
    ctx = mp.get_context("spawn")
    minutes = ctx.Array("d", [-1.0] * (cap + 1), lock=False)
    results: list[dict] = []
    inflight: dict[int, int] = {}            # slot -> seed
    q: queue.SimpleQueue = queue.SimpleQueue()
    spent, slot_next, seed_next = 0.0, 0, base_seed
    t0 = time.perf_counter()
    stage.mkdir(parents=True, exist_ok=True)

    def snapshot(state: str) -> dict:
        best = max(results, key=_rank) if results else None
        return {"state": state, "workers": w, "budget": seeds, "spent": round(spent, 2), "prune": prune,
                "candidates": [{k: c.get(k) for k in ("seed", "complete", "pruned_at", "met", "total", "score", "played_s", "wall_s", "error")}
                               for c in results],
                "playing": [{"seed": s, "minute": round(minutes[i], 1) if minutes[i] >= 0 else None} for i, s in inflight.items()],
                "best": None if best is None else {k: best[k] for k in ("seed", "met", "total", "complete")},
                "elapsed_s": round(time.perf_counter() - t0, 1)}

    with ctx.Pool(w, initializer=_init_worker, initargs=(minutes,)) as pool:
        def launch(prune_this: bool):
            nonlocal slot_next, seed_next
            slot, seed = slot_next, seed_next
            slot_next, seed_next = slot_next + 1, seed_next + 1
            inflight[slot] = seed
            pool.apply_async(play_candidate, (seed, spec_json, prune_this, str(stage), slot, period_s),
                             callback=lambda r, s=slot: q.put((s, r)),
                             error_callback=lambda e, s=slot, sd=seed: q.put((s, {"seed": sd, "error": f"{type(e).__name__}: {e}"})))

        done = False
        while not done:
            while (len(inflight) < w and spent + len(inflight) < seeds - 1e-9
                   and len(results) + len(inflight) < cap):
                launch(prune)
            if not inflight:
                if any(c.get("complete") for c in results) or slot_next > cap:
                    break
                launch(False)                        # never end empty handed: one candidate plays to the end
                continue
            if progress:
                progress(snapshot("playing"))
            try:
                slot, r = q.get(timeout=2.0)
            except queue.Empty:
                if time.perf_counter() - t0 > wait_s:
                    raise TimeoutError("the candidate search ran out of time")
                continue
            inflight.pop(slot, None)
            if "error" in r:
                r = {**r, "complete": False, "met": 0, "total": len(spec.beats), "partial": 0.0, "played_s": 0.0}
            results.append(r)
            spent += min(1.0, r.get("played_s", 0.0) / full) if r.get("played_s") else 1.0
            done = bool(r.get("complete") and r.get("all"))
        pool.terminate()

    finished = [c for c in results if c.get("complete")]
    if not finished:
        raise RuntimeError("no candidate finished: " + "; ".join(c.get("error", "") for c in results))
    best = max(finished, key=_rank)
    snap = snapshot("chosen")
    if progress:
        progress(snap)
    return {"best": best, "candidates": snap["candidates"], "workers": w, "prune": prune, "budget": seeds,
            "spent": round(spent, 2), "wall_s": round(time.perf_counter() - t0, 1),
            "played_s": round(sum(c.get("played_s", 0.0) for c in results), 1)}


# ----- the whole job ------------------------------------------------------------------------------

def free_base_seed(data_dir: Path, span: int, rng: random.Random | None = None) -> int:
    rng = rng or random.Random()
    for _ in range(200):
        base = rng.randrange(SEED_FLOOR, SEED_CEIL - span)
        if not any((data_dir / export.match_id(s)).exists() for s in range(base, base + span + 1)):
            return base
    raise RuntimeError("no free seed range for a written match")


def publish(result: dict, *, story: str, spec: ScenarioSpec, trace: list[dict], stage: Path, data_dir: Path) -> str:
    """Move the best candidate into the data dir and add the story, the beats and the trace to its meta."""
    best = result["best"]
    mid = best["match_id"]
    src, dst = stage / mid, data_dir / mid
    meta_p = src / "meta.json"                 # the note goes in before the move, so a reader never sees half a match
    md = json.loads(meta_p.read_text(encoding="utf-8"))
    md["scenario"] = {
        "story": story, "headline": headline(spec), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spec": spec.model_dump(), "trace": trace,
        "report": {k: best[k] for k in ("met", "total", "all", "partial", "beats")},
        "search": {k: result[k] for k in ("workers", "prune", "budget", "spent", "wall_s", "played_s", "candidates")},
        "replay": f"python -m touchline_live.scenario replay {mid} --out <dir>",
    }
    meta_p.write_text(json.dumps(md, indent=1), encoding="utf-8")
    if dst.exists():
        shutil.rmtree(dst)
    os.replace(src, dst)
    return mid


def prune_old(data_dir: Path, keep: int) -> list[str]:
    """Keep the newest `keep` written matches; the showcase matches below TL-900000 are never touched."""
    written = sorted((p for p in data_dir.glob("TL-9*") if (p / "meta.json").exists()
                      and int(p.name[3:]) >= SEED_FLOOR), key=lambda p: (p / "meta.json").stat().st_mtime)
    gone = []
    for p in written[: max(0, len(written) - keep)]:
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p.name)
    return gone


def write_match(story: str, *, data_dir: Path, seeds: int = 3, workers: int | None = None, prune: bool = True,
                use_llm: bool = True, base_seed: int | None = None, period_s: float | None = None,
                progress: Callable[[dict], None] | None = None, spec: ScenarioSpec | None = None,
                trace: list[dict] | None = None) -> dict:
    """Story in, a finished match in the data dir out (blocking; the Director call runs its own loop)."""
    story = story.strip()[:STORY_MAX]
    if spec is None:
        if progress:
            progress({"state": "directing"})
        spec, trace = asyncio.run(direct(story, use_llm=use_llm))
    trace = trace or []
    if progress:
        progress({"state": "directed", "spec": spec.model_dump(), "headline": headline(spec), "trace": trace})
    data_dir.mkdir(parents=True, exist_ok=True)
    base = base_seed if base_seed is not None else free_base_seed(data_dir, seeds * 4 + 1)
    stage = data_dir / ".staging" / f"w{base}"
    try:
        result = search(spec, stage=stage, seeds=seeds, workers=workers, prune=prune, base_seed=base,
                        period_s=period_s, progress=progress)
        mid = publish(result, story=story, spec=spec, trace=trace, stage=stage, data_dir=data_dir)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"match_id": mid, "headline": headline(spec), "spec": spec.model_dump(), "trace": trace,
            "report": {k: result["best"][k] for k in ("met", "total", "all", "beats")},
            "search": {k: result[k] for k in ("workers", "prune", "budget", "spent", "wall_s", "played_s", "candidates")}}


def replay(mid: str, data_dir: Path, out_dir: Path) -> dict:
    """Play a written match again from its meta alone and compare the event logs."""
    md = json.loads((data_dir / mid / "meta.json").read_text(encoding="utf-8"))
    sc = md["scenario"]
    spec = ScenarioSpec.model_validate(sc["spec"])
    home, away = teams_for(md["seed"], spec)
    m = EngineMatch(md["seed"], home, away).play()
    export.write(m, out_dir / mid)
    a = json.loads((data_dir / mid / "events.json").read_text(encoding="utf-8"))
    b = json.loads((out_dir / mid / "events.json").read_text(encoding="utf-8"))
    rep = validate(b, spec.beats)
    return {"match_id": mid, "identical_events": a == b, "events": len(b), "met": rep["met"], "total": rep["total"]}


# ----- command line --------------------------------------------------------------------------------

def _print_result(r: dict) -> None:
    print(f"{r['match_id']}  {r['headline']}")
    for t in r["trace"]:
        print("  trace", json.dumps(t, ensure_ascii=False))
    for b in r["report"]["beats"]:
        print(f"  {'met ' if b['ok'] else 'MISS'} {b['beat']}: {b['detail']}")
    s = r["search"]
    print(f"  {r['report']['met']}/{r['report']['total']} beats, {len(s['candidates'])} candidates, "
          f"{s['played_s'] / 60:.0f} match minutes played, {s['wall_s']:.0f} s on {s['workers']} workers")


def main() -> None:
    ap = argparse.ArgumentParser(prog="touchline_live.scenario")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="write a match from a story")
    w.add_argument("story")
    w.add_argument("--seeds", type=int, default=3)
    w.add_argument("--workers", type=int, default=None)
    w.add_argument("--no-prune", action="store_true")
    w.add_argument("--rules", action="store_true", help="use the rules Director, no model call")
    w.add_argument("--base-seed", type=int, default=None)
    w.add_argument("--data", default=os.environ.get("TOUCHLINE_DATA", str(ROOT / "data" / "matches")))
    d = sub.add_parser("direct", help="only the Director and the Gate: print the spec")
    d.add_argument("story")
    d.add_argument("--rules", action="store_true")
    r = sub.add_parser("replay", help="play a written match again from its meta and compare")
    r.add_argument("match_id")
    r.add_argument("--out", required=True)
    r.add_argument("--data", default=os.environ.get("TOUCHLINE_DATA", str(ROOT / "data" / "matches")))
    a = ap.parse_args()
    if a.cmd == "write":
        res = write_match(a.story, data_dir=Path(a.data), seeds=a.seeds, workers=a.workers, prune=not a.no_prune,
                          use_llm=not a.rules, base_seed=a.base_seed,
                          progress=lambda p: print("  ...", p.get("state"), json.dumps(p.get("playing", "")), flush=True)
                          if p.get("state") != "playing" else None)
        _print_result(res)
    elif a.cmd == "direct":
        spec, trace = asyncio.run(direct(a.story, use_llm=not a.rules))
        print(json.dumps({"headline": headline(spec), "spec": spec.model_dump(), "trace": trace}, indent=1, ensure_ascii=False))
    else:
        print(json.dumps(replay(a.match_id, Path(a.data), Path(a.out)), indent=1))


if __name__ == "__main__":
    main()
