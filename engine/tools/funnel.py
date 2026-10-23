"""Attacking funnel per team for one match, to compare with published per-team rates.

Published top-league per-team per-match reference points (FBref-style definitions):
passes into final third about 40-45, completed passes into the box about 10-12,
carries into the box about 5-8, touches in the attacking box about 20-28, shots about 13.
"""

import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from touchline_engine import pitch as P  # noqa: E402
from touchline_engine.sim import Match  # noqa: E402


def frame_of(e, x, y):
    pos = (e["team"] == "home") == (e["period"] == 1)
    return (x, y) if pos else (P.LENGTH - x, P.WIDTH - y)


def funnel(m: Match) -> dict:
    out = {t: Counter() for t in ("home", "away")}
    for e in m.events:
        if e["team"] not in out or e["x"] is None:
            continue
        c = out[e["team"]]
        sx, sy = frame_of(e, e["x"], e["y"])
        if e["end_x"] is not None:
            ex, ey = frame_of(e, e["end_x"], e["end_y"])
        else:
            ex, ey = sx, sy
        if e["type"] == "pass":
            if sx <= 70 < ex:
                c["passes_into_final_third"] += 1
            if not P.in_box(sx, sy) and P.in_box(ex, ey):
                c["passes_into_box"] += 1
                c["passes_into_box_completed"] += e["outcome"] == "complete"
        if e["type"] == "carry" and not P.in_box(sx, sy) and P.in_box(ex, ey):
            c["carries_into_box"] += 1
        if e["type"] in ("pass", "carry", "shot", "ball_recovery", "interception", "dribble") and P.in_box(sx, sy):
            c["box_actions"] += 1
        if e["type"] == "shot":
            c["shots"] += 1
            c["shots_" + (e["q"].get("set_piece") or ("header" if e["q"]["body_part"] == "head" else "open"))] += 1
        if e["type"] in ("tackle", "interception", "ball_recovery") and e["outcome"] != "lost" and sx > 70:
            c["high_regains"] += 1
    return {t: dict(c) for t, c in out.items()}


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    m = Match(seed).play()
    for t, c in funnel(m).items():
        print(t, m.score, dict(sorted(c.items())))
