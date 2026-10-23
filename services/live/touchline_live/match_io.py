"""Load a generated match and the helpers every live service shares."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

LENGTH, WIDTH, FPS = 105.0, 68.0, 25
HALF_S = 2700.0


@dataclass
class Match:
    path: Path
    meta: dict
    events: list[dict]
    frames: np.ndarray | None = None          # (F, 24, 2) metres
    period_frames: list[int] = field(default_factory=list)
    players: dict[str, dict] = field(default_factory=dict)
    team_name: dict[str, str] = field(default_factory=dict)
    team_short: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.meta["match_id"]

    def name(self, pid: str | None) -> str:
        return self.players[pid]["name"] if pid in self.players else ""

    def short(self, pid: str | None) -> str:
        return self.players[pid]["short"] if pid in self.players else ""

    def period_start_frame(self, period: int) -> int:
        return 0 if period == 1 else self.period_frames[0]

    def attacks_positive(self, team: str, period: int) -> bool:
        return (team == "home") == (period == 1)

    def to_attack(self, team: str, period: int, x: float, y: float) -> tuple[float, float]:
        if self.attacks_positive(team, period):
            return x, y
        return LENGTH - x, WIDTH - y


def load(path: str | Path, *, tracking: bool = True) -> Match:
    p = Path(path)
    meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
    events = json.loads((p / "events.json").read_text(encoding="utf-8"))
    m = Match(path=p, meta=meta, events=events)
    for side in ("home", "away"):
        t = meta[side]
        m.team_name[side] = t["name"]
        m.team_short[side] = t["short"]
        for pl in t["players"]:
            m.players[pl["id"]] = {**pl, "team": side}
    if tracking:
        z = np.load(p / "tracking.npz")
        m.frames = z["frames"].astype(np.float32) / 100.0
        m.period_frames = [int(v) for v in z["period_frames"]]
    else:
        m.period_frames = [pp["frames"] for pp in meta["periods"]]
    return m


def match_seconds(period: int, t: float) -> float:
    return t if period == 1 else HALF_S + t


def clock(period: int, t: float) -> str:
    """Broadcast clock: 63:12, or 45+2:10 in stoppage time."""
    base = 0 if period == 1 else 45
    mins = int(t // 60)
    secs = int(t % 60)
    if mins >= 45:
        return f"{base + 45}+{mins - 45 + 1}:{secs:02d}" if mins > 45 or secs > 0 else f"{base + 45}:00"
    return f"{base + mins}:{secs:02d}"


def minute(period: int, t: float) -> str:
    """Minute as broadcasters say it: 63', 45+2'."""
    base = 0 if period == 1 else 45
    mins = int(t // 60) + 1
    if mins > 45:
        return f"{base + 45}+{mins - 45}'"
    return f"{base + mins}'"
