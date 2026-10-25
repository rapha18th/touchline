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

With --tilt N it also plays N single full matches per story with no search, on the same seeds for three
settings (the Director's, the keyword Director's, none) and checks the gold beats on each. That isolates
what the settings alone do to the odds.

    python -m touchline_live.eval.scenario_bench --out reports/scenario_bench.json --workers 3 --tilt 4

The calibrated Director (the shipped prompt) ran its arm again on the same seeds:

    python -m touchline_live.eval.scenario_bench --out reports/scenario_bench_v2.json --arms director --tilt 4 --tilt-arms director --workers 3
    python -m touchline_live.eval.scenario_bench --director-check 5          the Director alone on the app's examples
    python -m touchline_live.eval.scenario_bench --tables                    every table in docs/SCENARIO.md

The run is resumable: the Director specs and every finished arm are saved to the report as they land,
and a rerun skips them. With one worker the search is deterministic; with more, which candidate ends
the search first can depend on timing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

from collections import Counter

from touchline_engine.sim import Match as EngineMatch

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


def _exact(b: Beat) -> tuple:
    return (b.kind, b.side, (b.at_least or 1) if b.kind == "red_card" else b.at_least, b.at_most,
            b.minute if b.kind == "late_goal" else None)


def read_score(spec_beats: list[Beat], gold: list[Beat]) -> dict:
    """Did the Director read the story?

    read:  gold beats matched by kind and side only; numbers and minutes are not compared, so "at least 3
           goals" reads gold "at least 4 goals".
    exact: gold beats matched by kind, side, bounds and minute.
    extra: beats the story never asked for (by kind and side).
    """
    key = lambda b: (b.kind, b.side)
    got = {key(b) for b in spec_beats}
    want = {key(b) for b in gold}
    exact = len({_exact(b) for b in gold} & {_exact(b) for b in spec_beats})
    return {"gold": len(want), "read": len(want & got), "exact": exact, "extra": len(got - want)}


def provenance() -> dict:
    """Which prompt and which commit made a report."""
    return {"prompt_sha256": S.prompt_sha256(), "git_sha": S.git_sha()}


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
            "played_min": round(res["played_s"] / 60, 1), "wall_s": res["wall_s"], "spent": res["spent"],
            # Play the single-worker search counts, and the seeds spare workers played ahead of it and dropped.
            "counted_min": round(sum(c.get("played_s") or 0.0 for c in res["candidates"]) / 60, 1),
            "ahead": len(res.get("ahead", []))}


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
            out[f"read_{who}"] = {k: sum(r.get(k, 0) for r in rs) for k in ("gold", "read", "exact", "extra")}
    return out


TILT_ARMS = ("director", "rules", "untouched")


def tilt(rep: dict, *, seeds: int, workers: int, base_seed: int, period_s: float | None, save,
         arms: tuple[str, ...] = TILT_ARMS) -> None:
    """Do the settings make the story more likely? Single matches, no search, the same seeds for every arm.

    Each story's gold beats are checked on `seeds` full matches played with the Director's settings, the
    keyword Director's settings and no settings at all. Paired by seed, so the clubs are the same clubs.
    """
    import multiprocessing as mp

    rep.setdefault("tilt", {})
    runs = rep.setdefault("tilt_runs", {})                  # one entry per match, so a rerun resumes match by match
    want: dict[str, list[str]] = {}
    todo = []
    for i, row in enumerate(rep["stories"]):
        if "spec" not in row:
            continue
        gold = STORIES[i][1]
        settings = {"director": ScenarioSpec.model_validate(row["spec"]), "rules": ScenarioSpec.model_validate(row["rules"]),
                    "untouched": ScenarioSpec()}
        for arm in arms:
            key = f"{i}:{arm}"
            spec = ScenarioSpec(home=settings[arm].home, away=settings[arm].away, beats=gold).model_dump_json()
            want[key] = []
            for k in range(seeds):
                seed = base_seed + 1000 * i + k
                rk = f"{key}:{seed}"
                want[key].append(rk)
                if rk not in runs:
                    todo.append((rk, (seed, spec, False, None, None, period_s)))

    def fold():
        for key, rks in want.items():
            if all(rk in runs for rk in rks):
                res = [runs[rk] for rk in rks]
                rep["tilt"][key] = {"seeds": [r["seed"] for r in res], "scores": [r["score"] for r in res],
                                    "met": [r["met"] for r in res], "total": res[0]["total"],
                                    "all": sum(r["all"] for r in res), "beats": [r["beats"] for r in res],
                                    "goals": sum(sum(r["score"]) for r in res) / len(res)}

    if todo:
        t0 = time.perf_counter()
        with mp.get_context("spawn").Pool(workers) as pool:
            jobs = {rk: pool.apply_async(S.play_candidate, args) for rk, args in todo}
            for n, (rk, job) in enumerate(jobs.items(), 1):
                r = job.get()
                runs[rk] = {"seed": r["seed"], "score": r["score"], "met": r["met"], "total": r["total"],
                            "all": r["all"], "beats": [b["ok"] for b in r["beats"]]}
                fold()
                save()
                print(f"tilt {rk:20s} {r['met']}/{r['total']} {r['score']}  {n}/{len(todo)} {time.perf_counter() - t0:.0f} s", flush=True)
    fold()
    save()


def tilt_summary(rep: dict) -> dict:
    out = {}
    for arm in TILT_ARMS:
        rows = [v for k, v in rep.get("tilt", {}).items() if k.endswith(f":{arm}")]
        if rows:
            out[arm] = {"stories": len(rows), "matches": sum(len(r["met"]) for r in rows),
                        "beats_met": sum(sum(r["met"]) for r in rows),
                        "beats_checked": sum(r["total"] * len(r["met"]) for r in rows),
                        "matches_all_gold": sum(r["all"] for r in rows),
                        "goals_per_match": round(sum(r["goals"] for r in rows) / len(rows), 2)}
    return out


DIAL_SETTINGS = {
    "high": {"tempo": 0.85, "risk": 0.85, "press": 0.75, "directness": 0.75},
    "low": {"tempo": 0.3, "risk": 0.2},
    "none": {},
}


LONG_BALL_M = 30.0
PROBE_METRICS = ("goals", "shots_a_side", "passes_a_side", "pass_completion", "long_ball_share",
                 "pressures_a_side", "tackles_a_side")


def probe_match(seed: int, spec_json: str) -> dict:
    """One full match with the probe's settings, and the numbers that say how it looked (a worker process)."""
    spec = ScenarioSpec.model_validate_json(spec_json)
    home, away = S.teams_for(seed, spec)
    m = EngineMatch(seed, home, away).play()
    ev = m.events
    passes = [e for e in ev if e["type"] == "pass"]
    count = lambda typ: sum(1 for e in ev if e["type"] == typ) / 2
    return {"score": list(m.score), "red": any(e["type"] == "card" and e.get("outcome") == "red" for e in ev),
            "goals": sum(m.score), "shots_a_side": count("shot"), "passes_a_side": len(passes) / 2,
            "pass_completion": round(sum(e.get("outcome") == "complete" for e in passes) / max(1, len(passes)), 4),
            "long_ball_share": round(sum((e.get("q") or {}).get("length_m", 0) >= LONG_BALL_M for e in passes)
                                     / max(1, len(passes)), 4),
            "pressures_a_side": count("pressure"), "tackles_a_side": count("tackle")}


def _mean_se(xs: list[float]) -> dict:
    n = len(xs)
    mean = sum(xs) / n
    sd = (sum((x - mean) ** 2 for x in xs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"mean": round(mean, 4), "se": round(sd / n ** 0.5, 4)}


def dial_probe(path: Path, n: int, workers: int, base_seed: int = 990_000) -> None:
    """How far the dials move the engine: n full matches on the same seeds with both sides' dials high, low and
    as drawn. Goals, red cards and the look of the match (shots, passes, completion, long balls, pressures,
    tackles), each a mean with its standard error. The Director's prompt quotes this file."""
    import multiprocessing as mp

    out = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    runs = out.setdefault("runs", {})
    out.update(settings=DIAL_SETTINGS, matches_per_setting=n, long_ball_m=LONG_BALL_M, **provenance(),
               command=f"python -m touchline_live.eval.scenario_bench --dial-probe {n} --dial-out {path.name} --workers {workers}")
    todo = []
    for name, dials in DIAL_SETTINGS.items():
        side = SideSettings(style=dials)
        spec = ScenarioSpec(home=side, away=side).model_dump_json()
        for k in range(n):
            if f"{name}:{base_seed + k}" not in runs:
                todo.append((f"{name}:{base_seed + k}", (base_seed + k, spec)))

    def save():
        summary = {}
        for name in DIAL_SETTINGS:
            rs = [v for k, v in runs.items() if k.startswith(f"{name}:")]
            if rs:
                summary[name] = {"matches": len(rs), "goals_per_match": round(sum(sum(r["score"]) for r in rs) / len(rs), 2),
                                 "matches_with_a_red": sum(r["red"] for r in rs)}
                if all("shots_a_side" in r for r in rs):
                    summary[name].update({k: _mean_se([r[k] for r in rs]) for k in PROBE_METRICS})
        out["summary"] = summary
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    if todo:
        with mp.get_context("spawn").Pool(workers, initializer=S._init_worker, initargs=(None,)) as pool:
            jobs = {k: pool.apply_async(probe_match, args) for k, args in todo}
            for k, job in jobs.items():
                runs[k] = job.get()
                save()
                print(f"dials {k} {runs[k]['score']}", flush=True)
    save()
    print(json.dumps(out["summary"], indent=1))


# The app's four examples in the product's other languages (the keyword reader reads English only).
EXAMPLES_I18N = {
    "es": ("Un equipo modesto aparca el autobús y gana al final",
           "Un empate loco de ida y vuelta con una tarjeta roja",
           "Los locales van perdiendo y remontan para ganar",
           "Un partido cerrado y táctico que se decide por un solo gol"),
    "fr": ("Un petit poucet gare le bus et l'emporte sur le tard",
           "Un match nul fou, d'un but à l'autre, avec un carton rouge",
           "L'équipe à domicile est menée puis renverse le match pour gagner",
           "Un match serré et prudent, décidé par un seul but"),
    "pt": ("Um azarão estaciona o ônibus e vence no fim",
           "Um empate maluco, lá e cá, com um cartão vermelho",
           "O time da casa sai perdendo e vira para vencer",
           "Um jogo truncado e cauteloso decidido por um único gol"),
}


def _director_row(story: str) -> dict:
    spec, trace = asyncio.run(S.direct(story))
    gates = [t for t in trace if t["role"] == "gate"]
    return {"beats": [S.describe(b) for b in spec.beats],
            "refused_first": sum(n_["action"] == "refuse" for n_ in gates[0]["notes"]) if gates else 0,
            "repaired": any(t.get("repair") for t in trace),
            "fallback": any(t.get("fallback") for t in trace),
            "ms": sum(t.get("ms", 0) for t in trace if t["role"] == "director")}


def director_check(path: Path, n: int, langs: tuple[str, ...] = ("es", "fr", "pt")) -> None:
    """The Director alone, n calls on each of the app's example stories: beats, gate refusals, repairs, fallbacks.
    The same four stories in Spanish, French and Portuguese go through the same calls.

    It writes its own file (reports/scenario_director.json), so it can run beside the match bench. Calls made
    under an older prompt are dropped, so the file always describes the prompt that ships."""
    out = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if out.get("prompt_sha256") != S.prompt_sha256():
        out = {}
    out.setdefault("stories", {})
    out.setdefault("languages", {})
    out.update(calls_per_story=n, **provenance(),
               command=f"python -m touchline_live.eval.scenario_bench --director-check {n}")

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")

    for story in S.EXAMPLES:
        rows = out["stories"].setdefault(story, [])
        while len(rows) < n:
            rows.append(_director_row(story))
            save()
        print(f"director {story}: " + " | ".join(", ".join(r["beats"]) for r in rows), flush=True)
    for lang in langs:
        for en, story in zip(S.EXAMPLES, EXAMPLES_I18N[lang]):
            entry = out["languages"].setdefault(lang, {}).setdefault(en, {"story": story, "calls": []})
            while len(entry["calls"]) < n:
                entry["calls"].append(_director_row(story))
                save()
            print(f"director {lang} {story}: " + " | ".join(", ".join(r["beats"]) for r in entry["calls"]), flush=True)


def tables(rep: dict, director_path: Path | None = None) -> str:
    """The tables docs/SCENARIO.md prints, from the saved reports alone."""
    s = rep["summary"]
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "n/a"
    out = ["| arm | gold beats met | stories with every gold beat | own beats met | candidates (pruned) | match minutes played | wall s |",
           "|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        if arm in s:
            r = s[arm]
            out.append(f"| {arm} | {r['gold_met']} of {r['gold_total']} ({pct(r['gold_met'], r['gold_total'])}) | "
                       f"{r['stories_all_gold']} of {r['stories']} | {r['own_met']} of {r['own_total']} | "
                       f"{r['candidates']} ({r['pruned']}) | {r['played_min']:.0f} | {r['wall_s']:.0f} |")
    gold_of = {story: gold for story, gold in STORIES}
    out += ["", "| Director | gold beats read (kind and side) | read exactly (bounds and minute too) | extra beats |",
            "|---|---|---|---|"]
    for who, key in (("director", "spec"), ("rules", "rules")):
        rs = [read_score(ScenarioSpec.model_validate(row[key]).beats, gold_of[row["story"]])
              for row in rep["stories"] if key in row and row["story"] in gold_of]
        if rs:
            t = {k: sum(r[k] for r in rs) for k in ("gold", "read", "exact", "extra")}
            out.append(f"| {who} | {t['read']} of {t['gold']} | {t['exact']} of {t['gold']} | {t['extra']} |")
    t = rep.get("tilt_summary")
    if t:
        out += ["", "| settings | single matches | gold beats met | matches with every gold beat | goals a match |", "|---|---|---|---|---|"]
        for arm in TILT_ARMS:
            if arm in t:
                r = t[arm]
                out.append(f"| {arm} | {r['matches']} | {r['beats_met']} of {r['beats_checked']} ({pct(r['beats_met'], r['beats_checked'])}) | "
                           f"{r['matches_all_gold']} of {r['matches']} | {r['goals_per_match']} |")
    if rep.get("tilt"):
        arms = [a for a in TILT_ARMS if any(k.endswith(f":{a}") for k in rep["tilt"])]
        out += ["", "| story (single matches) | " + " | ".join(f"{a}: gold met, goals a match" for a in arms) + " |",
                "|---|" + "---|" * len(arms)]
        for i, row in enumerate(rep["stories"]):
            cells = []
            for a in arms:
                t = rep["tilt"].get(f"{i}:{a}")
                cells.append("" if not t else f"{sum(t['met'])} of {t['total'] * len(t['met'])}, {t['goals']:.1f}")
            out.append(f"| {row['story']} | " + " | ".join(cells) + " |")
    out += ["", "| story | gold | director | rules | untouched | no_prune |", "|---|---|---|---|---|---|"]
    for row in rep["stories"]:
        cell = lambda a: (f"{row['arms'][a]['gold']['met']}/{row['arms'][a]['gold']['total']}" if a in row.get("arms", {}) else "")
        out.append(f"| {row['story']} | {', '.join(row['gold'])} | " + " | ".join(cell(a) for a in ARMS) + " |")
    if director_path and director_path.exists():
        d = json.loads(director_path.read_text(encoding="utf-8"))
        out += ["", "| example story | calls | first spec refused by the gate | repaired | fell back to rules | beats, most common |",
                "|---|---|---|---|---|---|"]
        for story, rows in d["stories"].items():
            common = max({tuple(r["beats"]) for r in rows}, key=lambda b: sum(tuple(r["beats"]) == b for r in rows))
            out.append(f"| {story} | {len(rows)} | {sum(r['refused_first'] > 0 for r in rows)} | {sum(r['repaired'] for r in rows)} | "
                       f"{sum(r['fallback'] for r in rows)} | {', '.join(common)} ({sum(tuple(r['beats']) == common for r in rows)} of {len(rows)}) |")
        if d.get("languages"):
            english = {story: Counter(frozenset(r["beats"]) for r in rows).most_common(1)[0][0] for story, rows in d["stories"].items()}
            out += ["", "| example story | language | calls | fell back to rules | same beats as the English, most common (any order) | beats, most common |",
                    "|---|---|---|---|---|---|"]
            for lang, stories in d["languages"].items():
                for en, entry in stories.items():
                    calls = entry["calls"]
                    common, k = Counter(tuple(r["beats"]) for r in calls).most_common(1)[0]
                    same = sum(frozenset(r["beats"]) == english.get(en) for r in calls)
                    out.append(f"| {entry['story']} | {lang} | {len(calls)} | {sum(r['fallback'] for r in calls)} | "
                               f"{same} of {len(calls)} | {', '.join(common)} ({k} of {len(calls)}) |")
    return "\n".join(out)


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
    ap.add_argument("--tilt", type=int, default=0, help="also play this many single matches per story and arm (no search)")
    ap.add_argument("--tilt-arms", default=",".join(TILT_ARMS), help="which settings the tilt check plays")
    ap.add_argument("--tables", action="store_true", help="print the tables from the saved report and stop")
    ap.add_argument("--director-check", type=int, default=0, help="only call the Director this many times per example story")
    ap.add_argument("--director-out", default=str(ROOT / "reports" / "scenario_director.json"))
    ap.add_argument("--dial-probe", type=int, default=0, help="only play this many matches per dial setting")
    ap.add_argument("--dial-out", default="scenario_dials.json", help="the dial probe's file under reports/")
    a = ap.parse_args()
    out, work = Path(a.out), Path(a.work)
    if a.dial_probe:
        dial_probe(ROOT / "reports" / a.dial_out, a.dial_probe, a.workers)
        return
    if a.director_check:
        director_check(Path(a.director_out), a.director_check)
        return
    if a.tables:
        print(tables(json.loads(out.read_text(encoding="utf-8")), Path(a.director_out)))
        return
    work.mkdir(parents=True, exist_ok=True)
    rep = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    rep.setdefault("stories", [])
    rep["config"] = {"seeds": a.seeds, "workers": a.workers, "base_seed": a.base_seed, "period_s": a.period_s,
                     "command": "python -m touchline_live.eval.scenario_bench " + " ".join(sys.argv[1:]), **provenance()}

    def save():
        rep["summary"] = summarise(rep)
        if rep.get("tilt"):
            rep["tilt_summary"] = tilt_summary(rep)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")

    for i, (story, gold) in enumerate(STORIES[: a.stories]):
        if i >= len(rep["stories"]):
            rep["stories"].append({"story": story, "gold": [S.describe(b) for b in gold], "arms": {}})
        row = rep["stories"][i]
        if "spec" not in row:
            for _ in range(2):                      # one retry when the model was out of reach
                spec, trace = asyncio.run(S.direct(story))
                if not any(t.get("fallback") for t in trace):
                    break
            row["director_fell_back"] = any(t.get("fallback") for t in trace)
            rules, _ = S.gate(S.rules_director(story))
            row.update(spec=spec.model_dump(), trace=trace, rules=rules.model_dump(), **provenance(),
                       beats={"director": [S.describe(b) for b in spec.beats], "rules": [S.describe(b) for b in rules.beats]})
            save()
        spec = ScenarioSpec.model_validate(row["spec"])
        rules = ScenarioSpec.model_validate(row["rules"])
        row["read"] = {"director": read_score(spec.beats, gold), "rules": read_score(rules.beats, gold)}
        base = a.base_seed + 100 * i
        for arm in [x for x in a.arms.split(",") if x]:
            if arm in row["arms"]:
                continue
            arm_spec = {"director": spec, "rules": rules, "untouched": untouched(spec), "no_prune": spec}[arm]
            t0 = time.perf_counter()
            r = run_arm(arm, arm_spec, gold, base=base, seeds=a.seeds, workers=a.workers, work=work, period_s=a.period_s)
            row["arms"][arm] = r
            save()
            print(f"{i} {arm:9s} gold {r['gold']['met']}/{r['gold']['total']} own {r['own']['met']}/{r['own']['total']} "
                  f"{r['candidates']} cand {r['played_min']} min {time.perf_counter() - t0:.0f} s  {story}", flush=True)
    if a.tilt:
        rep["config"]["tilt"] = a.tilt
        tilt(rep, seeds=a.tilt, workers=a.workers, base_seed=a.base_seed + 20_000, period_s=a.period_s, save=save,
             arms=tuple(x for x in a.tilt_arms.split(",") if x))
    save()
    print(json.dumps({"summary": rep["summary"], "tilt": rep.get("tilt_summary")}, indent=1))


if __name__ == "__main__":
    main()
