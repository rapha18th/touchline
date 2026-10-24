"""Plane and camera geometry in pitch metres: x along the length, y across the width, z up.

H maps a ground point (x, y) to a pixel (u, v). P is a full 3x4 pinhole camera on the same frame.
"""

from __future__ import annotations

import numpy as np

from . import LENGTH, WIDTH

# Pitch metres to the renderer's world frame (three.js: x right, y up, z toward the near touchline).
PITCH_TO_WORLD = np.array([[1.0, 0, 0, -LENGTH / 2], [0, 0, 1.0, 0], [0, 1.0, 0, -WIDTH / 2], [0, 0, 0, 1.0]])


def apply_h(H: np.ndarray, pts) -> np.ndarray:
    """Map (N, 2) points through a homography."""
    p = np.asarray(pts, float).reshape(-1, 2)
    q = p @ H[:, :2].T + H[:, 2]
    return q[:, :2] / q[:, 2:3]


def project(P: np.ndarray, X) -> np.ndarray:
    """Project (N, 3) pitch points to pixels."""
    X = np.asarray(X, float).reshape(-1, 3)
    q = X @ P[:, :3].T + P[:, 3]
    return q[:, :2] / q[:, 2:3]


def p_from_clip(m16, w: int, h: int) -> np.ndarray:
    """The renderer's clip-from-world matrix (column-major, 16 numbers) as a pitch-to-pixel 3x4 camera."""
    M = np.asarray(m16, float).reshape(4, 4).T
    C = M @ PITCH_TO_WORLD
    return np.stack([0.5 * w * (C[0] + C[3]), 0.5 * h * (C[3] - C[1]), C[3]])


def h_from_p(P: np.ndarray) -> np.ndarray:
    """The ground-plane (z = 0) homography of a camera."""
    H = P[:, [0, 1, 3]].copy()
    return H / H[2, 2]


def plane_h(P: np.ndarray, z: float) -> np.ndarray:
    """Homography of the horizontal plane at height z."""
    return np.stack([P[:, 0], P[:, 1], z * P[:, 2] + P[:, 3]], axis=1)


def ground_at_height(P: np.ndarray, uv, z: float) -> np.ndarray:
    """Pitch (x, y) of pixels assumed to lie at height z."""
    return apply_h(np.linalg.inv(plane_h(P, z)), uv)


def camera_from_h(H: np.ndarray, w: int, h: int) -> tuple[np.ndarray, float]:
    """A full camera from a ground homography, assuming square pixels and the principal point at the
    image centre. Returns P (3x4) and the focal length in pixels."""
    cx, cy = w / 2.0, h / 2.0
    Hs = np.array([H[0] - cx * H[2], H[1] - cy * H[2], H[2]], float)
    a, b = Hs[:, 0], Hs[:, 1]
    # g = 1 / f^2 from orthogonality and equal norm of the first two rotation columns.
    A = np.array([a[0] * b[0] + a[1] * b[1], a[0] ** 2 + a[1] ** 2 - b[0] ** 2 - b[1] ** 2])
    B = -np.array([a[2] * b[2], a[2] ** 2 - b[2] ** 2])
    g = float(A @ B / max(A @ A, 1e-18))
    f = 1.0 / np.sqrt(g) if g > 0 else float(max(w, h))
    Kinv = np.diag([1 / f, 1 / f, 1.0])
    r1, r2, t = Kinv @ a, Kinv @ b, Kinv @ Hs[:, 2]
    lam = 1.0 / np.sqrt(np.linalg.norm(r1) * np.linalg.norm(r2))
    r1, r2, t = r1 * lam, r2 * lam, t * lam
    if t[2] < 0:
        r1, r2, t = -r1, -r2, -t
    r3 = np.cross(r1, r2)
    if -(r3 @ t) < 0:                       # the camera must stand above the ground with z up
        r3 = -r3
    U, _, Vt = np.linalg.svd(np.stack([r1, r2, r3], axis=1))
    R = U @ Vt
    if np.sign(np.linalg.det(R)) != np.sign(np.linalg.det(np.stack([r1, r2, r3], axis=1))):
        R = U @ np.diag([1, 1, -1]) @ Vt
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
    return K @ np.column_stack([R, t]), float(f)


def camera_centre(P: np.ndarray) -> np.ndarray:
    """Camera centre in pitch metres."""
    return -np.linalg.solve(P[:, :3], P[:, 3])


def height_over(P: np.ndarray, ground_xy, uv) -> float:
    """Height of a point imaged at uv that stands straight above ground_xy (least squares)."""
    x, y = float(ground_xy[0]), float(ground_xy[1])
    base = P @ np.array([x, y, 0.0, 1.0])
    up = P[:, 2]
    u, v = float(uv[0]), float(uv[1])
    # u * (base2 + h up2) = base0 + h up0, and the same for v.
    A = np.array([u * up[2] - up[0], v * up[2] - up[1]])
    b = np.array([base[0] - u * base[2], base[1] - v * base[2]])
    return float(A @ b / max(A @ A, 1e-12))


def visible_grid(H: np.ndarray, w: int, h: int, step: float = 2.0, margin: float = 0.0) -> np.ndarray:
    """Pitch grid points whose image falls inside the frame."""
    xs, ys = np.meshgrid(np.arange(0, LENGTH + 1e-6, step), np.arange(0, WIDTH + 1e-6, step))
    g = np.column_stack([xs.ravel(), ys.ravel()])
    uv = apply_h(H, g)
    ok = (uv[:, 0] >= margin) & (uv[:, 0] < w - margin) & (uv[:, 1] >= margin) & (uv[:, 1] < h - margin)
    return g[ok]


def homography_error(H_est: np.ndarray, H_true: np.ndarray, w: int, h: int) -> dict:
    """How far an estimated ground homography lands from the true one.

    px: pixel distance between the two projections of visible pitch points.
    m: ground distance between the two back-projections of the same pixels, for pixels on the pitch.
    """
    g = visible_grid(H_true, w, h)
    if len(g) == 0:
        return {"px_mean": None, "px_p90": None, "m_mean": None, "m_p90": None, "n": 0}
    uv_true = apply_h(H_true, g)
    px = np.linalg.norm(apply_h(H_est, g) - uv_true, axis=1)
    m = np.linalg.norm(apply_h(np.linalg.inv(H_est), uv_true) - g, axis=1)
    return {"px_mean": float(px.mean()), "px_p90": float(np.percentile(px, 90)),
            "m_mean": float(m.mean()), "m_p90": float(np.percentile(m, 90)), "n": int(len(g))}
