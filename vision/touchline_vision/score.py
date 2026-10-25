"""Grade the pipeline against the engine's truth.

    python -m touchline_vision.score --out reports/vision

Truth is the engine's events (q.length_m, q.ball_speed_kmh, q.shot_speed_kmh), its tracking and the
renderer's camera matrix. A detected kick matches a true pass or shot whose kick frame is within
MATCH_FRAMES; each truth event takes at most one detection. The oracle row runs the same event code on
the true ball, projected through the true camera, with the true players: it is the ceiling the event
logic allows when detection and calibration are perfect.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import numpy as np

from .events import BALL_Z, detect_events
from .geometry import camera_centre, h_from_p, homography_error, p_from_clip, project

MATCH_FRAMES = 4
KICKS = ("pass", "shot")


def load_truth(clip_dir: Path) -> dict:
    t = json.loads((clip_dir / "truth.json").read_text(encoding="utf-8"))
    t["frames"] = np.load(clip_dir / "truth.npz")["frames"].astype(float) / 100.0
    return t


def truth_kicks(t: dict, n: int) -> list[dict]:
    f0 = t["clip"]["f0"]
    out = []
    for e in t["events"]:
        if e["type"] in KICKS:
            r = e["frame"] - f0
            if 2 <= r < n - 4:
                end = round(e["t_end"] * 25) - f0 if e.get("t_end") is not None else r
                out.append({**e, "rel": r, "lands_in_clip": end <= n - 2})
    return out


def match(dets: list[dict], truth: list[dict], tol: int = MATCH_FRAMES) -> list[tuple[int, int]]:
    """Greedy one-to-one pairs (det index, truth index) by kick frame distance within tol."""
    pairs = sorted((abs(d["frame"] - e["rel"]), i, j) for i, d in enumerate(dets) for j, e in enumerate(truth)
                   if abs(d["frame"] - e["rel"]) <= tol)
    used_i, used_j, out = set(), set(), []
    for _, i, j in pairs:
        if i not in used_i and j not in used_j:
            used_i.add(i)
            used_j.add(j)
            out.append((i, j))
    return out


def oracle_obs(t: dict, w: int, h: int) -> tuple[dict, np.ndarray]:
    """What a perfect detector would report: the true ball and players in the true camera."""
    fr = t["frames"]
    P = p_from_clip(t["clip_from_world"][0], w, h)
    z = np.maximum(0.11, fr[:, 23, 0]) + 0.11
    uv = project(P, np.column_stack([fr[:, 22, 0], fr[:, 22, 1], z]))
    ball = [[float(u), float(v)] if 0 <= u < w and 0 <= v < h else None for u, v in uv]
    players = []
    for k in range(len(fr)):
        row = []
        for s in range(22):
            lab = ("home" if s < 11 else "away") + ("_gk" if s in (0, 11) else "")
            x, y = fr[k, s]
            if -1.5 <= y <= 69.5:
                row.append([float(x), float(y), lab, s, 0.0, 0.0])
        players.append(row)
    return {"n": len(fr), "fps": 25.0, "ball_px": ball, "players": players}, P


def in_window(dets: list[dict], n: int) -> list[dict]:
    """Detections in the same frame window as the truth: a kick in the first two frames was under way
    before the clip began, and one in the last four has too little flight to read."""
    return [d for d in dets if 2 <= d["frame"] < n - 4]


def grade_kicks(dets: list[dict], truth: list[dict]) -> dict:
    pairs = match(dets, truth)
    rows = []
    for i, j in pairs:
        d, e = dets[i], truth[j]
        q = e["q"]
        row = {"truth_type": e["type"], "det_type": d["type"], "frame_err": d["frame"] - e["rel"],
               "outcome": e["outcome"], "height": q.get("height"), "aerial": d["aerial"], "lands_in_clip": e["lands_in_clip"]}
        if e["type"] == "pass":
            row["length_true"] = q["length_m"]
            row["travel_true"] = round(float(np.hypot(e["end_x"] - e["x"], e["end_y"] - e["y"])), 2)
            row["speed_true"] = q["ball_speed_kmh"]
        else:
            row["speed_true"] = q["shot_speed_kmh"]
        row["length_det"] = d["length_m"]
        row["speed_det"] = d["speed_kmh"]
        rows.append(row)
    matched_t = {j for _, j in pairs}
    matched_d = {i for i, _ in pairs}
    out = {"rows": rows, "n_truth": {k: sum(e["type"] == k for e in truth) for k in KICKS},
           "n_det": {k: sum(d["type"] == k for d in dets) for k in KICKS},
           "kick_found": len(matched_t), "kick_truth": len(truth), "kick_det": len(dets),
           "unmatched_det": [dets[i]["frame"] for i in range(len(dets)) if i not in matched_d],
           "missed_truth": [{"rel": e["rel"], "type": e["type"], "outcome": e["outcome"]} for j, e in enumerate(truth)
                            if j not in matched_t]}
    return out


def wilson(k: int, n: int, z: float = 1.96) -> list | None:
    """95% Wilson interval of a proportion k / n."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(float(c - h), 3), round(float(c + h), 3)]


def summarise(per_clip: list[dict], key: str) -> dict:
    rows = [r for c in per_clip for r in c[key]["rows"]]
    nt = {k: sum(c[key]["n_truth"][k] for c in per_clip) for k in KICKS}
    nd = {k: sum(c[key]["n_det"][k] for c in per_clip) for k in KICKS}
    out = {"kicks": {"truth": sum(c[key]["kick_truth"] for c in per_clip), "detected": sum(c[key]["kick_det"] for c in per_clip),
                     "matched": sum(c[key]["kick_found"] for c in per_clip)}}
    for k in KICKS:
        tp = sum(r["truth_type"] == k and r["det_type"] == k for r in rows)
        out[k] = {"truth": nt[k], "detected": nd[k], "true_positive": tp,
                  "recall": round(tp / nt[k], 3) if nt[k] else None, "recall_ci95": wilson(tp, nt[k]),
                  "precision": round(tp / nd[k], 3) if nd[k] else None, "precision_ci95": wilson(tp, nd[k])}
    passes = [r for r in rows if r["truth_type"] == "pass" and r["det_type"] == "pass"]
    comp = [r for r in passes if r["outcome"] == "complete" and r["lands_in_clip"]]
    shots = [r for r in rows if r["truth_type"] == "shot" and r["det_type"] == "shot"]

    def mae(rs, a, b):
        v = [abs(r[a] - r[b]) for r in rs if r.get(a) is not None and r.get(b) is not None]
        return {"mae": round(float(np.mean(v)), 2) if v else None, "median": round(float(np.median(v)), 2) if v else None,
                "bias": round(float(np.mean([r[a] - r[b] for r in rs if r.get(b) is not None])), 2) if v else None, "n": len(v)}

    out["pass_length_m"] = mae(comp, "length_det", "length_true")
    out["pass_travel_m"] = mae([r for r in passes if r["lands_in_clip"]], "length_det", "travel_true")
    out["ball_speed_kmh"] = mae(passes, "speed_det", "speed_true")
    out["ball_speed_kmh_ground"] = mae([r for r in passes if r["height"] == "ground"], "speed_det", "speed_true")
    out["ball_speed_kmh_lofted"] = mae([r for r in passes if r["height"] == "high"], "speed_det", "speed_true")
    out["shot_speed_kmh"] = mae(shots, "speed_det", "speed_true")
    out["kick_frame_abs_err"] = round(float(np.mean([abs(r["frame_err"]) for r in rows])), 2) if rows else None
    return out


def track_quality(t: dict, obs: dict, P: np.ndarray, w: int, h: int) -> dict:
    """Ball: share of frames seen, ground error while it rolls. Players: share found, ground error, team."""
    from .geometry import ground_at_height
    fr = t["frames"]
    Pt = p_from_clip(t["clip_from_world"][0], w, h)
    z = np.maximum(0.11, fr[:, 23, 0]) + 0.11
    uv_t = project(Pt, np.column_stack([fr[:, 22, 0], fr[:, 22, 1], z]))
    inside = (uv_t[:, 0] >= 0) & (uv_t[:, 0] < w) & (uv_t[:, 1] >= 0) & (uv_t[:, 1] < h)
    errs, px, seen = [], [], 0
    for k, b in enumerate(obs["ball_px"]):
        if b is None:
            continue
        seen += 1
        px.append(float(np.hypot(b[0] - uv_t[k, 0], b[1] - uv_t[k, 1])))
        if z[k] <= BALL_Z + 0.02:
            g = ground_at_height(P, [b], BALL_Z)[0]
            errs.append(float(np.hypot(*(g - fr[k, 22]))))
    perr, found, vis, team_ok = [], 0, 0, 0
    for k in range(0, len(fr), 5):
        feet = project(Pt, np.column_stack([fr[k, :22], np.zeros(22)]))
        on = (feet[:, 0] >= 0) & (feet[:, 0] < w) & (feet[:, 1] >= 0) & (feet[:, 1] < h) & (fr[k, :22, 1] > -1.5) & (fr[k, :22, 1] < 69.5)
        vis += int(on.sum())
        taken = set()
        for p in obs["players"][k]:
            d = np.hypot(fr[k, :22, 0] - p[0], fr[k, :22, 1] - p[1])
            s = int(np.argmin(d))
            if d[s] < 2.0 and s not in taken:
                taken.add(s)
                perr.append(float(d[s]))
                team_ok += int(p[2].split("_")[0] == ("home" if s < 11 else "away"))
        found += len(taken)
    return {"ball_seen": round(seen / len(fr), 3), "ball_in_frame": round(float(inside.mean()), 3),
            "ball_px_median": round(float(np.median(px)), 2) if px else None,
            "ball_px_p95": round(float(np.percentile(px, 95)), 2) if px else None,
            "ball_ground_err_m_median": round(float(np.median(errs)), 3) if errs else None,
            "ball_ground_err_m_p90": round(float(np.percentile(errs, 90)), 3) if errs else None,
            "players_found": round(found / max(vis, 1), 3), "player_err_m_median": round(float(np.median(perr)), 3) if perr else None,
            "player_team_correct": round(team_ok / max(len(perr), 1), 3)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    tmp = Path(tempfile.gettempdir()) / "touchline_vision"
    ap.add_argument("--clips", type=Path, default=tmp / "clips")
    ap.add_argument("--work", type=Path, default=tmp / "run")
    ap.add_argument("--out", type=Path, default=Path("reports/vision"))
    ap.add_argument("--name", default="dev", help="the clip set: dev (tuned on) or heldout (scored once the code was set)")
    args = ap.parse_args(argv)
    per_clip = []
    for d in sorted(p for p in args.clips.iterdir() if (p / "truth.json").exists()):
        work = args.work / d.name
        if not (work / "events.json").exists():
            continue
        info = json.loads((d / "clip.json").read_text(encoding="utf-8"))
        w, h = info["w"], info["h"]
        t = load_truth(d)
        cal = json.loads((work / "calib.json").read_text(encoding="utf-8"))
        obs = json.loads((work / "obs.json").read_text(encoding="utf-8"))
        ev = json.loads((work / "events.json").read_text(encoding="utf-8"))
        Pt = p_from_clip(t["clip_from_world"][0], w, h)
        herr = homography_error(np.array(cal["H"]), h_from_p(Pt), w, h)
        cam_err = float(np.linalg.norm(camera_centre(np.array(cal["P"])) - camera_centre(Pt)))
        n = obs["n"]
        tk = truth_kicks(t, n)
        oobs, oP = oracle_obs(t, w, h)
        oev = detect_events(oobs, oP)
        per_clip.append({"clip": d.name, "match": info["match_id"], "kind": t["clip"]["kind"],
                         "homography": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in herr.items()},
                         "camera_centre_err_m": round(cam_err, 2), "calib_fit": cal["fit"],
                         "tracks": track_quality(t, obs, np.array(cal["P"]), w, h),
                         "vision": grade_kicks(in_window(ev["kicks"], n), tk),
                         "oracle": grade_kicks(in_window(oev["kicks"], n), tk),
                         "ablation_ground_only": grade_kicks(in_window(detect_events(obs, np.array(cal["P"]), air=False)["kicks"], n), tk),
                         "ablation_no_kick_timing": grade_kicks(in_window(detect_events(obs, np.array(cal["P"]), back_time=False)["kicks"], n), tk)})
    H = [c["homography"] for c in per_clip]
    tr = [c["tracks"] for c in per_clip]

    def agg(xs, k, f=np.median):
        v = [x[k] for x in xs if x.get(k) is not None]
        return round(float(f(v)), 3) if v else None

    summary = {"clips": len(per_clip), "matches": sorted({c["match"] for c in per_clip}),
               "homography": {"px_mean_median": agg(H, "px_mean"), "px_mean_max": agg(H, "px_mean", np.max),
                              "m_mean_median": agg(H, "m_mean"), "m_mean_max": agg(H, "m_mean", np.max),
                              "camera_centre_err_m_median": round(float(np.median([c["camera_centre_err_m"] for c in per_clip])), 2)},
               "tracks": {k: agg(tr, k) for k in tr[0]} if tr else {},
               "vision": summarise(per_clip, "vision"), "oracle": summarise(per_clip, "oracle"),
               "ablation_ground_only": summarise(per_clip, "ablation_ground_only"),
               "ablation_no_kick_timing": summarise(per_clip, "ablation_no_kick_timing"),
               "by_match": {m: summarise([c for c in per_clip if c["match"] == m], "vision") for m in sorted({c["match"] for c in per_clip})},
               "match_frames": MATCH_FRAMES}
    out = args.out / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (out / "clips.json").write_text(json.dumps(per_clip, indent=1), encoding="utf-8")
    # The top-level summary holds every set scored so far, by name.
    top = args.out / "summary.json"
    allsets = json.loads(top.read_text(encoding="utf-8")) if top.exists() else {}
    allsets[args.name] = summary
    top.write_text(json.dumps(allsets, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("homography", "tracks")}, indent=1))
    for k in ("vision", "oracle", "ablation_ground_only", "ablation_no_kick_timing"):
        s = summary[k]
        print(k, json.dumps({x: s[x] for x in ("kicks", "pass", "shot")}), json.dumps({x: s[x] for x in ("pass_length_m", "ball_speed_kmh", "shot_speed_kmh")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
