"""The Calibrator: a gated agent that tunes the engine's dials against the realism report.

Each round:
  1. propose  a Foundry model (gpt-5-mini, keyless) reads the realism table, the dial table and
              every earlier trial, and proposes the next dial vectors inside bounded ranges
  2. measure  each proposal plays the same seeds (Kaggle CPU kernels; local only for smoke checks)
  3. gate     a proposal replaces the incumbent only if more metrics land in band, or as many land
              while the summed normalised distance to the bands falls, and no metric that was in
              band leaves it. Once all sixteen are in band, the tie breaks on the safety margin:
              how many standard errors each mean sits inside its band (capped at 2).

Every proposal, trial and refusal goes to reports/calibration/trace.jsonl, and summary.json
holds the incumbent and the path to it. The trace is the state: run the command again and it
resumes from the last incumbent.

    python engine/tools/calibrate.py --rounds 6 --k 4 --seeds 1-24
    python engine/tools/calibrate.py --backend local --smoke --out <scratch dir>   # plumbing check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from touchline_engine import realism, sim  # noqa: E402

# name: (low, high, what it moves). The model may only propose values inside these bounds.
BOUNDS = {
    "LOSS": (0.025, 0.06, "value of a possession given away; higher makes risky passes, carries and shots costlier everywhere (fewer shots, safer passing, higher completion, fewer turnovers)"),
    "RESIST_FINAL_THIRD": (0.5, 0.95, "pass success multiplier for targets in the final third; lower means fewer entries, fewer shots and corners, lower pass completion"),
    "RESIST_SHOOTING_RANGE": (0.15, 0.45, "pass and carry success multiplier for targets within 20 m of goal; lower means fewer close-range chances, fewer shots, lower completion"),
    "BOX_RESIST": (0.45, 0.9, "extra pass success multiplier for targets inside the box; lower means fewer balls received in the box"),
    "SHOT_VALUE": (0.5, 1.1, "a shot's worth per unit of xG in the carrier's choice; lower means fewer, better shots (higher xG per shot, more passing)"),
    "SHOT_RANGE": (24.0, 35.0, "the longest shot a carrier considers, metres; lower cuts long-range shots (fewer shots, higher xG per shot)"),
    "HEADER_SHOT": (0.3, 0.7, "chance a won header near goal becomes a shot"),
    "SAVED_SHARE": (0.3, 0.5, "share of unblocked misses the keeper saves; moves shots on target and on-target share, never goals"),
    "RESTART_PACE": (0.9, 1.6, "multiplier on the dead time before throw-ins, goal kicks, corners and free kicks; higher means less ball in play"),
    "SUB_STOP_S": (0.0, 60.0, "seconds each substitution window holds up the restart (about 4 to 5 windows a match)"),
    "LATE_STALL": (0.0, 1.0, "extra share of dead time when the restarting side leads after the hour"),
    "GOAL_STOP_S": (45.0, 90.0, "mean seconds from a goal to the kickoff"),
    "TREATMENT": (0.06, 0.2, "chance a foul stops play for treatment (50 to 140 s each)"),
    "YELLOW_SHARE": (0.12, 0.26, "chance a foul by an unbooked player is booked"),
    "RED_STRAIGHT": (0.0, 0.006, "chance any foul is a straight red"),
    "RED_SECOND": (0.0, 0.05, "chance a foul by a booked player is a second yellow"),
}
assert set(BOUNDS) == set(sim.DIALS)

Z_CAP = 2.0
EPS = 1e-4

INSTRUCTIONS = """You are the Calibrator for a synthetic football match engine. You tune league-level dials so the
engine's realism report lands all sixteen metrics inside their league bands.

You propose dial vectors. Each one is measured over the same seeds as the incumbent, then a gate decides.
The gate accepts a proposal only if more metrics land in band, or the same number land while the summed
normalised distance to the bands falls, and no metric that was in band leaves its band. With all sixteen in
band, it accepts a proposal that raises the safety margin (standard errors inside the band, capped at 2 per
metric). The best accepted proposal of a round becomes the new incumbent. Refusals are information: read them.

How to work:
- Diagnose first: which metrics are out, in which direction, and which dials move them.
- Use the measured history to estimate each dial's effect. Effects interact: fewer shots also means fewer
  goal kicks, corners and goals, so less dead time and more ball in play.
- Watch the metrics near a band edge: a proposal that fixes one metric but pushes an in-band one out is refused.
  standard_errors_inside_band says how safe each metric is (negative means outside); under about 1, match-to-match
  noise alone can push it out, so move it toward the middle of its band when a dial allows.
- Make your proposals in a round differ from each other so they test different hypotheses, for example one
  careful step and one bolder step, or two different dials for the same goal.
- Stay inside the bounds. List only the dials you change from the incumbent.
"""


def compact(summary: dict) -> dict:
    return {k: summary[k]["mean"] for k in realism.TARGETS}


def margin(summary: dict, n: int) -> float:
    """Sum over metrics of how many standard errors the mean sits inside its band, capped at Z_CAP."""
    tot = 0.0
    for k in realism.TARGETS:
        s = summary[k]
        if not s["pass"]:
            continue
        se = max(s["sd"] / math.sqrt(max(n, 1)), 1e-9)
        tot += min(min(s["mean"] - s["low"], s["high"] - s["mean"]) / se, Z_CAP)
    return round(tot, 3)


def assess(result: dict) -> dict:
    n = len(result["rows"])
    sc = realism.score({"summary": result["summary"]})
    return {"in_band": sc["in_band"], "distance": sc["distance"], "margin": margin(result["summary"], n),
            "per_metric": sc["per_metric"], "n": n}


def gate(cand: dict, inc: dict) -> tuple[bool, str]:
    """cand and inc each hold 'summary' and 'assess'. Returns (accepted, reason)."""
    cs, is_ = cand["summary"], inc["summary"]
    left = [k for k in realism.TARGETS if is_[k]["pass"] and not cs[k]["pass"]]
    ca, ia = cand["assess"], inc["assess"]
    if left:
        what = ", ".join(f"{k} {is_[k]['mean']:.3g} -> {cs[k]['mean']:.3g} (band {cs[k]['low']}-{cs[k]['high']})" for k in left)
        return False, f"refused: {len(left)} in-band metric(s) left the band: {what}"
    if ca["in_band"] > ia["in_band"]:
        return True, f"accepted: in band {ia['in_band']} -> {ca['in_band']}"
    if ca["in_band"] < ia["in_band"]:
        return False, f"refused: in band {ia['in_band']} -> {ca['in_band']}"
    if ca["distance"] < ia["distance"] - EPS:
        return True, f"accepted: in band {ca['in_band']} held, distance {ia['distance']:.3f} -> {ca['distance']:.3f}"
    if ca["distance"] > ia["distance"] + EPS:
        return False, f"refused: in band {ca['in_band']} held, distance rose {ia['distance']:.3f} -> {ca['distance']:.3f}"
    if ca["in_band"] == len(realism.TARGETS) and ca["margin"] > ia["margin"] + 0.05:
        return True, f"accepted: all in band, margin {ia['margin']:.2f} -> {ca['margin']:.2f}"
    return False, f"refused: no gain (in band {ca['in_band']}, distance {ca['distance']:.3f}, margin {ia['margin']:.2f} -> {ca['margin']:.2f})"


def better(a: dict, b: dict) -> bool:
    """Is accepted trial a better than accepted trial b?"""
    ka = (a["assess"]["in_band"], -a["assess"]["distance"], a["assess"]["margin"])
    kb = (b["assess"]["in_band"], -b["assess"]["distance"], b["assess"]["margin"])
    return ka > kb


# ----- the trace ---------------------------------------------------------------------------------

class Trace:
    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.path = out / "trace.jsonl"
        self.records = []
        if self.path.exists():
            self.records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def log(self, kind: str, **data) -> dict:
        rec = {"kind": kind, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **data}
        self.records.append(rec)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def trials(self) -> list[dict]:
        return [r for r in self.records if r["kind"] in ("baseline", "trial")]

    def incumbent(self) -> dict | None:
        inc = [r for r in self.records if r["kind"] == "incumbent"]
        if not inc:
            return None
        tid = inc[-1]["trial"]
        return next(r for r in self.trials() if r["trial"] == tid)

    def last_round(self) -> int:
        return max([r.get("round", 0) for r in self.records if r["kind"] == "trial"] or [0])


# ----- measurement -------------------------------------------------------------------------------

def measure_local(vectors: list[dict], seeds: list[int], workers: int, smoke: bool) -> list[dict]:
    """Smoke checks only: at most 2 workers, and with --smoke a single short period per match."""
    out = []
    for v in vectors:
        sim.set_dials(v)
        rows = []
        for seed in seeds:
            m = sim.Match(seed)
            if smoke:
                m.play_period(1, 600, 0)
            else:
                m.play()
            rows.append({"seed": seed, **realism.measure(m)})
        rep = realism.summarise(rows)
        out.append({"dials": v, "summary": rep["summary"], "rows": rows})
    return out


def measure(vectors: list[dict], args, log) -> list[dict | None]:
    if args.backend == "local":
        return measure_local(vectors, args.seed_list, min(args.workers, 2), args.smoke)
    import kaggle_eval
    return kaggle_eval.measure(vectors, args.seed_list, per_kernel=args.per_kernel, label=args.label, log=log)


# ----- the proposer ------------------------------------------------------------------------------

def _models():
    from pydantic import BaseModel, Field

    class Setting(BaseModel):
        name: str = Field(description="dial name, exactly as in the dial table")
        value: float

    class Proposal(BaseModel):
        settings: list[Setting]
        hypothesis: str = Field(description="what this vector should change and why, one or two sentences")

    class Plan(BaseModel):
        diagnosis: str
        proposals: list[Proposal]

    return Plan


def brief(inc: dict, trials: list[dict], k: int) -> str:
    s = inc["summary"]
    z = per_metric_z(inc)
    table = [{"metric": m, "mean": s[m]["mean"], "sd_across_matches": s[m]["sd"], "band": [s[m]["low"], s[m]["high"]],
              "in_band": s[m]["pass"], "distance": inc["assess"]["per_metric"][m],
              "standard_errors_inside_band": round(z[m], 2)} for m in realism.TARGETS]
    dials = [{"name": d, "value": inc["dials"][d], "low": BOUNDS[d][0], "high": BOUNDS[d][1], "effect": BOUNDS[d][2]}
             for d in sim.DIALS]
    history = []
    for t in trials:
        history.append({"trial": t["trial"], "round": t.get("round", 0), "changed_from_its_incumbent": t.get("changed", {}),
                        "in_band": t["assess"]["in_band"], "distance": t["assess"]["distance"],
                        "margin": t["assess"]["margin"], "means": compact(t["summary"]),
                        "verdict": t.get("verdict", "baseline")})
    return json.dumps({"incumbent_trial": inc["trial"], "matches_per_trial": inc["assess"]["n"],
                       "incumbent_in_band": inc["assess"]["in_band"], "incumbent_distance": inc["assess"]["distance"],
                       "incumbent_margin": inc["assess"]["margin"], "realism": table, "dials": dials,
                       "history": history,
                       "ask": f"Propose {k} new dial vectors for the next round."}, indent=0)


def propose(inc: dict, trials: list[dict], k: int, model: str, log) -> tuple[dict, dict]:
    from agent_framework import Agent
    from agent_framework.openai import OpenAIChatClient
    from azure.identity import AzureCliCredential

    Plan = _models()
    endpoint = os.environ.get("TOUCHLINE_AI_ENDPOINT", "https://touchline-ai-rm.openai.azure.com/")
    client = OpenAIChatClient(model=model, azure_endpoint=endpoint, credential=AzureCliCredential())
    agent = Agent(client=client, name="calibrator", instructions=INSTRUCTIONS)
    prompt = brief(inc, trials, k)
    last = None
    for attempt in range(6):
        t0 = time.perf_counter()
        try:
            r = asyncio.run(agent.run(prompt, options={"response_format": Plan}))
            plan = r.value if r.value is not None else Plan.model_validate_json(r.text)
            return plan.model_dump(), {"model": model, "seconds": round(time.perf_counter() - t0, 1),
                                       "prompt_chars": len(prompt)}
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"proposer attempt {attempt + 1} failed: {e}")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"proposer failed: {last}")


def materialise(inc_dials: dict, proposal: dict) -> tuple[dict | None, list[str]]:
    """Apply a proposal to the incumbent. Out-of-bounds values are clipped and noted; unknown dials refuse."""
    vec = dict(inc_dials)
    notes = []
    for s in proposal["settings"]:
        name, val = s["name"], float(s["value"])
        if name not in BOUNDS:
            return None, [f"unknown dial {name}"]
        lo, hi = BOUNDS[name][:2]
        if not lo <= val <= hi:
            notes.append(f"{name} {val} clipped to [{lo}, {hi}]")
            val = min(max(val, lo), hi)
        vec[name] = round(val, 5)
    return vec, notes


def changed(a: dict, b: dict) -> dict:
    return {k: [a[k], b[k]] for k in sim.DIALS if abs(a[k] - b[k]) > 1e-12}


# ----- the loop ----------------------------------------------------------------------------------

def write_summary(trace: Trace, args):
    trials = trace.trials()
    inc = trace.incumbent()
    path = []
    for r in trace.records:
        if r["kind"] == "incumbent":
            t = next(x for x in trials if x["trial"] == r["trial"])
            path.append({"trial": t["trial"], "round": t.get("round", 0), "in_band": t["assess"]["in_band"],
                         "distance": t["assess"]["distance"], "margin": t["assess"]["margin"]})
    refused = [t for t in trials if t.get("verdict", "").startswith("refused")]
    left = [t for t in refused if "left the band" in t.get("reason", "")]
    pre = [r for r in trace.records if r["kind"] == "refusal"]
    summary = {
        "seeds": args.seeds, "model": args.model,
        "rounds": trace.last_round(),
        "trials": len([t for t in trials if t["kind"] == "trial"]),
        "accepted": len([t for t in trials if t.get("verdict", "").startswith("accepted")]),
        "promoted": len(path) - 1,
        "refused_measured": len(refused),
        "refused_for_leaving_band": len(left),
        "refused_before_measuring": len(pre),
        "baseline": {k: trials[0]["assess"][k] for k in ("in_band", "distance", "margin")} if trials else None,
        "incumbent": None if inc is None else {
            "trial": inc["trial"], "dials": inc["dials"], "in_band": inc["assess"]["in_band"],
            "distance": inc["assess"]["distance"], "margin": inc["assess"]["margin"], "means": compact(inc["summary"]),
            "changed_from_defaults": changed(trials[0]["dials"], inc["dials"])},
        "path": path,
    }
    (trace.out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def record_trial(trace: Trace, rnd: int, tid: int, res: dict, inc: dict | None, hypothesis: str, kind="trial") -> dict:
    a = assess(res)
    rec = {"round": rnd, "trial": tid, "dials": res["dials"], "summary": res["summary"], "assess": a,
           "hypothesis": hypothesis, "wall_s": res.get("wall_s"), "kernel": res.get("kernel")}
    if inc is not None:
        rec["changed"] = changed(inc["dials"], res["dials"])
        ok, why = gate(rec, inc)
        rec["verdict"], rec["reason"] = ("accepted" if ok else "refused"), why
    return trace.log(kind, **rec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--k", type=int, default=4, help="proposals per round")
    ap.add_argument("--seeds", default="1-24")
    ap.add_argument("--backend", choices=["kaggle", "local"], default="kaggle")
    ap.add_argument("--per-kernel", type=int, default=2)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--smoke", action="store_true", help="local backend: one short period per match")
    ap.add_argument("--model", default=os.environ.get("TOUCHLINE_DEEP_MODEL", "gpt-5-mini"))
    ap.add_argument("--baseline-from", default=None, help="kaggle_eval --out JSON holding the baseline result")
    ap.add_argument("--out", default=str(ROOT / "reports" / "calibration"))
    ap.add_argument("--label", default="calibrate")
    args = ap.parse_args()
    import kaggle_eval
    args.seed_list = kaggle_eval.parse_seeds(args.seeds)
    trace = Trace(Path(args.out))

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    if not trace.trials():
        if args.baseline_from:
            res = json.loads(Path(args.baseline_from).read_text(encoding="utf-8"))[0]
        else:
            log("measuring the baseline")
            res = measure([sim.dials()], args, log)[0]
        if res is None:
            raise SystemExit("baseline failed")
        if [r["seed"] for r in res["rows"]] != args.seed_list:
            raise SystemExit("baseline seeds differ from --seeds")
        res["dials"] = {**sim.dials(), **res["dials"]}
        rec = record_trial(trace, 0, 0, res, None, "engine defaults", kind="baseline")
        trace.log("incumbent", trial=0, reason="baseline")
        log(f"baseline: {rec['assess']['in_band']}/16 in band, distance {rec['assess']['distance']}")

    for rnd in range(trace.last_round() + 1, args.rounds + 1):
        inc = trace.incumbent()
        if inc["assess"]["in_band"] == len(realism.TARGETS) and all(
                v >= 1.0 for v in per_metric_z(inc).values()):
            log("all sixteen in band with at least one standard error of margin each: done")
            break
        trials = trace.trials()
        plan, meta = propose(inc, trials, args.k, args.model, log)
        trace.log("proposal", round=rnd, incumbent=inc["trial"], diagnosis=plan["diagnosis"],
                  proposals=plan["proposals"], **meta)
        log(f"round {rnd}: {len(plan['proposals'])} proposals in {meta['seconds']} s. {plan['diagnosis'][:200]}")
        tried = [t["dials"] for t in trials]
        vecs, hyps = [], []
        for p in plan["proposals"][:args.k]:
            vec, notes = materialise(inc["dials"], p)
            if vec is None:
                trace.log("refusal", round=rnd, proposal=p, reason="refused before measuring: " + "; ".join(notes))
                continue
            if notes:
                trace.log("clip", round=rnd, proposal=p, notes=notes)
            if any(all(abs(vec[d] - t[d]) < 1e-9 for d in sim.DIALS) for t in tried + vecs):
                trace.log("refusal", round=rnd, proposal=p, reason="refused before measuring: duplicate of an earlier trial")
                continue
            vecs.append(vec)
            hyps.append(p["hypothesis"])
        if not vecs:
            log(f"round {rnd}: nothing new to measure")
            continue
        results = measure(vecs, args, log)
        next_id = max(t["trial"] for t in trace.trials()) + 1
        best = None
        for res, vec, hyp in zip(results, vecs, hyps):
            if res is None:
                trace.log("refusal", round=rnd, dials=vec, hypothesis=hyp, reason="measurement failed")
                continue
            res["dials"] = vec
            rec = record_trial(trace, rnd, next_id, res, inc, hyp)
            next_id += 1
            log(f"  trial {rec['trial']}: {rec['assess']['in_band']}/16, distance {rec['assess']['distance']:.3f}, "
                f"margin {rec['assess']['margin']:.2f}  {rec['reason']}")
            if rec["verdict"] == "accepted" and (best is None or better(rec, best)):
                best = rec
        if best is not None:
            trace.log("incumbent", round=rnd, trial=best["trial"], reason=best["reason"])
            log(f"round {rnd}: trial {best['trial']} is the new incumbent")
        else:
            trace.log("hold", round=rnd, trial=inc["trial"], reason="every proposal refused")
            log(f"round {rnd}: every proposal refused; incumbent holds")
        write_summary(trace, args)
    s = write_summary(trace, args)
    log(f"incumbent trial {s['incumbent']['trial']}: {s['incumbent']['in_band']}/16, distance {s['incumbent']['distance']}")


def per_metric_z(t: dict) -> dict:
    n = t["assess"]["n"]
    out = {}
    for k in realism.TARGETS:
        s = t["summary"][k]
        se = max(s["sd"] / math.sqrt(max(n, 1)), 1e-9)
        out[k] = min(s["mean"] - s["low"], s["high"] - s["mean"]) / se
    return out


if __name__ == "__main__":
    main()
