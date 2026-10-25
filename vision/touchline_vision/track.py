"""The ball's path through each frame's candidates, and players followed from frame to frame."""

from __future__ import annotations

import numpy as np


def ball_path(cands: list[np.ndarray], vmax: float = 40.0, max_gap: int = 12, gap_cost: float = 0.5,
              motion: float = 1.0) -> list[int | None]:
    """Choose at most one candidate per frame so the chosen points form a plausible ball path.

    cands[t] is an (n_t, 3) array of (u, v, score) per frame. A shortest path through a DAG: each chosen
    candidate earns -(1 + score), each skipped frame costs gap_cost, each step costs motion * (speed / vmax)^2
    and is allowed only if the ball moved at most vmax pixels per frame. Returns the chosen index per frame.
    """
    T = len(cands)
    best = [np.full(len(c), np.inf) for c in cands]
    back: list[list[tuple[int, int] | None]] = [[None] * len(c) for c in cands]
    for t in range(T):
        c = cands[t]
        if len(c) == 0:
            continue
        reward = -(1.0 + np.clip(c[:, 2], -1.0, 1.0))
        best[t] = gap_cost * t + reward          # start of the path here
        for s in range(max(0, t - max_gap), t):
            p = cands[s]
            if len(p) == 0:
                continue
            dt = t - s
            d = np.linalg.norm(c[None, :, :2] - p[:, None, :2], axis=2)        # (n_s, n_t)
            step = best[s][:, None] + gap_cost * (dt - 1) + motion * (d / dt / vmax) ** 2
            step = np.where(d <= vmax * dt + 4.0, step, np.inf)
            i = np.argmin(step, axis=0)
            val = step[i, np.arange(len(c))] + reward
            better = val < best[t]
            for j in np.nonzero(better)[0]:
                best[t][j] = val[j]
                back[t][j] = (s, int(i[j]))
    # The path may end at any node; frames after it cost gap_cost each.
    end, end_cost = None, np.inf
    for t in range(T):
        if len(cands[t]):
            j = int(np.argmin(best[t]))
            v = best[t][j] + gap_cost * (T - 1 - t)
            if v < end_cost:
                end, end_cost = (t, j), v
    out: list[int | None] = [None] * T
    while end is not None:
        t, j = end
        out[t] = j
        end = back[t][j]
    return out


def fill_gaps(t: np.ndarray, xy: np.ndarray, T: int, max_gap: int = 12) -> tuple[np.ndarray, np.ndarray]:
    """Linear fill of an (n, 2) track known at frames t over 0..T-1; gaps longer than max_gap stay NaN.
    Returns the (T, 2) track and a flag of the frames that were observed."""
    out = np.full((T, 2), np.nan)
    seen = np.zeros(T, bool)
    out[t] = xy
    seen[t] = True
    for a, b in zip(t[:-1], t[1:]):
        if 1 < b - a <= max_gap:
            w = (np.arange(a + 1, b) - a) / (b - a)
            out[a + 1:b] = xy[t == a][0] * (1 - w[:, None]) + xy[t == b][0] * w[:, None]
    return out, seen


class PlayerTracker:
    """Greedy nearest-neighbour tracks on the pitch: a detection joins the closest live track of the
    same team within reach, else starts a new one. Tracks unseen for a while are closed."""

    def __init__(self, reach: float = 1.6, patience: int = 10):
        self.reach, self.patience = reach, patience
        self.tracks: list[dict] = []

    def step(self, t: int, xy: np.ndarray, team: list[str]) -> list[int]:
        live = [k for k, tr in enumerate(self.tracks) if t - tr["last"] <= self.patience]
        ids = [-1] * len(xy)
        pairs = []
        for i, p in enumerate(xy):
            for k in live:
                tr = self.tracks[k]
                if tr["team"] != team[i]:
                    continue
                d = float(np.hypot(*(np.asarray(tr["xy"][-1]) - p)))
                if d <= self.reach * (1 + 0.3 * (t - tr["last"] - 1)):
                    pairs.append((d, i, k))
        used_i, used_k = set(), set()
        for d, i, k in sorted(pairs):
            if i in used_i or k in used_k:
                continue
            used_i.add(i)
            used_k.add(k)
            ids[i] = k
        for i, p in enumerate(xy):
            if ids[i] < 0:
                self.tracks.append({"team": team[i], "t": [], "xy": [], "last": t})
                ids[i] = len(self.tracks) - 1
            tr = self.tracks[ids[i]]
            tr["t"].append(t)
            tr["xy"].append([float(p[0]), float(p[1])])
            tr["last"] = t
        return ids
