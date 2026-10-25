"""What the Match Director, its settings and the pruned search buy, measured.

Eight generic stories, each with beats written by hand before any run (the gold beats). Every story
goes through four arms on the same seeds and the same budget of three full matches of play:

    director    the Match Director's spec (gpt-5.4-mini, gated), pruned search
    rules       the keyword Director's spec, pruned search (the baseline with no model)
    untouched   the Director's beats with the clubs left as drawn: no settings, pruned search
    no_prune    the Director's spec, every candidate plays to the end

Each arm offers its best match by its own beats; the bench then checks that match against the gold
beats with the same validator. It also scores how well each Director read the story: a gold beat
counts as read when the spec has a beat of the same kind for the same side.

    python -m touchline_live.eval.scenario_bench --out reports/scenario_bench.json --workers 3

The run is resumable: the Director specs and every finished arm are saved to the report as they land,
and a rerun skips them. With one worker the search is deterministic; with more, which candidate ends
the search first can depend on timing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

from .. import scenario as S
from ..scenario import Beat, ScenarioSpec, SideSettings

ROOT = Path(__file__).resolve().parents[4]

B = Beat
STORIES = [
    ("an underdog parks the bus and nicks it late",
     [B(kind="winner", side="away"), B(kind="late_goal", side="away", minute=75)]),
    ("a wild end-to-end draw with a red card",
     [B(kind="winner", side="draw"), B(kind="total_goals", at_least=4), B(kind="red_card", at_least=1)]),
    ("the home side fall behind and come back to win",
     [B(kind="comeback", side="home")]),
    ("a tight, cagey game settled by a single goal",
     [B(kind="total_goals", at_least=1, at_most=1)]),
    ("the favourites dominate at home and win with a clean sheet",
     [B(kind="winner", side="home"), B(kind="clean_sheet", side="home"), B(kind="shots_diff", side="home", at_least=6)]),
    ("a goalless draw where both defences win every battle",
     [B(kind="total_goals", at_most=0)]),
    ("the visitors score first, go down to ten men and hang on to win",
     [B(kind="first_goal", side="away"), B(kind="red_card", side="away", at_least=1), B(kind="winner", side="away")]),
    ("a late winner for the home side in a game of few chances",
     [B(kind="winner", side="home"), B(kind="late_goal", side="home", minute=75), B(kind="total_goals", at_most=3)]),
]
ARMS = ("director", "rules", "untouched", "no_prune")


def read_score(spec_beats: list[Beat], gold: list[Beat]) -> dict:
    """Did the Director read the story? Gold beats matched by kind and side, and extra beats."""
    key = lambda b: (b.kind, b.side)
    got = {key(b) for b in spec_beats}
    want = {key(b) for b in gold}
    return {"gold": len(want), "read": len(want & got), "extra": len(got - want)}


def untouched(spec: ScenarioSpec) -> ScenarioSpec:
    return ScenarioSpec(home=SideSettings(), away=SideSettings(), beats=spec.beats)


def run_arm(arm: str, spec: ScenarioSpec, gold: list[Beat], *, base: int, seeds: int, workers: int, work: Path,
            period_s: float | None) -> dict:
    stage = work / f"{arm}-{base}"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        res = S.search(spec, stage=stage, seeds=seeds, workers=workers, prune=arm != "no_prune",
                       base_seed=base, period_s=period_s)
        best = res["best"]
        events = json.loads((stage / best["match_id"] / "events.json").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    g = S.validate(events, gold)
    return {"arm": arm, "seed": best["seed"], "score": best["score"],
            "own": {"met": best["met"], "total": best["total"]},
            "gold": {"met": g["met"], "total": g["total"], "all": g["all"],
                     "beats": [{k: r[k] for k in ("beat", "ok", "detail")} for r in g["beats"]]},
            "candidates": len(res["candidates"]), "pruned": sum(1 for c in res["candidates"] if c.get("pruned_at")),
            "played_min": round(res["played_s"] / 60, 1), "wall_s": res["wall_s"], "spent": res["spent"]}


def summarise(rep: dict) -> dict:
    out = {}
    for arm in ARMS:
        rows = [s["arms"][arm] for s in rep["stories"] if arm in s.get("arms", {})]
        if not rows:
            continue
        out[arm] = {
            "stories": len(rows),
            "gold_met": sum(r["gold"]["met"] for r in rows), "gold_total": sum(r["gold"]["total"] for r in rows),
            "stories_all_gold": sum(r["gold"]["all"] for r in rows),
            "own_met": sum(r["own"]["met"] for r in rows), "own_total": sum(r["own"]["total"] for r in rows),
            "candidates": sum(r["candidates"] for r in rows), "pruned": sum(r["pruned"] for r in rows),
            "played_min": round(sum(r["played_min"] for r in rows), 1),
            "wall_s": round(sum(r["wall_s"] for r in rows), 1),
        }
    for who in ("director", "rules"):
        rs = [s["read"][who] for s in rep["stories"] if who in s.get("read", {})]
        if rs:
            out[f"read_{who}"] = {k: sum(r[k] for r in rs) for k in ("gold", "read", "extra")}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="touchline_live.eval.scenario_bench")
    ap.add_argument("--out", default=str(ROOT / "reports" / "scenario_bench.json"))
    ap.add_argument("--work", default=str(ROOT / "data" / "eval" / "scenario"))
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--base-seed", type=int, default=950_000)
    ap.add_argument("--stories", type=int, default=len(STORIES))
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--period-s", type=float, default=None, help="short periods for a quick check; the report uses full matches")
    a = ap.parse_args()
    out, work = Path(a.out), Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    rep = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    rep.setdefault("stories", [])
    rep["config"] = {"seeds": a.seeds, "workers": a.workers, "base_seed": a.base_seed, "period_s": a.period_s,
                     "command": "python -m touchline_live.eval.scenario_bench --out reports/scenario_bench.json --workers 3"}

    def save():
        rep["summary"] = summarise(rep)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")

    for i, (story, gold) in enumerate(STORIES[: a.stories]):
        if i >= len(rep["stories"]):
            rep["stories"].append({"story": story, "gold": [S.describe(b) for b in gold], "arms": {}})
        row = rep["stories"][i]
        if "spec" not in row:
            spec, trace = asyncio.run(S.direct(story))
            rules, _ = S.gate(S.rules_director(story))
            row.update(spec=spec.model_dump(), trace=trace, rules=rules.model_dump(),
                       beats={"director": [S.describe(b) for b in spec.beats], "rules": [S.describe(b) for b in rules.beats]})
            save()
        spec = ScenarioSpec.model_validate(row["spec"])
        rules = ScenarioSpec.model_validate(row["rules"])
        row["read"] = {"director": read_score(spec.beats, gold), "rules": read_score(rules.beats, gold)}
        base = a.base_seed + 100 * i
        for arm in a.arms.split(","):
            if arm in row["arms"]:
                continue
            arm_spec = {"director": spec, "rules": rules, "untouched": untouched(spec), "no_prune": spec}[arm]
            t0 = time.perf_counter()
            r = run_arm(arm, arm_spec, gold, base=base, seeds=a.seeds, workers=a.workers, work=work, period_s=a.period_s)
            row["arms"][arm] = r
            save()
            print(f"{i} {arm:9s} gold {r['gold']['met']}/{r['gold']['total']} own {r['own']['met']}/{r['own']['total']} "
                  f"{r['candidates']} cand {r['played_min']} min {time.perf_counter() - t0:.0f} s  {story}", flush=True)
    save()
    print(json.dumps(rep["summary"], indent=1))


if __name__ == "__main__":
    main()
