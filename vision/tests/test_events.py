import numpy as np
import pytest

from touchline_vision.events import (BALL_Z, classify, eval_air, fit_air, fit_ground, next_onset, release_speed)
from touchline_vision.geometry import look_camera, project
from touchline_vision.score import match, summarise
from touchline_vision.track import ball_path, fill_gaps

W, H, FPS = 1280, 720, 25.0
P = look_camera([40.0, 115.0, 26.0], [35.0, 34.0, 0.0], 29.0, W, H)


def test_fit_ground_recovers_a_rolling_ball():
    tau = np.arange(1, 20) / FPS
    x = 20 + 18 * tau - 4 * tau ** 2
    y = 30 + 6 * tau - 1 * tau ** 2
    uv = project(P, np.column_stack([x, y, np.full_like(x, BALL_Z)]))
    c, rms = fit_ground(P, tau, uv)
    assert rms < 1e-6
    assert np.allclose(c[0], [20, 30], atol=1e-6) and np.allclose(c[1], [18, 6], atol=1e-6)


def test_fit_air_recovers_a_lofted_ball_from_one_view():
    tau = np.arange(1, 30) / FPS
    th = np.array([30.0, 20.0, 14.0, 9.0, 8.0, -6.5])
    uv = project(P, eval_air(th, tau))
    est, rms = fit_air(P, tau, uv)
    assert rms < 1e-6 and np.allclose(est, th, atol=1e-5)
    # The rolling model cannot explain it.
    assert fit_ground(P, tau, uv)[1] > 1.0


def test_release_speed_of_a_decelerating_ball():
    tau = np.arange(0, 12) / FPS
    s = 15 * tau - 3 * tau ** 2
    g = np.column_stack([10 + 0.6 * s, 10 + 0.8 * s])
    assert release_speed(g, tau) == pytest.approx(15.0, rel=1e-6)


def test_next_onset_finds_the_kick_and_ignores_a_one_frame_jump():
    T = 60
    g = np.zeros((T, 2))
    g[:, 0] = 30 + 0.1 * np.arange(T)              # a slow dribble
    g[20:, 0] += 0.7                               # a one-frame jump of the ball ahead of the carrier
    kick = 40
    g[kick + 1:, 0] = g[kick, 0] + 0.6 * np.arange(1, T - kick)   # 15 m/s from frame 40
    assert next_onset(g, FPS) == kick


def test_classify_shot_pass_and_back_pass():
    assert classify([85, 30], [20, 1], [105, 31], 25, "home", None) == "shot"                # into the goal
    assert classify([85, 30], [20, 1], [103, 31], 25, "home", "away_gk") == "shot"           # saved
    assert classify([85, 30], [20, 1], [103, 31], 25, "home", "home_gk") == "pass"           # back to their keeper
    assert classify([85, 10], [2, 20], [90, 50], 25, "home", "home") == "pass"               # across the box
    assert classify([60, 30], [20, 1], [80, 31], 25, "home", None) == "pass"                 # 45 m out
    assert classify([85, 30], [20, 1], [103, 31], 25, "home", None, aerial_apex=7.0) == "pass"   # a cross


def test_ball_path_prefers_the_moving_ball_over_scattered_noise():
    rng = np.random.default_rng(1)
    cands = []
    for t in range(40):
        ball = [100 + 8 * t, 300 + 2 * t, 1.0]
        noise = rng.uniform([0, 0, -0.5], [1280, 720, 0.5], (3, 3))
        c = np.vstack([noise, ball]) if t % 7 else noise        # the ball is missing every 7th frame
        cands.append(c)
    path = ball_path(cands)
    for t, j in enumerate(path):
        if t % 7:
            assert j is not None and cands[t][j][0] == 100 + 8 * t


def test_fill_gaps_bridges_short_gaps_only():
    t = np.array([0, 3, 20])
    xy = np.array([[0.0, 0.0], [3.0, 3.0], [20.0, 20.0]])
    out, seen = fill_gaps(t, xy, 21, max_gap=12)
    assert np.allclose(out[1], [1, 1]) and np.isnan(out[10, 0]) and seen.sum() == 3


def test_match_is_one_to_one_within_tolerance():
    dets = [{"frame": 10}, {"frame": 12}, {"frame": 40}]
    truth = [{"rel": 11}, {"rel": 30}]
    assert sorted(match(dets, truth, tol=4)) == [(0, 0)]


def test_summarise_counts_and_errors():
    rows = [{"truth_type": "pass", "det_type": "pass", "frame_err": 0, "outcome": "complete", "height": "ground",
             "aerial": False, "lands_in_clip": True, "length_true": 10.0, "travel_true": 10.0, "speed_true": 60.0,
             "length_det": 11.0, "speed_det": 57.0},
            {"truth_type": "shot", "det_type": "pass", "frame_err": 1, "outcome": "saved", "height": None,
             "aerial": True, "lands_in_clip": True, "speed_true": 90.0, "length_det": 20.0, "speed_det": 88.0}]
    clip = {"x": {"rows": rows, "n_truth": {"pass": 2, "shot": 1}, "n_det": {"pass": 2, "shot": 0},
                  "kick_found": 2, "kick_truth": 3, "kick_det": 2}}
    s = summarise([clip], "x")
    assert s["pass"]["recall"] == 0.5 and s["pass"]["precision"] == 0.5
    assert s["shot"]["recall"] == 0.0 and s["shot"]["precision"] is None
    assert s["pass_length_m"]["mae"] == 1.0 and s["ball_speed_kmh"]["mae"] == 3.0 and s["ball_speed_kmh"]["bias"] == -3.0


def test_despike_straightens_a_zig_zag():
    from touchline_vision.track import despike
    cands = [np.array([[100.0 + 10 * t, 300.0, 1.0], [100.0 + 10 * t, 330.0, 0.0]]) for t in range(10)]
    path = [0] * 10
    path[5] = 1                                    # one frame on the white kit beside the ball
    assert despike(cands, path)[5] == 0


def test_kick_time_runs_a_hidden_kick_back_to_the_ball_at_rest():
    from touchline_vision.events import kick_time
    T = 40
    g = np.full((T, 2), np.nan)
    g[:10] = [30.0, 30.0]                          # at the kicker's feet
    kick = 12
    for t in range(16, T):                         # hidden from 10 to 15, kicked at 12, 15 m/s
        g[t] = [30.0 + 0.6 * (t - kick), 30.0]
    uv = np.where(np.isnan(g), np.nan, 1.0)
    fr = np.arange(17, 30)
    assert kick_time(16, fr, g, uv, None, FPS) == kick
