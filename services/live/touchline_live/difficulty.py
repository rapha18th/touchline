"""Pass difficulty from tracking alone.

At the release frame we read where the passer, the target and every opponent stand and fit a
logistic model of completion. Difficulty is 1 minus the predicted completion chance. The
engine's own success probability (the event's truth block) never enters the model; it grades
it afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .match_io import LENGTH, WIDTH, Match

MODEL_PATH = Path(__file__).with_name("pass_difficulty.json")
FEATURES = ["bias", "length_10m", "length_sq", "high", "high_x_length", "lane_race", "lane_opp",
            "log_lane_gap", "log_receiver_space", "log_passer_space", "forward_10m", "final_third", "box"]


def opponents_slice(team: str) -> slice:
    return slice(11, 22) if team == "home" else slice(0, 11)


def features(m: Match, e: dict) -> np.ndarray | None:
    """Features at the release frame: the passer, the intended target and every opponent.

    The intended target is the engine's aim point, so a failed pass's interception spot never
    leaks the outcome into the model."""
    if e["type"] != "pass" or m.frames is None or e["q"].get("target_x") is None:
        return None
    f = min(e["frame"], len(m.frames) - 1)
    fr = m.frames[f]
    a = np.array([e["x"], e["y"]], float)
    b = np.array([e["q"]["target_x"], e["q"]["target_y"]], float)
    opp = fr[opponents_slice(e["team"])].astype(float)
    opp = opp[(opp[:, 1] > -1) & (opp[:, 1] < WIDTH + 1)]
    seg = b - a
    L = float(np.linalg.norm(seg)) + 1e-9
    u = seg / L
    rel = opp - a
    along = rel @ u
    perp = np.abs(rel[:, 0] * u[1] - rel[:, 1] * u[0])
    in_lane = (along > 1.0) & (along < L - 1.0)
    lane_opp = float(np.sum(in_lane & (perp < 2.5)))
    lane_gap = float(np.min(perp[in_lane])) if np.any(in_lane) else 10.0
    recv_space = float(np.min(np.linalg.norm(opp - b, axis=1)))
    passer_space = float(np.min(np.linalg.norm(opp - a, axis=1)))
    high = float(e["q"].get("height") == "high")
    # Physics from tracking: can any opponent reach a point of the lane before the ball does?
    v0 = min(11.0 + 0.45 * L, 27.0)
    s = np.linspace(0.12, 0.96, 8)
    secs = 0.9 + L / 21.0 if high else 1.4 * L / v0
    tb = (s if high else 1 - (1 - s) ** (1 / 1.4)) * secs
    pts = a[None, :] + seg[None, :] * s[:, None]
    d = np.linalg.norm(opp[:, None, :] - pts[None, :, :], axis=2)
    to = np.maximum(d - 1.0, 0) / 6.5 + 0.3
    lane_race = float(np.max(tb[None, :] - to))
    ax, _ = m.to_attack(e["team"], e["period"], *a)
    bx, by = m.to_attack(e["team"], e["period"], *b)
    box = float(bx > LENGTH - 16.5 and abs(by - WIDTH / 2) < 20.16)
    return np.array([
        1.0, L / 10.0, (L / 10.0) ** 2, high, high * L / 10.0, float(np.clip(lane_race, -3, 3)), lane_opp,
        np.log(min(lane_gap, 10.0) + 0.5), np.log(min(recv_space, 15.0) + 0.5), np.log(min(passer_space, 15.0) + 0.5),
        (bx - ax) / 10.0, float(bx > 70), box,
    ])


def fit(matches: list[Match], l2: float = 1.0) -> dict:
    X, y, truth = [], [], []
    for m in matches:
        for e in m.events:
            x = features(m, e)
            if x is None:
                continue
            X.append(x)
            y.append(1.0 if e["outcome"] == "complete" else 0.0)
            truth.append(e.get("truth", {}).get("p_success", np.nan))
    X, y, truth = np.array(X), np.array(y), np.array(truth)
    w = np.zeros(X.shape[1])
    for _ in range(30):                       # Newton steps with a ridge penalty
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + l2 * np.r_[0, w[1:]]
        H = (X * (p * (1 - p))[:, None]).T @ X + l2 * np.diag(np.r_[0, np.ones(len(w) - 1)])
        w -= np.linalg.solve(H, g)
    p = 1 / (1 + np.exp(-X @ w))
    ok = ~np.isnan(truth)
    model = {
        "features": FEATURES,
        "weights": [round(float(v), 5) for v in w],
        "passes": int(len(y)),
        "completion": round(float(y.mean()), 4),
        "brier": round(float(np.mean((p - y) ** 2)), 4),
        "brier_base": round(float(np.mean((y.mean() - y) ** 2)), 4),
        "corr_with_engine_truth": round(float(np.corrcoef(p[ok], truth[ok])[0, 1]), 4),
        "calibration": calibration(p, y),
    }
    return model


def calibration(p, y, bins=8) -> list[dict]:
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (p >= lo) & (p <= hi)
        if sel.sum():
            out.append({"predicted": round(float(p[sel].mean()), 3), "observed": round(float(y[sel].mean()), 3), "n": int(sel.sum())})
    return out


class PassDifficulty:
    def __init__(self, path: Path = MODEL_PATH):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.w = np.array(d["weights"])
        self.names = d["features"]

    def completion(self, x: np.ndarray) -> float:
        return float(1 / (1 + np.exp(-x @ self.w)))

    def explain(self, x: np.ndarray) -> list[tuple[str, float]]:
        """The three features that pushed difficulty up the most, as (name, logit points)."""
        contrib = -(x * self.w)
        order = np.argsort(contrib)[::-1]
        return [(self.names[i], round(float(contrib[i]), 2)) for i in order[:3] if self.names[i] != "bias" and contrib[i] > 0]
