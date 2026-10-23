"""Engine invariants on a short match: determinism, consistency of events and tracking."""

import numpy as np
import pytest

from touchline_engine import pitch as P
from touchline_engine.sim import N, Match
from touchline_engine.squads import make_fixture


@pytest.fixture(scope="module")
def short_match():
    m = Match(11)
    m.play_period(1, 900, 0)
    return m


def test_same_seed_same_match():
    a, b = Match(5), Match(5)
    a.play_period(1, 240, 0)
    b.play_period(1, 240, 0)
    assert [(e["type"], e["frame"]) for e in a.events] == [(e["type"], e["frame"]) for e in b.events]
    assert np.array_equal(a.frames[0], b.frames[0])


def test_fixture_is_fictional_and_complete():
    home, away = make_fixture(3)
    assert len(home.players) == 18 and len(away.players) == 18
    names = {p.name for p in home.players + away.players}
    assert len(names) == 36
    assert home.name != away.name


def test_score_matches_goal_events(short_match):
    goals = [e for e in short_match.events if e["type"] == "goal"]
    home = sum(e["team"] == "home" for e in goals)
    away = sum(e["team"] == "away" for e in goals)
    assert short_match.score == [home, away]


def test_every_shot_has_xg_and_speed(short_match):
    for e in short_match.events:
        if e["type"] == "shot":
            assert 0 < e["q"]["xg"] < 1
            assert 30 < e["q"]["shot_speed_kmh"] < 130
            assert e["truth"]["xg"] == e["q"]["xg"]


def test_pass_truth_is_a_probability(short_match):
    passes = [e for e in short_match.events if e["type"] == "pass"]
    assert passes
    for e in passes:
        assert 0 <= e["truth"]["p_success"] <= 1
        assert e["outcome"] in ("complete", "incomplete", "offside")


def test_events_are_time_ordered_by_frame(short_match):
    frames = [e["frame"] for e in short_match.events]
    assert frames == sorted(frames)


def test_tracking_stays_on_the_pitch_and_under_top_speed(short_match):
    fr = short_match.frames[0]
    assert fr.shape[1:] == (N + 2, 2)
    players = fr[:, :N]
    assert players[..., 0].min() > -3 and players[..., 0].max() < P.LENGTH + 3
    step = np.linalg.norm(np.diff(players, axis=0), axis=2) * P.FPS
    step = step[1:]                     # the kickoff layout snaps on frame 0
    assert np.percentile(step, 99.9) < 10.5


def test_xg_model_shape():
    assert P.xg(P.LENGTH - P.PENALTY_SPOT, P.GOAL_Y) > 0.2
    assert P.xg(P.LENGTH - 25, P.GOAL_Y) < 0.06
    assert P.xg(P.LENGTH - 8, P.GOAL_Y, header=True) < P.xg(P.LENGTH - 8, P.GOAL_Y)
