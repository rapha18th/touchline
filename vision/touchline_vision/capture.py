"""Film clips of generated matches through the fan app's own 3D pitch.

    python -m touchline_vision.capture --matches TL-000012 TL-000013 --shots 5 --passes 5

Starts the web service on a local port with the crew off, opens web/capture.html in headless Chrome,
feeds each clip's 25 Hz tracking in, steps it frame by frame and writes 1280x720 JPEG frames to
%TEMP%/touchline_vision/clips/<clip>/. The renderer's camera matrix goes into truth.json for grading.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import clips as C

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("TOUCHLINE_DATA", ROOT / "data" / "matches"))
OUT = Path(os.environ.get("TOUCHLINE_VISION_OUT", Path(tempfile.gettempdir()) / "touchline_vision"))


@contextmanager
def serve(port: int):
    """The Touchline web service on localhost, crew off; stopped on exit."""
    env = dict(os.environ, TOUCHLINE_CREW="0", TOUCHLINE_DATA=str(DATA), TOUCHLINE_WEB=str(ROOT / "web"))
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "engine"), str(ROOT / "services" / "live"), env.get("PYTHONPATH", "")])
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "touchline_live.server:app", "--app-dir",
                             str(ROOT / "services" / "live"), "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning"], env=env)
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("web service did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def film(page, base: str, clip: C.Clip, meta: dict, frames_cm: np.ndarray, events: list[dict],
         w: int, h: int, quality: float, out: Path) -> dict:
    d = out / clip.id
    (d / "frames").mkdir(parents=True, exist_ok=True)
    seg = frames_cm[clip.f0:clip.f1].astype(np.float32) / 100.0
    n = page.evaluate("f => capture.load(f)", [round(float(v), 3) for v in seg.reshape(-1)])
    page.evaluate("c => capture.camera(c)", clip.camera)
    mats = []
    t0 = time.perf_counter()
    for i in range(int(n)):
        r = page.evaluate("([i, q]) => capture.frame(i, q)", [i, quality])
        (d / "frames" / f"{i:05d}.jpg").write_bytes(base64.b64decode(r["image"].split(",", 1)[1]))
        mats.append([round(v, 9) for v in r["clip_from_world"]])
    (d / "clip.json").write_text(json.dumps(C.clip_json(clip, meta, w, h)), encoding="utf-8")
    C.save_truth(d, clip, events, frames_cm, mats)
    return {"clip": clip.id, "frames": int(n), "seconds": round(time.perf_counter() - t0, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--matches", nargs="+", required=True)
    ap.add_argument("--shots", type=int, default=5, help="shot clips per match")
    ap.add_argument("--passes", type=int, default=5, help="passing clips per match")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--quality", type=float, default=0.92, help="JPEG quality")
    ap.add_argument("--seed", type=int, default=7, help="camera jitter")
    ap.add_argument("--out", type=Path, default=OUT / "clips")
    ap.add_argument("--limit-frames", type=int, default=0, help="smoke test: cut every clip to this many frames")
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    log = []
    with serve(args.port) as base, sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True, args=["--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": args.w, "height": args.h}, device_scale_factor=1)
        page.on("console", lambda m: m.type == "error" and print("[page]", m.text, file=sys.stderr))
        for mid in args.matches:
            page.goto(f"{base}/web/capture.html?w={args.w}&h={args.h}")
            page.wait_for_function("window.capture && window.capture.ready", timeout=60000)
            meta, events, frames_cm, pf = C.load_match(DATA / mid)
            page.evaluate("id => capture.init(id)", mid)
            rng = np.random.default_rng([args.seed, int(mid.split("-")[1])])
            for clip in C.pick(events, pf, args.shots, args.passes, prefix=mid.replace("TL-", "m")):
                if args.limit_frames:
                    clip.f1 = clip.f0 + args.limit_frames
                clip.camera = C.choose_camera(C.ball_track(frames_cm[clip.f0:clip.f1]), rng, args.w, args.h)
                r = film(page, base, clip, meta, frames_cm, events, args.w, args.h, args.quality, args.out)
                log.append(r)
                print(json.dumps(r), flush=True)
            del frames_cm
        browser.close()
    (args.out / "capture_log.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
