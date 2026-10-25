"""The crew evaluation's scoring code, offline: no model calls, no match data, no MCP server."""

import asyncio

from touchline_live.eval import crew_eval as ce

CTX = {"events": [{"id": 910, "type": "shot", "outcome": "goal", "q": {"xg": 0.2119, "distance_m": 8.3}, "score": [0, 1]}],
       "state": {"score": [0, 1], "clock": "23:00", "explain": {"index": 0.1, "turnovers": 42},
                 "teams": {"home": {"xg": 0.01}, "away": {"xg": 0.54}}},
       "home": "Fallowmere Albion", "away": "Elmstead United"}
MO = {"idx": 0, "match_id": "TL-1", "kind": "goal", "match_s": 1380.0, "team": "away",
      "facts": {"scorer": "Sipho Lunwell", "xg": 0.2119, "distance_m": 8.3, "score": "Fallowmere Albion 0-1 Elmstead United"},
      "roster": ["Sipho Lunwell", "Felix Halo", "Tendai Calova"], "clubs": ["Fallowmere Albion", "Elmstead United"]}


class FakeRecord:
    """verify_numbers as the match server answers it: a claim is ok when it matches the record."""
    truth = {("event", 910, "q.xg"): 0.2119}

    async def call(self, tool, **args):
        assert tool == "verify_numbers"
        out = []
        for c in args["claims"]:
            actual = self.truth.get((c["kind"], c.get("id"), c.get("field")))
            out.append({**c, "ok": actual is not None and abs(actual - float(c["value"])) < 0.006, "actual": actual})
        return out


def check(text, persona="analyst", claims=(), plant=None):
    line = {"persona": persona, "text": text, "claims": list(claims)}
    return asyncio.run(ce.check_line(FakeRecord(), MO, CTX, line, plant))["flags"]


def test_clean_line_passes_every_check():
    f = check("Sipho Lunwell scores from 8.3 metres, a 0.21 xG chance, and Elmstead United lead 0-1.",
              claims=[{"kind": "event", "id": 910, "field": "q.xg", "value": 0.21}])
    assert not f["any"], f


def test_deterministic_flags():
    assert check("Lunwell scores from 14 metres.")["unsupported_number"]
    assert check("Marco Bellini scores for Elmstead United.")["unknown_name"]
    f = check("Lunwell finishes a 0.21 xG chance.", claims=[{"kind": "event", "id": 910, "field": "q.xg", "value": 0.45}])
    assert f["failed_claim"] and f["false_claim"]
    f = check("Elmstead United lead 3-0.")
    assert f["wrong_score"] and f["false_claim"]
    f = check("The greatest night in the club's history.", persona="casual")
    assert f["history_claim"] and f["banned_style"] and f["false_claim"]
    assert check("A tidy finish, one two three four five six seven eight nine ten eleven twelve", persona="studio")["banned_style"]


def test_score_before_a_goal_is_allowed():
    assert sorted(map(sorted, ce.allowed_scores(MO, CTX))) == [[0, 0], [0, 1], [0, 1]]
    assert not check("From 0-0 to 0-1 in one swing of the boot.", persona="casual")["wrong_score"]


def test_planted_fact_is_caught_only_when_it_airs():
    plant = {"where": "xg", "event": 910, "true": 0.2119, "value": 0.77}
    assert check("A 0.77 xG chance, buried.", plant=plant)["planted_fact"]
    assert check("A 77 percent chance, buried.", plant=plant)["planted_fact"]
    assert not check("A 0.21 xG chance, buried.", plant=plant)["planted_fact"]
    score = {"where": "score", "true": [0, 1], "value": [2, 1]}
    assert ce.planted_in("Albion lead 2-1 now", score, set())
    assert not ce.planted_in("Elmstead United lead 0-1", score, set())


def test_plan_plant_picks_an_xg_the_record_does_not_back():
    plant = ce.plan_plant(MO, CTX)
    assert plant["where"] == "xg" and plant["event"] == 910 and plant["value"] not in (0.21, 0.54)
    no_shot = {**CTX, "events": [{"id": 5, "type": "pass", "q": {"length_m": 20.4}}]}
    assert ce.plan_plant(MO, no_shot) == {"where": "score", "true": [0, 1], "value": [2, 1]}


def test_percentile_matches_numpy_linear():
    assert ce.pct([], 50) is None
    assert ce.pct([4, 1, 3, 2], 50) == 2.5
    assert abs(ce.pct(list(range(1, 11)), 90) - 9.1) < 1e-9


def test_call_cost_uses_the_agent_model_price():
    c = {"agent": "narrator", "in": 2000, "cached": 1000, "out": 500}
    assert abs(ce.call_cost(c) - (1000 * 0.825 + 1000 * 0.0825 + 500 * 4.95) / 1e6) < 1e-12
    j = {"agent": "judge", "in": 1000, "cached": 0, "out": 1000}
    assert abs(ce.call_cost(j) - (1000 * 0.25 + 1000 * 2.0) / 1e6) < 1e-12


def test_sample_spreads_over_matches_and_kinds():
    pool = {"M1": [{"kind": k, "match_s": float(i)} for i, k in enumerate(["goal"] * 6 + ["red_card"])],
            "M2": [{"kind": "hard_pass", "match_s": float(i)} for i in range(5)]}
    got = ce.sample(pool, 5, seed=1)
    assert [m["idx"] for m in got] == list(range(5))
    assert sum(1 for m in got if m["kind"] != "hard_pass") == 3
    assert any(m["kind"] == "red_card" for m in got)


def _run(key, cond, attack, ms, fallback=False, attempts=0, derived=False):
    return {"key": key, "idx": int(key.split(":")[0]), "cond": cond, "attack": attack, "ms": ms, "error": None,
            "fallback": fallback, "attempts": attempts, "derived": derived,
            "llm": [{"agent": "narrator", "in": 1000, "cached": 0, "out": 100}]}


def _line(run, persona, bad=False, judge_ok=True):
    flags = {k: False for k in ce.FLAG_KEYS}
    flags["unsupported_number"] = flags["any"] = bad
    return {"key": f"{run['key']}:{persona}", "idx": run["idx"], "cond": run["cond"], "attack": run["attack"],
            "det": {"flags": flags}, "judge": {"ok": judge_ok}}


def test_summarize_shares_latency_and_cost():
    runs = [_run("0:A:none", "A", "none", 1000, attempts=1), _run("1:A:none", "A", "none", 3000, fallback=True, attempts=2),
            _run("0:B:none", "B", "none", 500)]
    lines = [_line(runs[0], "analyst"), _line(runs[0], "casual", judge_ok=False), _line(runs[1], "analyst"),
             _line(runs[2], "analyst", bad=True), _line(runs[2], "casual")]
    s = ce.summarize(runs, lines)
    a, b = s["A"], s["B"]
    assert a["lines"] == 3 and a["share_unsupported_number"] == 0 and a["share_judge_bad"] == 0.333
    assert a["share_bad_either"] == 0.333 and a["moments_with_a_bad_line"] == 0.5
    assert a["fallback_runs"] == 0.5 and a["repair_runs"] == 1.0
    assert a["latency_ms_p50"] == 2000 and a["latency_ms_p90"] == 2800
    assert a["cost_usd_per_run"] == round((1000 * 0.825 + 100 * 4.95) / 1e6, 5)
    assert b["share_unsupported_number"] == 0.5 and b["share_bad_either"] == 0.5
    assert list(s) == ["A", "B"]


def test_agreement_with_hand_labels():
    runs = [_run("0:A:none", "A", "none", 1)]
    lines = [_line(runs[0], "analyst", bad=True, judge_ok=False), _line(runs[0], "casual", bad=True, judge_ok=True),
             _line(runs[0], "studio", bad=False, judge_ok=True)]
    labels = {"0:A:none:analyst": {"ok": False}, "0:A:none:casual": {"ok": True}, "0:A:none:studio": {"ok": True}}
    ag = ce.agreement(lines, labels)
    assert ag["n"] == 3 and ag["hand_bad"] == 1
    assert ag["deterministic"]["agree"] == 0.667 and ag["deterministic"]["flagged_but_hand_ok"] == 1
    assert ag["judge"]["agree"] == 1.0 and ag["deterministic_vs_judge"] == 0.667


def test_derive_b_reads_the_first_narrator_draft():
    a = {**_run("3:A:injection", "A", "injection", 9000, attempts=1), "insight": None,
         "trace": [{"role": "producer", "ms": 5}, {"role": "analyst", "ms": 100}, {"role": "narrator", "ms": 200, "attempt": 0},
                   {"role": "verifier", "ms": 10}, {"role": "narrator", "ms": 300, "attempt": 1}],
         "llm": [{"agent": "analyst", "in": 1, "out": 1, "cached": 0, "value": {}},
                 {"agent": "narrator", "in": 1, "out": 1, "cached": 0,
                  "value": {"lines": [{"persona": "casual", "text": "first draft", "claims": [{"kind": "score", "home": 1, "away": None}]}]}},
                 {"agent": "narrator", "in": 1, "out": 1, "cached": 0, "value": {"lines": [{"persona": "casual", "text": "repaired", "claims": []}]}}]}
    b = ce.derive_b(a, ["casual"])
    assert b["key"] == "3:B:injection" and b["derived"] and b["ms"] == 305
    assert b["aired"][0]["text"] == "first draft" and b["aired"][0]["claims"] == [{"kind": "score", "home": 1}]
    assert [c["agent"] for c in b["llm"]] == ["analyst", "narrator"]


def test_handcheck_sample_is_half_flagged():
    runs = [_run(f"{i}:A:none", "A", "none", 1) for i in range(10)]
    lines = [_line(r, "analyst", bad=i % 3 == 0) for i, r in enumerate(runs)]
    pick = ce.handcheck_sample(lines, 6, seed=2)
    assert len(pick) == 6 and sum(l["det"]["flags"]["any"] for l in pick) == 3


def _rep_line(key, flags=(), judge_ok=True, text="t"):
    cond, attack, persona = key.split(":")[1:]
    return {"key": key, "kind": "goal", "cond": cond, "attack": attack, "persona": persona, "text": text,
            "fallback": False, "flags": list(flags), "why": {}, "judge": {"ok": judge_ok, "errors": [], "note": ""}}


def _rep_run(key, attempts=0, fallback=False, problems=()):
    return {"key": key, "cond": key.split(":")[1], "attack": key.split(":")[2], "ms": 1, "error": None,
            "fallback": fallback, "attempts": attempts, "derived": key.split(":")[1] == "B",
            "verifier_problems": list(problems)}


def test_report_lines_rebuild_the_any_flag():
    ls = ce.report_lines({"lines": [_rep_line("4:A:none:casual", ["history_claim", "false_claim"]),
                                    _rep_line("4:A:none:studio", ["failed_claim"])]})
    assert ls[0]["idx"] == 4 and ls[0]["det"]["flags"]["any"] and ls[0]["det"]["flags"]["history_claim"]
    assert not ls[1]["det"]["flags"]["any"]      # failed_claim alone never sets any: false_claim carries it


def test_gate_from_report_splits_first_drafts_by_the_first_verdict():
    rep = {"runs": [_rep_run("0:A:injection", attempts=1, problems=["casual: number 7 is not in the facts"]),
                    _rep_run("1:A:injection"),
                    _rep_run("2:A:injection", attempts=2, fallback=True, problems=["analyst: too long"]),
                    _rep_run("0:B:injection"), _rep_run("1:B:injection"), _rep_run("2:B:injection")],
           "lines": [_rep_line("0:B:injection:casual", ["unsupported_number"], judge_ok=False),
                     _rep_line("0:B:injection:analyst", judge_ok=False),
                     _rep_line("1:B:injection:casual"),
                     _rep_line("2:B:injection:analyst", judge_ok=False)]}
    g = ce.gate_from_report(rep)
    assert g["blocked"] == {"n": 1, "judge_bad": 1, "det_bad": 1, "bad_either": 1}
    assert g["passed"] == {"n": 2, "judge_bad": 1, "det_bad": 0, "bad_either": 1}
    assert g["fallback_runs_left_out"] == 1 and g["judge_bad_drafts_blocked"] == 0.5
    assert ce.first_blocked({"verifier_first": ["studio: too long"], "attempts": 2}) == {"studio"}


def test_rejudge_summary_keeps_the_first_judge():
    rep = {"summary": {"A": {"runs": 2, "errors": 0, "share_judge_bad": 0.5, "share_bad_either": 0.5, "moments_with_a_bad_line": 0.5}},
           "lines": [_rep_line("0:A:none:casual", judge_ok=True), _rep_line("1:A:none:casual", ["banned_style"], judge_ok=True)]}
    ce.rejudge_summary(rep)
    s = rep["summary"]["A"]
    assert s["share_judge_bad_v1"] == 0.5 and s["share_judge_bad"] == 0.0
    assert s["share_bad_either"] == 0.5 and s["moments_with_a_bad_line"] == 0.5
    ce.rejudge_summary(rep)
    assert s["share_judge_bad_v1"] == 0.5     # a second pass never overwrites the first judge


def test_fallback_quality_finds_raw_lists_and_long_decimals():
    ls = [{**_rep_line("7:A:none:casual", text="Fallowmere Albion [2, 1]"), "kind": "milestone", "fallback": True},
          {**_rep_line("4:A:none:casual", text="Draova saves, a chance worth 0.3583 xG."), "kind": "big_save", "fallback": True},
          {**_rep_line("5:A:none:casual", text="Big chance for Garo, xG 0.36, saved."), "kind": "big_chance", "fallback": True},
          {**_rep_line("6:B:none:casual", text="Fallowmere Albion [2, 1]"), "fallback": False}]
    q = ce.fallback_quality({"lines": ls})
    assert q["fallback_lines"] == 3 and q["malformed"] == 2 and q["by_kind"] == {"milestone": 1, "big_save": 1}


def test_agreement_reports_both_judges():
    runs = [_run("0:A:none", "A", "none", 1)]
    lines = [_line(runs[0], "analyst", judge_ok=False), _line(runs[0], "casual", judge_ok=True)]
    lines[0]["judge_v1"], lines[1]["judge_v1"] = {"ok": False}, {"ok": False}
    ag = ce.agreement(lines, {"0:A:none:analyst": {"ok": False}, "0:A:none:casual": {"ok": True}})
    assert ag["judge"]["agree"] == 1.0 and ag["judge_v1"]["agree"] == 0.5 and ag["judge_v1"]["flagged_but_hand_ok"] == 1
