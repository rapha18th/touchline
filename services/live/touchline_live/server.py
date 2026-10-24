"""Touchline web service: the fan app, the broadcast overlay, the match API and live channels.

    uvicorn touchline_live.server:app --port 8080

GET  /                        fan app
GET  /overlay                 broadcast overlay (transparent, for a browser source)
GET  /api/matches             matches and recorded broadcasts
GET  /api/match/{id}          meta: clubs, squads, periods
GET  /api/match/{id}/tracking 12.5 Hz tracking, int16 centimetres, little endian, (F, 24, 2)
GET  /api/match/{id}/events   public events (the engine's truth block removed)
GET  /api/broadcast/{id}      a recorded broadcast: every cue a live run put on air
WS   /ws/live/{id}            join the live channel: cues and the clock, pushed as they happen
POST /mcp                     the match MCP server over streamable HTTP (read-only tools, stateless)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from . import feed, telemetry
from .match_io import FPS, load
from .mcp_server import HTTPEndpoint
from .state import LiveState

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ.get("TOUCHLINE_DATA", ROOT / "data" / "matches"))
BROADCASTS = Path(os.environ.get("TOUCHLINE_BROADCASTS", ROOT / "broadcasts"))
WEB = Path(os.environ.get("TOUCHLINE_WEB", ROOT / "web"))
STEP = 4                                    # 25 Hz -> 6.25 Hz for the browser, which interpolates
UNIT = 10                                   # decimetres: small integers compress well
CREW = os.environ.get("TOUCHLINE_CREW", "1") == "1"

telemetry.setup("touchline-web")
MCP = HTTPEndpoint()


@asynccontextmanager
async def lifespan(_app):
    async with MCP.lifespan():
        yield


app = FastAPI(title="Touchline", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.router.routes.append(Route("/mcp", endpoint=MCP))
_cache: dict[str, object] = {}


def match(mid: str):
    if not (DATA / mid).exists():
        raise HTTPException(404, f"no match {mid}")
    if mid not in _cache:
        _cache[mid] = load(DATA / mid)
    return _cache[mid]


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/overlay")
def overlay():
    return FileResponse(WEB / "overlay.html")


@app.get("/healthz")
def health():
    return {"ok": True, "matches": len(list(DATA.glob("TL-*")))}


@app.get("/api/matches")
def matches():
    out = []
    for p in sorted(DATA.glob("TL-*")):
        md = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        out.append({"match_id": md["match_id"], "home": md["home"]["name"], "away": md["away"]["name"],
                    "home_short": md["home"]["short"], "away_short": md["away"]["short"],
                    "kits": {s: md[s]["kit"] for s in ("home", "away")}, "final": md["final_score"],
                    "venue": md["venue"], "kickoff": md["kickoff_local"],
                    "broadcast": (BROADCASTS / f"{md['match_id']}.cues.jsonl").exists()})
    return out


@app.get("/api/match/{mid}")
def meta(mid: str):
    m = match(mid)
    md = dict(m.meta)
    md["stream"] = {"hz": FPS / STEP, "step": STEP, "scale": UNIT, "frames": [int(np.ceil(n / STEP)) for n in m.period_frames]}
    return md


@app.get("/api/match/{mid}/tracking")
def tracking(mid: str):
    key = f"bin:{mid}"
    if key not in _cache:
        m = match(mid)
        parts, start = [], 0
        for n in m.period_frames:
            parts.append(m.frames[start:start + n:STEP])
            start += n
        arr = np.round(np.concatenate(parts) * UNIT).astype("<i2")
        _cache[key] = arr.tobytes()
    return Response(_cache[key], media_type="application/octet-stream",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/match/{mid}/events")
def events(mid: str):
    m = match(mid)
    keep = ("id", "period", "t", "t_end", "frame", "team", "player", "type", "outcome", "x", "y", "end_x", "end_y",
            "possession", "score", "q")
    return JSONResponse([{k: e.get(k) for k in keep} for e in m.events], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/broadcast/{mid}")
def broadcast(mid: str):
    p = BROADCASTS / f"{mid}.cues.jsonl"
    if not p.exists():
        raise HTTPException(404, "no recorded broadcast for this match")
    cues = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    for c in cues:
        c.pop("crew", None) if c.get("kind") not in ("line", "insight", "recap") else None
    return JSONResponse(cues, headers={"Cache-Control": "public, max-age=3600"})


# ----- live channels ------------------------------------------------------------------------

class Channel:
    """One live run of a match shared by every viewer who joins it."""

    def __init__(self, mid: str, speed: float, start_s: float):
        self.mid, self.speed, self.start_s = mid, speed, start_s
        self.clients: set[WebSocket] = set()
        self.task: asyncio.Task | None = None
        self.clock = start_s
        self.counts = Counter()
        self.recent: list[dict] = []          # replayed to a viewer who joins mid-run
        self.started = time.time()

    async def send_all(self, msg: dict):
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def push(self, cue: dict):
        self.counts[cue["kind"]] += 1
        slim = {k: v for k, v in cue.items() if k != "crew"}
        if cue.get("crew"):
            # The Crew room needs the whole story of a line: handoffs, timings, MCP calls and every verdict,
            # including the drafts the Verifier refused.
            slim["crew"] = {k: cue["crew"][k] for k in ("job", "trace", "attempts", "verdicts") if k in cue["crew"]}
        self.recent = (self.recent + [slim])[-60:]
        asyncio.get_event_loop().create_task(self.send_all({"type": "cue", "cue": slim}))

    async def run(self):
        m = match(self.mid)
        st = LiveState(m)
        record = None
        stack = None
        tasks = []
        try:
            if CREW:
                from contextlib import AsyncExitStack

                from .crew.verify import MatchRecord
                from .crew.workflow import run_job
                from .live import Producer, job_for
                stack = AsyncExitStack()
                record = await stack.enter_async_context(MatchRecord(DATA))
                producer = Producer()
                sem = asyncio.Semaphore(3)

                async def crew(job):
                    async with sem:
                        try:
                            await run_job(record, self.push, job)
                        except Exception as exc:
                            await self.send_all({"type": "crew_error", "kind": job.kind, "match_s": job.match_s, "error": f"{type(exc).__name__}"})
            # Catch the state up to the start point at full speed, then play live from there.
            for it in feed.items(m, track_every=25, end_s=self.start_s):
                if it.kind == "event":
                    st.apply(it.payload)
                else:
                    st.track(it.frame, it.payload, it.period, it.t)
            await asyncio.sleep(0)
            last_clock = 0.0
            async for it in feed.stream(m, speed=self.speed, track_every=5, start_s=self.start_s):
                if it.kind == "event":
                    cues, moments = st.apply(it.payload)
                else:
                    cues, moments = st.track(it.frame, it.payload, it.period, it.t), []
                for c in cues:
                    self.push(c)
                if CREW:
                    for mo in moments:
                        if producer.wants(mo):
                            tasks.append(asyncio.create_task(crew(job_for(mo, st, ["es", "zu"], None))))
                self.clock = it.match_s
                if time.perf_counter() - last_clock > 0.5:
                    last_clock = time.perf_counter()
                    await self.send_all({"type": "clock", "match_s": round(it.match_s, 2), "period": it.period,
                                         "frame": it.frame, "score": st.score})
                if not self.clients:
                    break
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.send_all({"type": "end", "score": st.score})
        finally:
            if stack:
                await stack.aclose()
            CHANNELS.pop(self.mid, None)


CHANNELS: dict[str, Channel] = {}


@app.websocket("/ws/live/{mid}")
async def live(ws: WebSocket, mid: str, speed: float = 2.0, start: float = 0.0):
    await ws.accept()
    match(mid)
    ch = CHANNELS.get(mid)
    if ch is None:
        ch = CHANNELS[mid] = Channel(mid, max(0.5, min(speed, 8.0)), max(0.0, start))
    ch.clients.add(ws)
    await ws.send_text(json.dumps({"type": "hello", "match_id": mid, "speed": ch.speed, "match_s": ch.clock,
                                   "viewers": len(ch.clients), "recent": ch.recent}, ensure_ascii=False))
    if ch.task is None:
        ch.task = asyncio.create_task(ch.run())
    try:
        while True:
            await ws.receive_text()               # viewers only listen; keep the socket open
    except WebSocketDisconnect:
        ch.clients.discard(ws)


if WEB.exists():
    app.mount("/web", StaticFiles(directory=WEB), name="web")
