"""Write a played match to disk: meta.json, events.json and tracking.npz."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import pitch as P
from .sim import N, Match

SCHEMA_VERSION = "touchline.match/1"
VENUES = ["Riverside Park", "The Foundry Ground", "Harbour Road", "Kestrel Lane", "Old Mill Stadium", "Northbank Arena"]
WEATHER = ["clear", "light rain", "overcast", "cold and dry", "windy", "mild"]


def match_id(seed: int) -> str:
    return f"TL-{seed:06d}"


def meta(m: Match) -> dict:
    rng = np.random.default_rng([m.seed, 0x5EED])
    kick = datetime(2026, 8, 15, 15, 0) + timedelta(days=int(rng.integers(0, 270)), hours=int(rng.choice([0, 2, 4, 5])))
    home, away = m.teams
    return {
        "schema": SCHEMA_VERSION,
        "match_id": match_id(m.seed),
        "seed": m.seed,
        "synthetic": True,
        "competition": "Touchline Synthetic League",
        "kickoff_local": kick.isoformat(timespec="minutes"),
        "venue": VENUES[int(rng.integers(len(VENUES)))],
        "weather": WEATHER[int(rng.integers(len(WEATHER)))],
        "referee": f"Ref. {['Dale', 'Okoro', 'Vance', 'Pillai', 'Moreau', 'Hart'][int(rng.integers(6))]}",
        "pitch": {"length_m": P.LENGTH, "width_m": P.WIDTH},
        "fps": P.FPS,
        "direction": "home attacks +x in period 1, -x in period 2",
        "periods": [{"period": i + 1, "frames": f, "length_s": round(f / P.FPS, 1)} for i, f in enumerate(m.period_frames)],
        "final_score": {"home": m.score[0], "away": m.score[1]},
        "home": home.as_dict(),
        "away": away.as_dict(),
        "tracking": {
            "file": "tracking.npz",
            "slots": "0-10 home in formation order, 11-21 away; substitution events name the slot",
            "units": "centimetres, int16; ball z in row 23 col 0",
        },
    }


def write(m: Match, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = meta(m)
    mid = md["match_id"]
    events = [{"match_id": mid, **e} for e in m.events]
    (out / "meta.json").write_text(json.dumps(md, indent=1), encoding="utf-8")
    (out / "events.json").write_text(json.dumps(events, separators=(",", ":")), encoding="utf-8")
    frames = np.concatenate(m.frames, axis=0)
    cm = np.round(frames * 100).astype(np.int16)
    np.savez_compressed(out / "tracking.npz", frames=cm, period_frames=np.array(m.period_frames, np.int32), slots=N)
    return out


def load_tracking(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Frames as float metres (F, 24, 2) and the frame count of each period."""
    z = np.load(Path(path) / "tracking.npz")
    return z["frames"].astype(np.float32) / 100.0, z["period_frames"]
