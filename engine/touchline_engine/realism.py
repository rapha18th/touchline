"""Realism report: generated matches against published league base rates.

Targets come from aggregates only. The event-level bands span the 2023-24 and 2024-25
Premier League seasons, computed from football-data.co.uk match totals (760 matches).
Passing and physical bands are literature ranges, marked as such until checked against
an open tracking sample.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from . import pitch as P
from .sim import Match

# name: (low, high, source, per)
TARGETS = {
    "goals_per_match": (2.6, 3.5, "PL 23-24 3.28, 24-25 2.93", "match"),
    "shots_per_team": (11.5, 14.5, "PL 23-24 13.8, 24-25 13.0", "team"),
    "sot_per_team": (4.0, 5.4, "PL 23-24 4.9, 24-25 4.6", "team"),
    "goals_per_shot": (0.095, 0.13, "PL 23-24 0.119, 24-25 0.113", "ratio"),
    "sot_share": (0.32, 0.39, "PL 23-24 0.358, 24-25 0.351", "ratio"),
    "fouls_per_team": (9.5, 12.5, "PL 23-24 11.1, 24-25 11.0", "team"),
    "corners_per_team": (4.4, 6.2, "PL 23-24 5.4, 24-25 5.2", "team"),
    "yellows_per_match": (3.3, 4.9, "PL 23-24 4.2, 24-25 4.1", "match"),
    "reds_per_match": (0.05, 0.3, "PL 23-24 0.15, 24-25 0.14", "match"),
    "xg_per_shot": (0.09, 0.13, "literature: top-league npxG per shot about 0.10-0.12", "ratio"),
    "passes_per_team": (380, 600, "literature: top-league range", "team"),
    "pass_completion": (0.76, 0.87, "literature: top-league range", "ratio"),
    "ball_in_play_min": (53, 61, "PL reported 2024-25 56:59", "match"),
    "match_length_min": (95, 103, "PL recent seasons about 98-100", "match"),
    "distance_km_outfield": (9.0, 12.0, "literature: 10-11 km per outfield player per 90", "player"),
    "top_speed_kmh": (30, 37, "literature: elite sprint peaks 32-36 km/h", "player"),
}


def measure(m: Match) -> dict:
    ev = m.events
    c = Counter((e["type"], e["outcome"]) for e in ev)
    t = Counter(e["type"] for e in ev)
    shots = [e for e in ev if e["type"] == "shot"]
    n_shots = len(shots)
    sot = sum(e["outcome"] in ("goal", "saved") for e in shots)
    goals = sum(e["outcome"] == "goal" for e in shots)
    passes = [e for e in ev if e["type"] == "pass"]
    complete = sum(e["outcome"] == "complete" for e in passes)
    frames = np.concatenate(m.frames, axis=0)
    # Ball in play: frames between a restart and the next stoppage.
    stops = {"out", "foul", "offside", "goal", "period_end"}
    starts = {"kickoff", "throw_in", "goal_kick", "corner", "free_kick", "penalty"}
    in_play = 0.0
    live_from = None
    for e in ev:
        g = e["frame"]
        if e["type"] in starts and live_from is None:
            live_from = g
        elif e["type"] in stops and live_from is not None:
            in_play += g - live_from
            live_from = None
    # Physical: per-slot path length, players on for at least 80 minutes.
    pl = frames[:, :22, :]
    step = np.linalg.norm(np.diff(pl, axis=0), axis=2)
    bounds = np.cumsum([0] + list(m.period_frames))
    for b in bounds[1:-1]:
        step[b - 1] = 0
    step[step > 1.0] = 0      # substitution and send-off jumps
    dist_km = step.sum(axis=0) / 1000.0
    speed = step * P.FPS * 3.6
    k = 12
    smooth = np.convolve(np.ones(k) / k, np.ones(1), mode="full")
    top = np.array([np.convolve(speed[:, s], np.ones(k) / k, mode="valid").max() for s in range(22)])
    outfield = ~m.is_gk
    total_min = frames.shape[0] / P.FPS / 60
    return {
        "score": list(m.score),
        "goals_per_match": goals,
        "shots_per_team": n_shots / 2,
        "sot_per_team": sot / 2,
        "goals_per_shot": goals / max(n_shots, 1),
        "sot_share": sot / max(n_shots, 1),
        "fouls_per_team": t["foul"] / 2,
        "corners_per_team": t["corner"] / 2,
        "yellows_per_match": c[("card", "yellow")],
        "reds_per_match": c[("card", "red")],
        "xg_per_shot": float(np.mean([e["q"]["xg"] for e in shots])) if shots else 0.0,
        "passes_per_team": len(passes) / 2,
        "pass_completion": complete / max(len(passes), 1),
        "ball_in_play_min": in_play / P.FPS / 60,
        "match_length_min": total_min,
        "distance_km_outfield": float(np.mean(dist_km[outfield]) * 90 / total_min),
        "top_speed_kmh": float(np.median(top[outfield])),
        "events": len(ev),
        "counts": {f"{a}:{b}" if b else a: v for (a, b), v in c.items()},
    }


def _one(seed: int) -> dict:
    return {"seed": seed, **measure(Match(seed).play())}


def run(seeds: list[int], workers: int = 4) -> dict:
    with ProcessPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(_one, seeds))
    return summarise(rows)


def band_distance(mean: float, lo: float, hi: float) -> float:
    """How far a mean sits outside its band, in band widths; 0 inside the band."""
    if mean < lo:
        return (lo - mean) / (hi - lo)
    if mean > hi:
        return (mean - hi) / (hi - lo)
    return 0.0


def score(report: dict) -> dict:
    """Metrics in band and the summed normalised distance to the bands: what the calibration gate reads."""
    s = report["summary"]
    dist = {k: 0.0 if s[k]["pass"] else max(round(band_distance(s[k]["mean"], s[k]["low"], s[k]["high"]), 4), 1e-4)
            for k in TARGETS}
    return {"in_band": sum(d == 0 for d in dist.values()), "distance": round(sum(dist.values()), 4),
            "per_metric": dist}


def summarise(rows: list[dict]) -> dict:
    summary = {}
    for name, (lo, hi, src, per) in TARGETS.items():
        vals = [r[name] for r in rows]
        mean = float(np.mean(vals))
        summary[name] = {"mean": round(mean, 4), "sd": round(float(np.std(vals)), 4), "low": lo, "high": hi,
                         "pass": lo <= mean <= hi, "source": src}
    results = [r["score"] for r in rows]
    hda = Counter("H" if a > b else "A" if b > a else "D" for a, b in results)
    summary["home_draw_away"] = {k: round(v / len(rows), 3) for k, v in hda.items()}
    return {"matches": len(rows), "summary": summary, "rows": rows}


def table(report: dict) -> str:
    lines = [f"{'metric':24} {'mean':>9} {'band':>15}  ok  source"]
    for name, s in report["summary"].items():
        if name == "home_draw_away":
            continue
        band = f"{s['low']}-{s['high']}"
        lines.append(f"{name:24} {s['mean']:>9.3f} {band:>15}  {'yes' if s['pass'] else ' NO'}  {s['source']}")
    lines.append(f"home/draw/away: {report['summary']['home_draw_away']}")
    ok = sum(s["pass"] for k, s in report["summary"].items() if k != "home_draw_away")
    lines.append(f"{ok}/{len(TARGETS)} metrics inside their band over {report['matches']} matches")
    return "\n".join(lines)


def save(report: dict, path: str | Path):
    Path(path).write_text(json.dumps(report, indent=1), encoding="utf-8")
