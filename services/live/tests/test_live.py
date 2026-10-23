"""Live services on a freshly generated short match: state, cues, the claim checker and the Verifier gates."""

import asyncio

import pytest

from touchline_engine import export
from touchline_engine.sim import Match as EngineMatch
from touchline_live import feed
from touchline_live.crew.models import Claim
from touchline_live.crew.verify import verify_text
from touchline_live.match_io import load
from touchline_live.mcp_server import check
from touchline_live.state import LiveState


@pytest.fixture(scope="module")
def match(tmp_path_factory):
    m = EngineMatch(21)
    m.play_period(1, 1200, 0)
    m.period_frames = m.period_frames[:1]
    out = tmp_path_factory.mktemp("matches") / "TL-000021"
    export.write(m, out)
    return load(out)


@pytest.fixture(scope="module")
def replayed(match):
    st = LiveState(match)
    cues = []
    for it in feed.items(match, track_every=5):
        if it.kind == "event":
            c, _ = st.apply(it.payload)
        else:
            c = st.track(it.frame, it.payload, it.period, it.t)
        cues += c
    return st, cues


def test_feed_is_time_ordered(match):
    times = [it.match_s for it in feed.items(match, track_every=25)]
    assert times == sorted(times)


def test_state_counts_match_the_log(match, replayed):
    st, _ = replayed
    for side in ("home", "away"):
        shots = sum(1 for e in match.events if e["type"] == "shot" and e["team"] == side)
        passes = sum(1 for e in match.events if e["type"] == "pass" and e["team"] == side)
        assert st.teams[side].shots == shots
        assert st.teams[side].passes == passes


def test_cues_follow_the_schema_shape(replayed):
    _, cues = replayed
    assert cues
    for c in cues:
        assert c["schema"] == "touchline.cue/1"
        assert {"id", "kind", "match_s", "frame", "priority", "ttl_s", "audience", "payload", "sources"} <= set(c)
        assert 0 <= c["priority"] <= 3


def test_claims_pass_when_true_and_fail_when_false(match, replayed):
    st, _ = replayed
    shot = next(e for e in match.events if e["type"] == "shot")
    true = {"kind": "event", "id": shot["id"], "field": "q.xg", "value": shot["q"]["xg"]}
    false = {**true, "value": round(shot["q"]["xg"] + 0.3, 2)}
    prefixless = {"kind": "event", "id": shot["id"], "field": "xg", "value": shot["q"]["xg"]}
    score = {"kind": "score", "home": st.score[0], "away": st.score[1]}
    assert check(true, st, match)["ok"]
    assert not check(false, st, match)["ok"]
    assert check(prefixless, st, match)["ok"]
    assert check(score, st, match)["ok"]
    assert not check({"kind": "score", "home": st.score[0] + 1, "away": st.score[1]}, st, match)["ok"]


class FakeRecord:
    calls = 0

    async def call(self, tool, **args):
        return [{**c, "ok": True, "actual": c.get("value")} for c in args["claims"]]


def test_verifier_gates_catch_invented_numbers_names_and_dashes():
    facts = {"facts": {"xg": 0.21, "distance_m": 8.3, "score": "Harbourne FC 1-0 Corran Town"}}
    roster, clubs = ["Ade Okmar"], ["Harbourne FC", "Corran Town"]
    run = lambda text, claims=(): asyncio.run(verify_text(FakeRecord(), "TL-1", 600, text, list(claims), facts, roster, clubs))
    assert run("Ade Okmar scores from 8.3 metres, xG 0.21.")["ok"]
    assert not run("Ade Okmar scores from 14 metres.")["ok"]                 # number not in the record
    assert not run("Marco Vettel scores for Harbourne FC.")["ok"]            # name not in this match
    assert not run("Ade Okmar scores — Harbourne FC lead.")["ok"]            # dash
    assert not run("Ade Okmar scores his best goal in history.")["ok"]       # history claim
    ok_claim = Claim(kind="event", id=3, field="q.xg", value=0.21)
    assert run("Ade Okmar, xG 0.21.", [ok_claim])["ok"]
