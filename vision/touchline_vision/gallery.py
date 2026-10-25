"""Small PNGs of what the pipeline saw: the fitted pitch lines, a pass, a shot, and a failure.

    python -m touchline_vision.gallery --out reports/vision/gallery
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import cv2
import numpy as np

from .calibrate import _POLY
from .events import eval_air
from .geometry import apply_h_front, project

RED, WHITE, AMBER, CYAN, BLACK = (60, 60, 235), (245, 245, 245), (75, 184, 242), (230, 200, 60), (0, 0, 0)


def _text(img, s, xy, scale=0.55, col=WHITE):
    cv2.putText(img, s, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, BLACK, 4, cv2.LINE_AA)
    cv2.putText(img, s, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, col, 1, cv2.LINE_AA)


def lines_overlay(img, H, col=RED):
    for pl in _POLY:
        q, ok = apply_h_front(np.asarray(H), pl)
        if ok.all():
            cv2.polylines(img, [np.round(q * 4).astype(np.int32)], False, col, 1, cv2.LINE_AA, 2)


def save(img, path: Path, width: int = 800):
    h, w = img.shape[:2]
    small = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), small, [cv2.IMWRITE_PNG_COMPRESSION, 9])


def kick_frame(clips: Path, work: Path, clip: str, kick: dict, row: dict | None, title: str) -> np.ndarray:
    cal = json.loads((work / clip / "calib.json").read_text(encoding="utf-8"))
    obs = json.loads((work / clip / "obs.json").read_text(encoding="utf-8"))
    P = np.array(cal["P"])
    t0, t1 = kick["frame"], kick["end"]
    mid = (t0 + t1) // 2
    img = cv2.imread(str(clips / clip / "frames" / f"{mid:05d}.jpg"))
    lines_overlay(img, cal["H"], (90, 90, 160))
    for t in range(max(0, t0 - 10), min(obs["n"], t1 + 10)):
        b = obs["ball_px"][t]
        if b is not None:
            col = AMBER if t0 < t <= t1 else (150, 150, 150)
            cv2.circle(img, (int(round(b[0])), int(round(b[1]))), 3, col, -1, cv2.LINE_AA)
    s, e = np.array(kick["start"]), np.array(kick["end_xy"])
    ground = project(P, np.column_stack([np.linspace(s[0], e[0], 30), np.linspace(s[1], e[1], 30), np.zeros(30)]))
    cv2.polylines(img, [np.round(ground * 4).astype(np.int32)], False, CYAN, 2, cv2.LINE_AA, 2)
    for pt in (s, e):
        u, v = project(P, [[pt[0], pt[1], 0.0]])[0]
        cv2.circle(img, (int(u), int(v)), 7, CYAN, 2, cv2.LINE_AA)
    if kick["aerial"] and "air" in kick:
        tt = np.linspace(0, (t1 - t0) / 25.0, 40)
        arc = project(P, eval_air(kick["air"], tt))
        cv2.polylines(img, [np.round(arc * 4).astype(np.int32)], False, AMBER, 2, cv2.LINE_AA, 2)
    _text(img, title, (20, 36), 0.9)
    y = 70
    lines = [f"{kick['type']}  {'in the air' if kick['aerial'] else 'on the ground'}  frames {t0} to {t1}",
             f"vision: {kick['length_m']:.1f} m   {kick['speed_kmh']:.1f} km/h"]
    if row:
        truth = f"truth:  {row.get('length_true', row.get('travel_true', 0)):.1f} m   {row['speed_true']:.1f} km/h" if "length_true" in row \
            else f"truth:  {row['speed_true']:.1f} km/h"
        lines.append(truth + f"   ({row['truth_type']}, {row['outcome']})")
    for ln in lines:
        _text(img, ln, (20, y))
        y += 28
    h = img.shape[0]
    _text(img, "dots: ball seen in each frame   blue: start to end on the grass   amber arc: fitted flight in the air",
          (20, h - 18), 0.5)
    return img


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    tmp = Path(tempfile.gettempdir()) / "touchline_vision"
    ap.add_argument("--clips", type=Path, default=tmp / "clips")
    ap.add_argument("--work", type=Path, default=tmp / "run")
    ap.add_argument("--report", type=Path, default=Path("reports/vision/dev"))
    ap.add_argument("--out", type=Path, default=Path("reports/vision/gallery"))
    args = ap.parse_args(argv)
    out = args.out
    per = json.loads((args.report / "clips.json").read_text(encoding="utf-8"))

    # 1. The pitch found from its lines: the estimate drawn over the frame.
    best = min(per, key=lambda c: c["homography"]["px_mean"])
    cal = json.loads((args.work / best["clip"] / "calib.json").read_text(encoding="utf-8"))
    img = cv2.imread(str(args.work / best["clip"] / "background.jpg"))
    lines_overlay(img, cal["H"])
    _text(img, f"{best['clip']}: pitch lines from the image alone", (20, 36), 0.9)
    _text(img, f"reprojection error {best['homography']['px_mean']:.2f} px mean, {100 * best['homography']['m_mean']:.1f} cm on the grass", (20, 70))
    save(img, out / "01_calibration.png")

    # 2. A pass and 3. a shot, measured, with the truth beside them.
    for name, kind, label in (("02_pass.png", "pass", "a pass, measured from the feed"), ("03_shot.png", "shot", "a shot, measured from the feed")):
        best_k = None
        for c in per:
            ev = json.loads((args.work / c["clip"] / "events.json").read_text(encoding="utf-8"))
            tk = c["vision"]["rows"]
            for k in ev["kicks"]:
                if k["type"] != kind:
                    continue
                rows = [r for r in tk if r["det_type"] == kind and r["truth_type"] == kind and abs(r["length_det"] - k["length_m"]) < 1e-6
                        and abs(r["speed_det"] - k["speed_kmh"]) < 1e-6]
                if rows and (kind == "shot" or rows[0].get("lands_in_clip")):
                    score = (k["aerial"] if kind == "shot" else 0) * 10 + k["length_m"] - abs(rows[0]["speed_det"] - rows[0]["speed_true"])
                    if best_k is None or score > best_k[0]:
                        best_k = (score, c["clip"], k, rows[0])
        if best_k:
            _, clip, k, r = best_k
            save(kick_frame(args.clips, args.work, clip, k, r, f"{clip}: {label}"), out / name)

    # 4. A failure: a true kick the feed missed, at its kick frame.
    for c in per:
        miss = c["vision"]["missed_truth"]
        if miss and c["oracle"]["kick_found"] > c["vision"]["kick_found"]:
            m = miss[0]
            obs = json.loads((args.work / c["clip"] / "obs.json").read_text(encoding="utf-8"))
            f = min(m["rel"] + 3, obs["n"] - 1)
            img = cv2.imread(str(args.clips / c["clip"] / "frames" / f"{f:05d}.jpg"))
            for t in range(max(0, f - 12), min(obs["n"], f + 12)):
                b = obs["ball_px"][t]
                if b is not None:
                    cv2.circle(img, (int(round(b[0])), int(round(b[1]))), 3, AMBER, -1, cv2.LINE_AA)
            _text(img, f"{c['clip']}: a {m['type']} ({m['outcome']}) the feed missed, frame {m['rel']}", (20, 36), 0.9)
            _text(img, "amber dots: where the tracker saw the ball in the second around it", (20, 70))
            if m["outcome"] == "blocked":
                _text(img, "a blocked shot flies three or four frames before the blocker has it: too short to read as a kick", (20, 98))
            save(img, out / "04_failure.png")
            break
    print("gallery written to", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
