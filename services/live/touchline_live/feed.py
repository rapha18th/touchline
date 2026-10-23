"""The feed: replays a generated match as a live stream.

Events are released when they resolve (t_end), exactly as a live data provider would send
them; tracking samples arrive every few frames. `speed` paces the stream against the wall
clock (1 = real time, 0 = as fast as possible).
"""

from __future__ import annotations

import asyncio
import heapq
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import numpy as np

from .match_io import FPS, Match, match_seconds


@dataclass(order=True)
class Item:
    match_s: float
    order: int
    kind: str          # "event" or "track"
    period: int
    t: float
    frame: int
    payload: object = None


def items(m: Match, *, track_every: int = 5, start_s: float = 0.0, end_s: float | None = None) -> Iterator[Item]:
    heap: list[Item] = []
    for e in m.events:
        t_rel = e["t_end"] if e.get("t_end") is not None else e["t"]
        ms = match_seconds(e["period"], t_rel)
        heap.append(Item(ms, e["id"] * 2, "event", e["period"], t_rel, e["frame"], e))
    if m.frames is not None:
        offset = 0
        for period, n in enumerate(m.period_frames, start=1):
            for f in range(0, n, track_every):
                t = f / FPS
                heap.append(Item(match_seconds(period, t), 10_000_000 + offset + f, "track", period, t, offset + f, None))
            offset += n
    heapq.heapify(heap)
    while heap:
        it = heapq.heappop(heap)
        if it.match_s < start_s:
            continue
        if end_s is not None and it.match_s > end_s:
            break
        if it.kind == "track":
            it.payload = m.frames[it.frame]
        yield it


async def stream(m: Match, *, speed: float = 1.0, **kw) -> AsyncIterator[Item]:
    t0_wall = time.perf_counter()
    t0_match: float | None = None
    for it in items(m, **kw):
        if t0_match is None:
            t0_match = it.match_s
        if speed > 0:
            due = t0_wall + (it.match_s - t0_match) / speed
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        yield it
