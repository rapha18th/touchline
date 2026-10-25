import numpy as np
import pytest

from touchline_vision import pitch_model
from touchline_vision.geometry import (apply_h, apply_h_front, camera_centre, camera_from_h, ground_at_height,
                                       h_from_p, height_over, homography_error, look_camera, plane_h, project)
from touchline_vision.optim import nelder_mead

W, H = 1280, 720


def cam():
    return look_camera([40.0, 115.0, 26.0], [35.0, 34.0, 0.0], 29.0, W, H)


def test_look_camera_centre_and_look_point():
    P = cam()
    assert np.allclose(camera_centre(P), [40.0, 115.0, 26.0], atol=1e-6)
    # The look point lands on the principal point (OpenCV pixel centres).
    assert np.allclose(project(P, [[35.0, 34.0, 0.0]])[0], [(W - 1) / 2, (H - 1) / 2], atol=1e-6)


def test_ground_homography_matches_camera_on_the_ground():
    P = cam()
    Hg = h_from_p(P)
    pts = np.array([[0, 0], [52.5, 34], [105, 68], [20, 50]], float)
    assert np.allclose(apply_h(Hg, pts), project(P, np.c_[pts, np.zeros(4)]), atol=1e-6)


def test_camera_from_h_recovers_the_full_camera():
    P = cam()
    P2, f = camera_from_h(h_from_p(P), W, H)
    X = np.array([[30, 30, 0.22], [50, 20, 5.0], [10, 40, 10.0], [80, 60, 2.0]])
    assert np.allclose(project(P, X), project(P2, X), atol=1e-4)
    assert f == pytest.approx((H / 2) / np.tan(np.radians(29.0) / 2), rel=1e-6)


def test_ground_at_height_inverts_projection():
    P = cam()
    X = np.array([[33.0, 21.0, 1.7], [70.0, 50.0, 1.7]])
    assert np.allclose(ground_at_height(P, project(P, X), 1.7), X[:, :2], atol=1e-6)
    assert np.allclose(plane_h(P, 0.0) / plane_h(P, 0.0)[2, 2], h_from_p(P), atol=1e-9)


def test_height_over_reads_a_point_above_the_ground():
    P = cam()
    uv = project(P, [[44.0, 30.0, 3.2]])[0]
    assert height_over(P, [44.0, 30.0], uv) == pytest.approx(3.2, abs=1e-6)


def test_front_flags_points_behind_the_camera():
    P = cam()
    _, front = apply_h_front(h_from_p(P), [[52.5, 34.0], [40.0, 300.0]])
    assert front.tolist() == [True, False]


def test_homography_error_is_zero_for_the_same_h_and_grows_with_a_shift():
    Hg = h_from_p(cam())
    e0 = homography_error(Hg, Hg, W, H)
    assert e0["px_mean"] == pytest.approx(0.0, abs=1e-9) and e0["n"] > 100
    shift = np.array([[1, 0, 2.0], [0, 1, 0], [0, 0, 1]])
    e1 = homography_error(shift @ Hg, Hg, W, H)
    assert e1["px_mean"] == pytest.approx(2.0, abs=1e-6)


def test_pitch_model_lines():
    lines = pitch_model.polylines()
    assert len(lines) == 6 + 2 * 3
    centre = lines[5]
    r = np.hypot(centre[:, 0] - 52.5, centre[:, 1] - 34.0)
    assert np.allclose(r, pitch_model.CIRCLE_R)
    pts = pitch_model.sample(0.5)
    assert pts[:, 0].min() >= 0 and pts[:, 0].max() <= 105 and pts[:, 1].min() >= 0 and pts[:, 1].max() <= 68
    # Penalty arcs meet the box edge.
    arc = lines[8]
    assert np.allclose(arc[[0, -1], 0], pitch_model.BOX_D, atol=1e-9)


def test_nelder_mead_finds_a_quadratic_minimum():
    x, fx = nelder_mead(lambda v: (v[0] - 3) ** 2 + 4 * (v[1] + 1) ** 2 + 2, [0.0, 0.0], [1.0, 1.0], iters=500, tol=1e-12)
    assert np.allclose(x, [3, -1], atol=1e-4) and fx == pytest.approx(2.0, abs=1e-6)
