"""Match MCP server: the crew's single source of truth.

Every tool answers "as of" a match time, by replaying the event log up to that moment, so an
agent (or a judge) can only see what was known when a line went to air.

    python -m touchline_live.mcp_server            # stdio, for agents and MCP clients
    python -m touchline_live.mcp_server --http     # streamable HTTP on :8765

The web service also serves these tools at /mcp (see HTTPEndpoint). Every tool is read-only.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import math
import os
import re
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import feed
from .match_io import Match, clock, load, match_seconds
from .state import LiveState

DATA = Path(os.environ.get("TOUCHLINE_DATA", Path(__file__).resolve().parents[3] / "data" / "matches"))
INSTRUCTIONS = ("Facts about a synthetic football match, as of a match time in seconds (period 2 starts at 2700). "
                "Cite event ids from these tools; never state a number these tools did not return.")
mcp = FastMCP("touchline-match", instructions=INSTRUCTIONS)
# Every tool reads the record and changes nothing; clients may call them freely and repeat them.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


MATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@functools.lru_cache(maxsize=8)
def _match(match_id: str) -> Match:
    if not MATCH_ID.match(match_id) or not (DATA / match_id).is_dir():
        raise ValueError(f"no match {match_id!r}; list_matches gives the ids")
    return load(DATA / match_id, tracking=True)


@functools.lru_cache(maxsize=64)
def _state(match_id: str, match_s: int) -> LiveState:
    m = _match(match_id)
    st = LiveState(m)
    for it in feed.items(m, track_every=25, end_s=float(match_s)):
        if it.kind == "event":
            st.apply(it.payload)
        else:
            st.track(it.frame, it.payload, it.period, it.t)
    return st


def _asof(match_s: float) -> int:
    """Whole second just after the moment, so the moment's own events always count."""
    return int(math.ceil(match_s)) + 1


def _public(e: dict, m: Match) -> dict:
    """An event as a fan-facing fact: names resolved, the truth block removed."""
    out = {k: v for k, v in e.items() if k not in ("truth", "match_id")}
    out["clock"] = clock(e["period"], e["t"])
    out["match_s"] = round(match_seconds(e["period"], e["t"]), 1)
    if e.get("player"):
        out["player_name"] = m.name(e["player"])
    for k in ("recipient", "on", "off", "past", "won_by"):
        if e["q"].get(k):
            out["q"] = {**out["q"], f"{k}_name": m.name(e["q"][k])}
    if e.get("team"):
        out["team_name"] = m.team_name[e["team"]]
    return out


@mcp.tool(annotations=READ_ONLY)
def list_matches() -> list[dict]:
    """Every generated match available, with its clubs and final score."""
    out = []
    for p in sorted(DATA.glob("TL-*")):
        m = _match(p.name)
        out.append({"match_id": m.id, "home": m.team_name["home"], "away": m.team_name["away"], "final": m.meta["final_score"]})
    return out


@mcp.tool(annotations=READ_ONLY)
def match_info(match_id: str) -> dict:
    """Clubs, formations, style dials, squads (id, name, number, role), venue and referee."""
    m = _match(match_id)
    md = m.meta
    squads = {s: [{"id": p["id"], "name": p["name"], "number": p["number"], "role": p["role"]} for p in md[s]["players"]]
              for s in ("home", "away")}
    return {"match_id": m.id, "home": md["home"]["name"], "away": md["away"]["name"], "venue": md["venue"],
            "referee": md["referee"], "weather": md["weather"], "synthetic": True,
            "formations": {s: md[s]["formation"] for s in ("home", "away")},
            "styles": {s: md[s]["style"] for s in ("home", "away")}, "squads": squads}


@mcp.tool(annotations=READ_ONLY)
def get_events(match_id: str, ids: list[int]) -> list[dict]:
    """The events with these ids, names resolved. Use it to check any claim that cites ids."""
    m = _match(match_id)
    return [_public(m.events[i], m) for i in ids[:50] if 0 <= i < len(m.events)]


@mcp.tool(annotations=READ_ONLY)
def events_between(match_id: str, from_s: float, to_s: float, types: list[str] | None = None, limit: int = 60) -> list[dict]:
    """Events released between two match times (seconds), optionally filtered by type."""
    m = _match(match_id)
    limit = max(1, min(int(limit), 200))
    out = []
    for e in m.events:
        ms = match_seconds(e["period"], e["t_end"] if e.get("t_end") is not None else e["t"])
        if from_s <= ms <= to_s and (not types or e["type"] in types):
            out.append(_public(e, m))
            if len(out) >= limit:
                break
    return out


@mcp.tool(annotations=READ_ONLY)
def state_at(match_id: str, match_s: float) -> dict:
    """Score, team stats and the control/chaos explainer as known at this match time."""
    return _state(match_id, _asof(match_s)).snapshot()


def _player_line(match_id: str, player: str, match_s: float) -> dict:
    m = _match(match_id)
    pid = player if player in m.players else next((k for k, v in m.players.items() if player.lower() in v["name"].lower()), None)
    if pid is None:
        return {"error": f"no player matches {player!r}"}
    P = _state(match_id, _asof(match_s)).players[pid]
    return {"id": pid, "name": m.name(pid), "team": m.team_name[m.players[pid]["team"]], "role": m.players[pid]["role"],
            "passes": P.passes, "completed": P.complete, "pass_accuracy_pct": round(100 * P.complete / max(P.passes, 1)),
            "pass_m": round(P.pass_m), "hard_passes_completed": P.hard_complete, "shots": P.shots, "goals": P.goals,
            "xg": round(P.xg, 2), "key_passes": P.key_passes, "tackles": P.tackles, "interceptions": P.interceptions,
            "distance_km": round(P.dist_m / 1000, 2), "top_speed_kmh": round(P.top_kmh, 1), "sprints_near_ball": P.sprints}


@mcp.tool(annotations=READ_ONLY)
def player_line(match_id: str, player: str, match_s: float) -> dict:
    """One player's numbers up to this match time. `player` is an id like home-09 or a name."""
    return _player_line(match_id, player, match_s)


@mcp.tool(annotations=READ_ONLY)
def verify_numbers(match_id: str, match_s: float, claims: list[dict]) -> list[dict]:
    """Check typed claims against the record. Each claim is one of:
    {"kind": "score", "home": 1, "away": 0}
    {"kind": "team_stat", "team": "home"|"away", "stat": "shots"|"xg"|"passes"|..., "value": 7}
    {"kind": "player_stat", "player": "<id or name>", "stat": "goals"|"xg"|"passes"|..., "value": 2}
    {"kind": "event", "id": 812, "field": "outcome"|"q.xg"|"player_name"|..., "value": "goal"}
    {"kind": "explain", "stat": "turnovers"|"index"|"tempo_passes_per_min"|"pass_share"|"press_wins", "team": "home", "value": 41}
    Numbers pass within 2% or 0.05, whichever is larger."""
    st = _state(match_id, _asof(match_s))
    m = _match(match_id)
    return [check(c, st, m) for c in claims]


OUTCOME_ALIASES = {"save": "saved", "saves": "saved", "block": "blocked", "scored": "goal", "miss": "off_target",
                   "missed": "off_target", "wide": "off_target", "completed": "complete", "incompleted": "incomplete"}


def _field(e: dict, path: str):
    cur = e
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _close(claimed, actual) -> bool:
    """A claim passes when it matches, or when it is the actual value honestly rounded."""
    if isinstance(claimed, (int, float)) and isinstance(actual, (int, float)) and not isinstance(claimed, bool):
        if abs(claimed - actual) <= max(0.05, 0.02 * abs(actual)):
            return True
        return any(abs(round(actual, d) - claimed) < 1e-9 for d in (0, 1, 2))
    return str(claimed).strip().lower() == str(actual).strip().lower()


def check(c: dict, st: LiveState, m: Match) -> dict:
    kind = c.get("kind")
    try:
        if kind == "score":
            actual = {"home": st.score[0], "away": st.score[1]}
            ok = actual["home"] == c.get("home") and actual["away"] == c.get("away")
        elif kind == "team_stat":
            T = vars(st.teams[c["team"]])
            actual = T[c["stat"]]
            ok = _close(c["value"], actual)
        elif kind == "player_stat":
            line = _player_line(m.id, c["player"], st.match_s)
            actual = line.get(c["stat"])
            ok = actual is not None and _close(c["value"], actual)
        elif kind == "event":
            e = _public(m.events[int(c["id"])], m)
            ms = e["match_s"]
            if ms > st.match_s + 15:
                return {**c, "ok": False, "actual": None, "why": "event is in the future at this match time"}
            field = c.get("field") or "outcome"
            actual = _field(e, field)
            if actual is None and not field.startswith("q."):
                actual = _field(e, "q." + field)          # writers often drop the qualifier prefix
            if field == "outcome" and actual is None and e["type"] in ("goal", "save", "block"):
                actual = {"goal": "goal", "save": "saved", "block": "blocked"}[e["type"]]
            if c.get("value") is None:
                return {**c, "ok": True, "actual": actual, "why": "citation only"}
            value = OUTCOME_ALIASES.get(str(c.get("value")).lower(), c.get("value"))
            ok = actual is not None and _close(value, actual)
        elif kind == "explain":
            ex = st.explain()
            actual = ex.get(c["stat"])
            if isinstance(actual, dict):
                actual = actual.get(c.get("team"))
            ok = actual is not None and _close(c["value"], actual)
        else:
            return {**c, "ok": False, "why": f"unknown claim kind {kind!r}"}
        return {**c, "ok": bool(ok), "actual": actual}
    except Exception as exc:  # a malformed claim fails closed
        return {**c, "ok": False, "why": f"{type(exc).__name__}: {exc}"}


TOOLS = (list_matches, match_info, get_events, events_between, state_at, player_line, verify_numbers)


def _threaded(fn, limiter: anyio.CapacityLimiter):
    """Run a tool on a worker thread: a replay to a late match time takes about a second of CPU, and inside
    the web service that must not stall the live channels on the event loop."""
    @functools.wraps(fn)
    async def run(**kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs), limiter=limiter)
    return run


def http_server(workers: int = 2) -> FastMCP:
    """The same read-only tools for streamable HTTP inside the web service.

    Stateless, with plain JSON replies: any replica can answer any request, and a browser can call it
    with one POST. The host check against DNS rebinding is off because the server is public and read-only
    over synthetic data; there is nothing private to reach.
    """
    srv = FastMCP("touchline-match", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    limiter = anyio.CapacityLimiter(workers)
    for fn in TOOLS:
        srv.add_tool(_threaded(fn, limiter), name=fn.__name__, description=fn.__doc__, annotations=READ_ONLY)
    return srv


class HTTPEndpoint:
    """An ASGI endpoint for /mcp that a host app mounts as a route and starts in its lifespan.

    Each lifespan gets a fresh session manager, so the host can start and stop more than once (tests do).
    """

    def __init__(self, workers: int = 2):
        self.server = http_server(workers)
        self.manager: StreamableHTTPSessionManager | None = None

    @contextlib.asynccontextmanager
    async def lifespan(self):
        s = self.server.settings
        self.manager = StreamableHTTPSessionManager(app=self.server._mcp_server, json_response=s.json_response,
                                                    stateless=s.stateless_http, security_settings=s.transport_security)
        try:
            async with self.manager.run():
                yield
        finally:
            self.manager = None

    async def __call__(self, scope, receive, send):
        if self.manager is None:
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("MCP server is not running", status_code=503)(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.http:
        mcp.settings.port = a.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
