"""The deterministic pipeline: feed -> state -> cues and moments, with pluggable sinks."""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from . import feed
from .match_io import Match
from .state import LiveState, Moment

Sink = Callable[[dict], None]


class JsonlSink:
    def __init__(self, path: str | Path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, cue: dict) -> None:
        self.f.write(json.dumps(cue, separators=(",", ":")) + "\n")
        self.f.flush()


def run_fast(m: Match, *, sinks: list[Sink] = (), on_moment: Callable[[Moment, LiveState], None] | None = None,
             track_every: int = 5, end_s: float | None = None) -> tuple[LiveState, dict]:
    """Replay a whole match as fast as possible. Returns the final state and timing stats."""
    st = LiveState(m)
    kinds = Counter()
    moments = Counter()
    t0 = time.perf_counter()
    lat = []
    for it in feed.items(m, track_every=track_every, end_s=end_s):
        a = time.perf_counter()
        if it.kind == "event":
            cues, ms = st.apply(it.payload)
        else:
            cues, ms = st.track(it.frame, it.payload, it.period, it.t), []
        lat.append(time.perf_counter() - a)
        for c in cues:
            kinds[c["kind"]] += 1
            for s in sinks:
                s(c)
        for mo in ms:
            moments[mo.kind] += 1
            if on_moment:
                on_moment(mo, st)
    stats = {
        "seconds": round(time.perf_counter() - t0, 2),
        "cues": dict(kinds), "moments": dict(moments),
        "per_item_ms_p99": round(sorted(lat)[int(0.99 * len(lat))] * 1000, 3) if lat else 0,
    }
    return st, stats
