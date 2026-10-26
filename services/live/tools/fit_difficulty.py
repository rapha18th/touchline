"""Fit the tracking-only pass difficulty model and grade it on held-out matches.

    python services/live/tools/fit_difficulty.py data/matches --train 7 10 11 13 --test 8 9 12
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from touchline_live import difficulty as D  # noqa: E402
from touchline_live.match_io import load  # noqa: E402


def grade(model: dict, matches) -> dict:
    w = np.array(model["weights"])
    p, y, t = [], [], []
    for m in matches:
        for e in m.events:
            x = D.features(m, e)
            if x is None:
                continue
            p.append(1 / (1 + np.exp(-x @ w)))
            y.append(1.0 if e["outcome"] == "complete" else 0.0)
            t.append(e["truth"]["p_success"])
    p, y, t = map(np.array, (p, y, t))
    return {
        "passes": int(len(y)),
        "brier": round(float(np.mean((p - y) ** 2)), 4),
        "brier_engine_truth": round(float(np.mean((t - y) ** 2)), 4),
        "brier_base_rate": round(float(np.mean((y.mean() - y) ** 2)), 4),
        "corr_with_engine_truth": round(float(np.corrcoef(p, t)[0, 1]), 4),
        "mean_abs_gap_to_truth": round(float(np.mean(np.abs(p - t))), 4),
        "calibration": D.calibration(p, y),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--train", nargs="+", type=int, required=True)
    ap.add_argument("--test", nargs="+", type=int, required=True)
    a = ap.parse_args()
    load_seed = lambda s: load(Path(a.root) / f"TL-{s:06d}")
    train = [load_seed(s) for s in a.train]
    test = [load_seed(s) for s in a.test]
    model = D.fit(train)
    model["held_out"] = grade(model, test)
    model["train_seeds"], model["test_seeds"] = a.train, a.test
    D.MODEL_PATH.write_text(json.dumps(model, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in model.items() if k != "calibration"}, indent=1))
