"""Touchline web service: the fan app, the broadcast overlay, the match API and live channels.

    uvicorn touchline_live.server:app --port 8080

GET  /                        fan app
GET  /overlay                 broadcast overlay (transparent, for a browser source)
GET  /api/matches             matches and recorded broadcasts
GET  /api/match/{id}          meta: clubs, squads, periods
GET  /api/match/{id}/tracking 12.5 Hz tracking, int16 centimetres, little endian, (F, 24, 2)
GET  /api/match/{id}/events   public events (the engine's truth block removed)
GET  /api/broadcast/{id}      a recorded broadcast: every cue a live run put on air
GET  /api/audio/{name}        a voiced line (mp3): the live crew's cache, or {match}/{name} for a recording
WS   /ws/live/{id}            join the live channel: cues and the clock, pushed as they happen
POST /mcp                     the match MCP server over streamable HTTP (read-only tools, stateless):
                              POST only, one worker, 429 when busy, a token bucket per client
POST /api/ask/{id}            Ask the record: an agent answers over /mcp, the Verifier passes it first;
                              six questions per client in ten minutes, beside a cap for the whole page
POST /api/scenario            Write the match: a story in, a background job out (its id)
GET  /api/scenario/{job}      the job's progress: candidates played, beats met, the match id when done
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.routing import Route

from . import feed, telemetry
from .match_io import FPS, load
from .guard import McpGate, Window, client_key
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
MCP = HTTPEndpoint(workers=1)
MCP_GATE = McpGate(MCP, workers=1)


@asynccontextmanager
async def lifespan(_app):
    async with MCP.lifespan():
        yield


app = FastAPI(title="Touchline", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.router.routes.append(Route("/mcp", endpoint=MCP_GATE))
CACHE_MATCHES = int(os.environ.get("TOUCHLINE_CACHE_MATCHES", "8"))
_cache: OrderedDict[str, object] = OrderedDict()     # match id -> Match, and "bin:<id>" -> its tracking bytes
_cache_lock = threading.Lock()


def _cached(key: str):
    with _cache_lock:
        v = _cache.get(key)
        if v is not None:
            _cache.move_to_end(key)
        return v


def _keep(key: str, value):
    """Hold at most CACHE_MATCHES matches; the oldest leaves together with its tracking bytes."""
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        mids = [k for k in _cache if not k.startswith("bin:")]
        for old in mids[: max(0, len(mids) - CACHE_MATCHES)]:
            _cache.pop(old, None)
            _cache.pop(f"bin:{old}", None)
    return value


def _forget(mid: str):
    with _cache_lock:
        _cache.pop(mid, None)
        _cache.pop(f"bin:{mid}", None)


def match(mid: str):
    if not (DATA / mid).exists():
        raise HTTPException(404, f"no match {mid}")
    return _cached(mid) or _keep(mid, load(DATA / mid))


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
        try:
            md = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sc = md.get("scenario")
        out.append({"match_id": md["match_id"], "home": md["home"]["name"], "away": md["away"]["name"],
                    "home_short": md["home"]["short"], "away_short": md["away"]["short"],
                    "kits": {s: md[s]["kit"] for s in ("home", "away")}, "final": md["final_score"],
                    "venue": md["venue"], "kickoff": md["kickoff_local"],
                    "broadcast": (BROADCASTS / f"{md['match_id']}.cues.jsonl").exists(),
                    "voiced": (BROADCASTS / "audio" / md["match_id"]).is_dir(),
                    # A written match shows the headline built from its typed beats, never the story's own words.
                    "written": None if not sc else {"headline": sc["headline"], "created": sc.get("created"),
                                                    "met": sc["report"]["met"], "total": sc["report"]["total"]}})
    return out


def public_scenario(sc: dict) -> dict:
    """What a viewer of a written match sees: the headline and the beat report. The story stays on the server."""
    return {"headline": sc["headline"], "created": sc.get("created"),
            "report": {"met": sc["report"]["met"], "total": sc["report"]["total"],
                       "beats": [{k: b[k] for k in ("beat", "ok", "detail", "sources")} for b in sc["report"]["beats"]]},
            "search": {k: sc["search"][k] for k in ("workers", "prune", "budget", "spent", "wall_s", "played_s")}}


@app.get("/api/match/{mid}")
def meta(mid: str):
    m = match(mid)
    md = dict(m.meta)
    if md.get("scenario"):
        md["scenario"] = public_scenario(md["scenario"])
    md["stream"] = {"hz": FPS / STEP, "step": STEP, "scale": UNIT, "frames": [int(np.ceil(n / STEP)) for n in m.period_frames]}
    return md


@app.get("/api/match/{mid}/tracking")
def tracking(mid: str):
    key = f"bin:{mid}"
    data = _cached(key)
    if data is None:
        m = match(mid)
        parts, start = [], 0
        for n in m.period_frames:
            parts.append(m.frames[start:start + n:STEP])
            start += n
        data = _keep(key, np.round(np.concatenate(parts) * UNIT).astype("<i2").tobytes())
    return Response(data, media_type="application/octet-stream",
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


AUDIO_NAME =re.compile(r"^(?:(TL-\d{6})/)?([0-9a-f]{20})\.mp3$")


@app.get("/api/audio/{name:path}")
def audio(name: str):
    # Clips are named by a hash of their text and voice, so a name never changes what it plays.
    m = AUDIO_NAME.match(name)
    if not m:
        raise HTTPException(404, "no such clip")
    from .voice import CACHE
    match_id, clip = m.groups()
    p = (BROADCASTS / "audio" / match_id if match_id else CACHE) / f"{clip}.mp3"
    if not p.is_file():
        raise HTTPException(404, "no such clip")
    return FileResponse(p, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ----- ask the record ------------------------------------------------------------------------
ASK_LIMIT = int(os.environ.get("TOUCHLINE_ASK_PER_10MIN", "40"))   # a public page spends real model credit
ASK_PER_CLIENT = int(os.environ.get("TOUCHLINE_ASK_PER_CLIENT", "6"))
ASKED = Window(ASK_LIMIT, ASK_PER_CLIENT, span=600)
_ask_gate = asyncio.Semaphore(2)


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    match_s: float = Field(ge=0, le=7200)


@app.post("/api/ask/{mid}")
async def ask_the_record(mid: str, q: Question, request: Request):
    match(mid)
    if not CREW:
        raise HTTPException(503, "the crew is off on this server")
    who = client_key(request.headers, request.client)
    full, stamp = ASKED.take(who)
    if full == "global":
        raise HTTPException(429, "the record has answered a lot of questions; try again in a few minutes")
    if full == "client":
        raise HTTPException(429, f"you have asked {ASKED.per_client} questions in ten minutes; try again in a few minutes")
    from .crew.ask import ask
    port = (request.scope.get("server") or ("127.0.0.1", 8080))[1]
    url = os.environ.get("TOUCHLINE_MCP_URL", f"http://127.0.0.1:{port}/mcp")
    progress = {"attempts": 0}
    async with _ask_gate:
        try:
            return await asyncio.wait_for(ask(q.question, mid, q.match_s, url, progress=progress), 75)
        except Exception as exc:
            # Our failure, not the viewer's question: the slot comes back, and the reply says what broke.
            ASKED.give_back(who, stamp)
            err = f"{type(exc).__name__}: {str(exc)[:300]}".rstrip(": ")
            return JSONResponse(status_code=502, content={
                "detail": f"the record agent did not answer: {type(exc).__name__}",
                "error": err, "attempts": progress["attempts"]})


# ----- write the match -----------------------------------------------------------------------
# One job plays at a time on a host; the engine already uses the process pool scenario.pool_size picks.
WRITE_LIMIT = int(os.environ.get("TOUCHLINE_SCENARIO_PER_HOUR", "12"))
WRITE_QUEUE = int(os.environ.get("TOUCHLINE_SCENARIO_QUEUE", "3"))
WRITE_KEEP = int(os.environ.get("TOUCHLINE_SCENARIO_KEEP", "12"))
WRITE_SEEDS = int(os.environ.get("TOUCHLINE_SCENARIO_SEEDS", "3"))
WRITE_PER_CLIENT = int(os.environ.get("TOUCHLINE_SCENARIO_PER_CLIENT", "3"))
WROTE = Window(WRITE_LIMIT, WRITE_PER_CLIENT, span=3600)
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scenario")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()          # sync endpoints run on parallel threads


class Story(BaseModel):
    story: str = Field(min_length=8, max_length=300)


def _job_view(job: dict) -> dict:
    keep = ("id", "state", "story", "headline", "beats", "gate", "director", "repaired", "played", "playing", "best", "budget",
            "spent", "workers", "match_id", "report", "error", "elapsed_s", "queued_behind")
    view = {k: job[k] for k in keep if k in job}
    if job["state"] in ("queued", "directing", "playing"):
        view["elapsed_s"] = round(time.time() - job["created"], 1)
        if job["state"] == "queued":
            with _jobs_lock:
                ahead = list(_jobs.values())
            view["queued_behind"] = sum(1 for j in ahead if j["state"] in ("queued", "directing", "playing")
                                        and j["created"] < job["created"])
    return view


def _write_job(job: dict) -> None:
    from . import scenario

    def progress(p: dict) -> None:
        st = p.get("state")
        if st == "directing":
            job["state"] = "directing"
        elif st == "directed":
            spec = scenario.ScenarioSpec.model_validate(p["spec"])
            gates = [t for t in p["trace"] if t.get("role") == "gate"]
            refusals = lambda t: [{k: n[k] for k in ("what", "why")} for n in t["notes"] if n["action"] == "refuse"]
            job.update(state="playing", headline=p["headline"], beats=[scenario.describe(b) for b in spec.beats],
                       director=next((t.get("model") for t in p["trace"] if t.get("role") == "director" and t.get("model")), None),
                       repaired=any(t.get("repair") for t in p["trace"]),
                       # The gate's refusals in the first round (which the Director then repaired) and in the last.
                       gate={"first": refusals(gates[0]) if len(gates) > 1 else [], "final": refusals(gates[-1]) if gates else []})
        elif st in ("playing", "chosen"):
            job.update(played=[{k: c.get(k) for k in ("seed", "complete", "pruned_at", "met", "total", "score")}
                               for c in p["candidates"]],
                       playing=[pl["minute"] for pl in p["playing"]], best=p["best"], budget=p["budget"],
                       spent=p["spent"], workers=p["workers"])

    try:
        res = scenario.write_match(job["story"], data_dir=DATA, seeds=WRITE_SEEDS, progress=progress, use_llm=CREW)
        for gone in scenario.prune_old(DATA, WRITE_KEEP):
            _forget(gone)
        job.update(state="done", match_id=res["match_id"], playing=[],
                   report={"met": res["report"]["met"], "total": res["report"]["total"],
                           "beats": [{k: b[k] for k in ("beat", "ok", "detail")} for b in res["report"]["beats"]]})
    except scenario.NoBeats as exc:
        WROTE.give_back(job["who"], job["stamp"])      # nothing was played: the slot comes back
        job.update(state="failed", error=str(exc))
    except Exception as exc:
        logging.getLogger("touchline.scenario").exception("scenario job %s failed", job["id"])
        WROTE.give_back(job["who"], job["stamp"])      # our failure, not the viewer's story
        job.update(state="failed", error=f"{type(exc).__name__}: the match could not be written")
    job["elapsed_s"] = round(time.time() - job["created"], 1)


@app.post("/api/scenario", status_code=202)
def write_scenario(s: Story, request: Request):
    now = time.time()
    who = client_key(request.headers, request.client)
    with _jobs_lock:
        # WRITE_QUEUE counts the job that is playing and the ones waiting behind it.
        if sum(1 for j in _jobs.values() if j["state"] in ("queued", "directing", "playing")) >= WRITE_QUEUE:
            raise HTTPException(429, "the engine is busy writing other matches; try again in a few minutes")
        full, stamp = WROTE.take(who)
        if full == "global":
            raise HTTPException(429, "a lot of matches have been written this hour; try again later")
        if full == "client":
            raise HTTPException(429, f"you have written {WROTE.per_client} matches this hour; try again later")
        for old in [k for k, j in _jobs.items() if now - j["created"] > 6 * 3600]:
            _jobs.pop(old, None)
        job = {"id": uuid.uuid4().hex[:12], "state": "queued", "story": s.story.strip(), "created": now,
               "who": who, "stamp": stamp}
        _jobs[job["id"]] = job
    _writer.submit(_write_job, job)
    return _job_view(job)


@app.get("/api/scenario/{jid}")
def scenario_job(jid: str):
    with _jobs_lock:
        job = _jobs.get(jid)
    if job is None:
        raise HTTPException(404, "no such job on this server")
    return _job_view(job)


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
                            tasks.append(asyncio.create_task(crew(job_for(mo, st, ["es", "fr", "pt"], None))))
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
