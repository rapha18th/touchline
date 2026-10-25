import cv2
import numpy as np

from touchline_vision import pitch_model
from touchline_vision.calibrate import Target, cameras, icp, nearest_on_segments, placement
from touchline_vision.geometry import apply_h, h_from_p, homography_error, look_camera

W, H = 1280, 720


def synthetic(Hg: np.ndarray) -> np.ndarray:
    """A plain pitch drawn through a ground homography: green turf, a darker margin, white lines."""
    img = np.full((H, W, 3), (10, 12, 8), np.uint8)
    margin = apply_h(Hg, pitch_model.grass_outline())
    cv2.fillPoly(img, [np.round(margin).astype(np.int32)], (36, 58, 18))
    turf = apply_h(Hg, [[0, 0], [105, 0], [105, 68], [0, 68]])
    cv2.fillPoly(img, [np.round(turf).astype(np.int32)], (52, 84, 29))
    for pl in pitch_model.polylines():
        cv2.polylines(img, [np.round(apply_h(Hg, pl) * 16).astype(np.int32)], False, (235, 245, 245), 2, cv2.LINE_AA, 4)
    return img


def test_lines_and_icp_recover_a_perturbed_homography():
    Ht = h_from_p(look_camera([40.0, 115.0, 26.0], [35.0, 34.0, 0.0], 29.0, W, H))
    t = Target(synthetic(Ht))
    assert t.lines.sum() > 3000
    Hp = look_camera([40.6, 114.2, 26.4], [35.3, 33.7, 0.0], 29.3, W, H)
    H0 = h_from_p(Hp)
    assert homography_error(H0, Ht, W, H)["px_mean"] > 2
    H1 = icp(t, H0)
    assert homography_error(H1, Ht, W, H)["px_mean"] < 1.0
    assert t.chamfer(H1)[0] < t.chamfer(H0)[0]


def test_orbit_placement_round_trip():
    x = np.array([35.0, 34.0, 85.0, np.radians(18), np.radians(4), np.log(18.0), 0.0])
    pos, look, fov, roll = placement(x, H)
    assert np.isclose(np.linalg.norm(pos[0] - look[0]), 85.0)
    assert pos[0, 1] > 68 and pos[0, 2] > 0
    P = cameras(x, W, H)[0]
    # Image scale at the look point is exp(s) pixels per metre, give or take the perspective.
    a = apply_h(h_from_p(P), [[34.5, 34.0], [35.5, 34.0]])
    assert abs(np.linalg.norm(a[1] - a[0]) - 18.0) < 1.0


def test_nearest_on_segments():
    a = np.array([[0.0, 0.0], [10.0, 0.0]])
    b = np.array([[10.0, 0.0], [10.0, 10.0]])
    near, j, s = nearest_on_segments(np.array([[5.0, 1.0], [12.0, 5.0]]), a, b)
    assert np.allclose(near, [[5, 0], [10, 5]]) and j.tolist() == [0, 1]
