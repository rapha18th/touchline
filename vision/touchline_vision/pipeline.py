"""Run the vision pipeline over filmed clips: camera, detections, tracks, events.

    python -m touchline_vision.pipeline --clips %TEMP%/touchline_vision/clips --stage all

Each stage caches its output per clip under --work, so a later stage can be rerun alone.
The pipeline reads only the frames and clip.json. truth.json is read by touchline_vision.score.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import numpy as np

from .calibrate import Calibration, calibrate
from .detect import Detector
from .geometry import apply_h, camera_from_h
from .track import PlayerTracker, ball_path, despike
from .video import Clip

WORK = Path(tempfile.gettempdir()) / "touchline_vision" / "run"
GOOD_FIT = 6.0          # chamfer cost of a converged calibration on these clips is 1 to 5


def stage_calibrate(clip: Clip, work: Path, force: bool = False) -> Calibration:
    f = work / "calib.json"
    if f.exists() and not force:
        d = json.loads(f.read_text(encoding="utf-8"))
        return Calibration(H=np.array(d["H"]), P=np.array(d["P"]), params=d["params"], fit=d["fit"])
    t0 = time.perf_counter()
    bg = clip.background()
    cal = calibrate(bg)
    # The fit grades itself: a symmetric chamfer above GOOD_FIT means the search found the wrong pitch.
    # Search again, wider, from fresh seeds, and keep the best; flag the clip if none fits.
    tries = 1
    for seed in (1, 2):
        if cal.fit["cost"] <= GOOD_FIT:
            break
        alt = calibrate(bg, seed=seed, n_search=60000, hops=16)
        tries += 1
        if alt.fit["cost"] < cal.fit["cost"]:
            cal = alt
    cal.fit["tries"] = tries
    cal.fit["trusted"] = bool(cal.fit["cost"] <= GOOD_FIT)
    # The camera used downstream is the one implied by the polished ground homography.
    P, focal = camera_from_h(cal.H, clip.w, clip.h)
    cal.fit["focal_px_from_h"] = round(focal, 2)
    cal.fit["seconds"] = round(time.perf_counter() - t0, 1)
    work.mkdir(parents=True, exist_ok=True)
    import cv2
    cv2.imwrite(str(work / "background.jpg"), bg)
    f.write_text(json.dumps({"H": cal.H.tolist(), "P": P.tolist(), "P_orbit": cal.P.tolist(), "params": cal.params,
                             "fit": cal.fit}), encoding="utf-8")
    cal.P = P
    return cal


def stage_observe(clip: Clip, cal: Calibration, work: Path, force: bool = False) -> dict:
    """Per-frame ball candidates and players, the chosen ball path, and player tracks on the pitch."""
    f = work / "obs.json"
    if f.exists() and not force:
        return json.loads(f.read_text(encoding="utf-8"))
    import cv2
    bg = cv2.imread(str(work / "background.jpg"))
    det = Detector(bg, clip.info["kits"], cal.H)
    Hinv = np.linalg.inv(cal.H)
    cands, players = [], []
    tracker = PlayerTracker()
    for i in range(len(clip)):
        balls, pls, _ = det.detect(clip.frame(i))
        cands.append(np.array([[b.u, b.v, b.score] for b in balls], float).reshape(-1, 3))
        keep = [p for p in pls if p.label != "other"]
        xy = apply_h(Hinv, [[p.u, p.v] for p in keep]) if keep else np.zeros((0, 2))
        ids = tracker.step(i, xy, [p.label.replace("_gk", "") for p in keep])
        players.append([[round(float(x), 3), round(float(y), 3), p.label, int(k), round(p.u, 1), round(p.v, 1)]
                        for (x, y), p, k in zip(xy, keep, ids)])
    out = {"n": len(clip), "fps": clip.fps, "cands": [np.round(c, 2).tolist() for c in cands], "players": players,
           "candidates_per_frame": round(float(np.mean([len(c) for c in cands])), 2)}
    out["ball_px"] = choose_ball(out)
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def choose_ball(obs: dict) -> list:
    """The ball's pixel per frame (None where unseen) from the stored candidates."""
    cands = [np.array(c, float).reshape(-1, 3) for c in obs["cands"]]
    path = despike(cands, ball_path(cands))
    return [None if j is None else [round(float(cands[t][j, 0]), 2), round(float(cands[t][j, 1]), 2)] for t, j in enumerate(path)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clips", type=Path, default=Path(tempfile.gettempdir()) / "touchline_vision" / "clips")
    ap.add_argument("--work", type=Path, default=WORK)
    ap.add_argument("--stage", choices=["calibrate", "observe", "events", "all"], default="all")
    ap.add_argument("--only", nargs="*", default=None, help="clip ids")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    for d in sorted(p for p in args.clips.iterdir() if (p / "clip.json").exists()):
        clip = Clip(d)
        if args.only and clip.id not in args.only:
            continue
        work = args.work / clip.id
        work.mkdir(parents=True, exist_ok=True)
        stages = ["calibrate", "observe", "events"] if args.stage == "all" else [args.stage]
        cal = stage_calibrate(clip, work, force=args.force and "calibrate" in stages)
        msg = {"clip": clip.id, "calib_cost": cal.fit.get("cost")}
        if "observe" in stages or "events" in stages:
            obs = stage_observe(clip, cal, work, force=args.force and "observe" in stages)
            msg["ball_seen"] = round(sum(b is not None for b in obs["ball_px"]) / obs["n"], 3)
        if "events" in stages:
            from . import events as E
            obs["ball_px"] = choose_ball(obs)
            (work / "obs.json").write_text(json.dumps(obs), encoding="utf-8")
            ev = E.detect_events(obs, cal.P, cal.H)
            (work / "events.json").write_text(json.dumps(ev, indent=1), encoding="utf-8")
            msg["kicks"] = len(ev["kicks"])
        print(json.dumps(msg), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
