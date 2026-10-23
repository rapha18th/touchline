"""Run a match live: feed -> state -> cues, and moments -> the crew -> verified lines.

    python -m touchline_live.live TL-000007 --speed 8 --langs es zu --until 1800
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from . import feed
from .crew.models import Job
from .crew.verify import MatchRecord
from .crew.workflow import RECAP_KINDS, build, template
from .match_io import load
from .state import LiveState, Moment

ROOT = Path(__file__).resolve().parents[3]

# Which moments earn words, and how often.
ALWAYS = {"goal", "red_card", "half_time", "full_time"}
GAP_S = {"big_chance": 60, "big_save": 60, "hard_pass": 240, "milestone": 120, "momentum": 600}


class Producer:
    """The editorial policy in front of the crew: what deserves a line, and how often."""

    def __init__(self):
        self.last: dict[str, float] = {}

    def wants(self, mo: Moment) -> bool:
        if mo.kind in ALWAYS:
            return True
        gap = GAP_S.get(mo.kind)
        if gap is None:
            return False
        if mo.match_s - self.last.get(mo.kind, -1e9) < gap:
            return False
        self.last[mo.kind] = mo.match_s
        return True


def job_for(mo: Moment, st: LiveState, langs: list[str], viewer: dict | None) -> Job:
    m = st.match
    roster = [p["name"] for p in m.players.values()]
    return Job(match_id=m.id, kind=mo.kind, match_s=mo.match_s, frame=mo.frame, period=mo.period,
               priority=mo.priority, facts=mo.facts, sources=mo.sources,
               context={"viewer": viewer} if viewer else {},
               personas=["analyst", "casual", "studio"] if mo.kind not in RECAP_KINDS else ["studio"],
               langs=["en", *langs], team=mo.team, player=mo.player, roster=roster,
               clubs=[m.team_name["home"], m.team_name["away"]])


async def run(match_id: str, *, speed: float, langs: list[str], until: float | None, out: Path,
              concurrency: int = 3, viewer: dict | None = None) -> dict:
    m = load(ROOT / "data" / "matches" / match_id)
    st = LiveState(m)
    producer = Producer()
    out.parent.mkdir(parents=True, exist_ok=True)
    f = open(out, "w", encoding="utf-8")
    write = lambda c: (f.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n"), f.flush())
    counts, results = Counter(), []
    sem = asyncio.Semaphore(concurrency)
    t0 = time.perf_counter()

    async with MatchRecord(ROOT / "data" / "matches") as record:
        async def crew(job: Job):
            async with sem:
                a = time.perf_counter()
                try:
                    wf = build(record, write)
                    res = await wf.run(job)
                    outs = res.get_outputs()
                    results.append({"kind": job.kind, "match_s": job.match_s, "ms": round((time.perf_counter() - a) * 1000),
                                    "fallback": job.fallback, "attempts": job.attempts, "trace": job.trace,
                                    "outputs": outs})
                except Exception as exc:
                    results.append({"kind": job.kind, "match_s": job.match_s, "error": f"{type(exc).__name__}: {exc}"})

        tasks = []
        async for it in feed.stream(m, speed=speed, end_s=until):
            if it.kind == "event":
                cues, moments = st.apply(it.payload)
            else:
                cues, moments = st.track(it.frame, it.payload, it.period, it.t), []
            for c in cues:
                counts[c["kind"]] += 1
                write(c)
            for mo in moments:
                if producer.wants(mo):
                    job = job_for(mo, st, langs, viewer)
                    if mo.priority >= 3 and mo.kind not in RECAP_KINDS:
                        # Instant caption from verified facts; the crew's lines follow a few seconds later.
                        c = st.cue("line", priority=3, ttl=6, sources=mo.sources, personas=("studio",), team=mo.team,
                                   player=mo.player, payload={"text": template(job, "studio"), "persona": "studio",
                                                              "moment": mo.kind, "instant": True})
                        counts["instant_line"] += 1
                        write(c)
                    tasks.append(asyncio.create_task(crew(job)))
            if speed == 0 and len(tasks) % 4 == 0:
                await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        mcp_calls = record.calls
    f.close()
    return {"match": match_id, "score": st.score_line(), "wall_s": round(time.perf_counter() - t0, 1),
            "state_cues": dict(counts), "crew_jobs": len(results), "mcp_calls": mcp_calls,
            "fallbacks": sum(1 for r in results if r.get("fallback")), "repairs": sum(1 for r in results if r.get("attempts")),
            "errors": [r for r in results if "error" in r], "jobs": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("match_id")
    ap.add_argument("--speed", type=float, default=8.0)
    ap.add_argument("--langs", nargs="*", default=["es"])
    ap.add_argument("--until", type=float, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--concurrency", type=int, default=3)
    a = ap.parse_args()
    out = Path(a.out) if a.out else ROOT / "data" / "runs" / f"{a.match_id}.cues.jsonl"
    rep = asyncio.run(run(a.match_id, speed=a.speed, langs=a.langs, until=a.until, out=out, concurrency=a.concurrency))
    (out.with_suffix(".report.json")).write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "jobs"}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
