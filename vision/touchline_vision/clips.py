"""Pick clips around passes and shots, and the fixed camera that films each one.

This module is the capture side: it reads the engine's tracking to place the camera and to keep
the truth for grading. The vision pipeline never imports it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import FPS, LENGTH, WIDTH
from .geometry import PITCH_TO_WORLD, project

GK_COLOURS = {"home": "#d9c632", "away": "#3fbf9a"}     # web/js/pitch.js paints keepers in these
DEAD = {"out", "goal", "foul", "substitution", "period_start", "period_end", "card", "treatment", "var_check", "kickoff"}


@dataclass
class Clip:
    id: str
    match_id: str
    period: int
    f0: int                     # first absolute tracking frame
    f1: int                     # one past the last
    kind: str                   # shot or passes
    anchor: int                 # the event id the clip was cut around
    camera: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return self.f1 - self.f0


def load_match(path: Path) -> tuple[dict, list[dict], np.ndarray, list[int]]:
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    events = json.loads((path / "events.json").read_text(encoding="utf-8"))
    z = np.load(path / "tracking.npz")
    return meta, events, z["frames"], [int(v) for v in z["period_frames"]]


def period_span(period_frames: list[int], period: int) -> tuple[int, int]:
    start = 0 if period == 1 else period_frames[0]
    return start, start + period_frames[period - 1]


def pick(events: list[dict], period_frames: list[int], n_shot: int, n_pass: int, prefix: str,
         before_s: float = 6.0, after_s: float = 2.5, pass_len_s: float = 8.0) -> list[Clip]:
    """Shot clips spread across the match, then the busiest open-play passing windows."""
    taken: list[tuple[int, int]] = []
    clips: list[Clip] = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    def fits(period, a, b):
        s, e = period_span(period_frames, period)
        return s <= a and b <= e

    shots = [e for e in events if e["type"] == "shot" and e["outcome"] in ("goal", "saved", "off_target")]
    order = np.linspace(0, len(shots) - 1, num=min(n_shot * 3, len(shots))).round().astype(int)
    for i in order:
        if sum(c.kind == "shot" for c in clips) >= n_shot:
            break
        s = shots[int(i)]
        a, b = s["frame"] - int(before_s * FPS), s["frame"] + int(after_s * FPS)
        if not fits(s["period"], a, b) or not free(a, b):
            continue
        taken.append((a, b))
        clips.append(Clip(f"{prefix}-s{len(clips):02d}", s["match_id"], s["period"], a, b, "shot", s["id"]))

    passes = [e for e in events if e["type"] == "pass"]
    dead = [e["frame"] for e in events if e["type"] in DEAD]
    span = int(pass_len_s * FPS)
    cands = []
    for p in passes:
        a = p["frame"] - FPS
        b = a + span
        if not fits(p["period"], a, b):
            continue
        n = sum(a + FPS <= q["frame"] < b - int(1.5 * FPS) for q in passes)
        if any(a <= d < b for d in dead):
            continue
        cands.append((n, a, b, p))
    cands.sort(key=lambda c: (-c[0], c[1]))
    # Spread the passing clips: at most one per tenth of the match.
    total = sum(period_frames)
    used_bins: set[int] = set()
    for n, a, b, p in cands:
        if sum(c.kind == "passes" for c in clips) >= n_pass:
            break
        bin_ = int(10 * a / total)
        if bin_ in used_bins or not free(a, b):
            continue
        used_bins.add(bin_)
        taken.append((a, b))
        clips.append(Clip(f"{prefix}-p{len(clips):02d}", p["match_id"], p["period"], a, b, "passes", p["id"]))
    return clips


def look_camera(pos, look, fov_deg: float, w: int, h: int) -> np.ndarray:
    """The pitch-to-pixel camera of a three.js PerspectiveCamera placed at pos looking at look."""
    pw = (PITCH_TO_WORLD @ np.r_[np.asarray(pos, float), 1.0])[:3]
    lw = (PITCH_TO_WORLD @ np.r_[np.asarray(look, float), 1.0])[:3]
    fwd = lw - pw
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0.0, 1.0, 0.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    R = np.stack([right, -up, fwd])
    f = (h / 2) / np.tan(np.radians(fov_deg) / 2)
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
    return K @ np.column_stack([R, -R @ pw]) @ PITCH_TO_WORLD


def choose_camera(ball: np.ndarray, rng: np.random.Generator, w: int, h: int) -> dict:
    """A fixed broadcast camera on the near side, framed on the clip's play, jittered per clip.
    Widens until the ball stays in frame for the whole clip."""
    cx, cy = float(np.median(ball[:, 0])), float(np.median(ball[:, 1]))
    cx = float(np.clip(cx, 22, LENGTH - 22))
    pos = [LENGTH / 2 + 0.8 * (cx - LENGTH / 2) + rng.uniform(-6, 6), WIDTH + rng.uniform(40, 56), rng.uniform(22, 34)]
    look = [LENGTH / 2 + 0.9 * (cx - LENGTH / 2), WIDTH / 2 + 0.35 * (cy - WIDTH / 2) + rng.uniform(-3, 3), 0.0]
    fov = rng.uniform(26, 32)
    X = np.column_stack([ball[:, :2], np.maximum(ball[:, 2], 0.11) + 0.11])
    while fov < 44:
        uv = project(look_camera(pos, look, fov, w, h), X)
        inside = (uv[:, 0] > 8) & (uv[:, 0] < w - 8) & (uv[:, 1] > 8) & (uv[:, 1] < h - 8)
        if inside.mean() > 0.995:
            break
        fov += 1.0
    return {"pos": [round(v, 3) for v in pos], "look": [round(v, 3) for v in look], "fov": round(fov, 3)}


def ball_track(frames_cm: np.ndarray) -> np.ndarray:
    """(n, 3) ball x, y, z in metres from int16 centimetre tracking."""
    f = frames_cm.astype(np.float32) / 100.0
    return np.column_stack([f[:, 22, 0], f[:, 22, 1], f[:, 23, 0]])


def truth_events(events: list[dict], f0: int, f1: int) -> list[dict]:
    keep = ("id", "period", "t", "frame", "t_end", "team", "player", "type", "outcome", "x", "y", "end_x", "end_y", "q")
    return [{k: e.get(k) for k in keep} for e in events if f0 <= e["frame"] < f1]


def clip_json(c: Clip, meta: dict, w: int, h: int) -> dict:
    """What the pipeline may read: the frames' size and rate and the kit colours."""
    return {"id": c.id, "match_id": c.match_id, "fps": FPS, "w": w, "h": h, "n": c.n,
            "kits": {s: {**meta[s]["kit"], "keeper": GK_COLOURS[s]} for s in ("home", "away")}}


def save_truth(out: Path, c: Clip, events: list[dict], frames_cm: np.ndarray, clip_from_world: list) -> None:
    (out / "truth.json").write_text(json.dumps({"clip": asdict(c), "events": truth_events(events, c.f0, c.f1),
                                                "clip_from_world": clip_from_world}), encoding="utf-8")
    np.savez_compressed(out / "truth.npz", frames=frames_cm[c.f0:c.f1])
