"""The camera, found from the painted lines alone.

1. A coarse search over broadcast camera placements (a camera on the near side, above the pitch,
   looking at it) scores each candidate by how many projected model lines land on detected lines.
2. The best candidates are refined with Nelder-Mead on a symmetric chamfer cost: model to image and
   image to model, truncated so goal frames and stray marks do not pull.
3. ICP polishes the full 8-parameter ground homography against the line pixels.

The renderer's camera matrix is never read here; it only grades the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from . import pitch_model
from .geometry import apply_h, apply_h_front, h_from_p, look_cameras
from .lines import grass_mask, line_mask
from .optim import nelder_mead

MODEL = pitch_model.sample(0.5)
_POLY = pitch_model.polylines()
SEG_A = np.concatenate([p[:-1] for p in _POLY])
SEG_B = np.concatenate([p[1:] for p in _POLY])


@dataclass
class Calibration:
    H: np.ndarray                   # ground (z = 0) to pixel
    P: np.ndarray                   # full 3x4 camera (pitch metres to pixels)
    params: list = field(default_factory=list)   # look x, y, distance, elevation, azimuth, log scale, roll
    fit: dict = field(default_factory=dict)


def placement(params: np.ndarray, h: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Camera position, look point, vertical fov and roll from orbit parameters.

    The camera orbits its look point (lx, ly) at distance D, elevation el and azimuth az (0 is straight
    out over the near touchline), and s = log of the image scale in pixels per metre at the look point.
    Scale instead of focal length keeps the fit out of the long dolly-zoom valley: moving the camera
    back at fixed scale changes only the strength of the perspective."""
    p = np.atleast_2d(params)
    lx, ly, D, el, az, s, roll = p.T
    pos = np.column_stack([lx + D * np.cos(el) * np.sin(az), ly + D * np.cos(el) * np.cos(az), D * np.sin(el)])
    look = np.column_stack([lx, ly, np.zeros(len(p))])
    f = np.exp(s) * D
    fov = np.degrees(2 * np.arctan((h / 2) / f))
    return pos, look, fov, roll


def cameras(params: np.ndarray, w: int, h: int) -> np.ndarray:
    pos, look, fov, roll = placement(params, h)
    return look_cameras(pos, look, fov, w, h, roll)


def prior(rng: np.random.Generator, n: int) -> np.ndarray:
    """Broadcast placements: on the near side, 35 to 150 m from the look point, 8 to 45 degrees down,
    6 to 60 pixels per metre at the look point."""
    return np.column_stack([rng.uniform(5, 100, n), rng.uniform(6, 62, n), rng.uniform(35, 150, n),
                            np.radians(rng.uniform(8, 45, n)), np.radians(rng.uniform(-40, 40, n)),
                            rng.uniform(np.log(6), np.log(60), n), np.zeros(n)])


STEP = np.array([1.5, 1.5, 8.0, np.radians(2.0), np.radians(3.0), 0.05, np.radians(0.3)])


def project_batch(P: np.ndarray, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ground points (M, 2) through cameras (N, 3, 4): pixels (N, M, 2) and in-front flags (N, M)."""
    Hs = P[:, :, [0, 1, 3]]
    q = np.einsum("nij,mj->nmi", Hs, np.column_stack([pts, np.ones(len(pts))]))
    front = q[..., 2] > 1e-6
    return q[..., :2] / np.where(front, q[..., 2], 1.0)[..., None], front


class Target:
    """What one empty-pitch image offers the fit: line pixels, their distance transform and the grass."""

    def __init__(self, bg: np.ndarray, max_pts: int = 3000, seed: int = 0):
        self.h, self.w = bg.shape[:2]
        self.grass = grass_mask(bg)
        self.lines = line_mask(bg, self.grass)
        self.dt = cv2.distanceTransform((1 - self.lines).astype(np.uint8), cv2.DIST_L2, 5)
        ys, xs = np.nonzero(self.lines)
        pts = np.column_stack([xs, ys]).astype(float)
        if len(pts) > max_pts:
            pts = pts[np.random.default_rng(seed).choice(len(pts), max_pts, replace=False)]
        self.pts = pts

    def lookup(self, uv: np.ndarray, front: np.ndarray, img: np.ndarray, fill: float) -> tuple[np.ndarray, np.ndarray]:
        u, v = uv[..., 0], uv[..., 1]
        vis = front & (u >= 0) & (u < self.w - 1) & (v >= 0) & (v < self.h - 1)
        ui = np.where(vis, u, 0).astype(int)
        vi = np.where(vis, v, 0).astype(int)
        return np.where(vis, img[vi, ui], fill), vis

    def coarse(self, params: np.ndarray, sigma: float = 12.0, n0: int = 150) -> np.ndarray:
        """Share of projected model points that sit near a line pixel, on the grass."""
        out = np.empty(len(params))
        for a in range(0, len(params), 1000):
            P = cameras(params[a:a + 1000], self.w, self.h)
            uv, front = project_batch(P, MODEL)
            d, vis = self.lookup(uv, front, self.dt, 1e3)
            g, _ = self.lookup(uv, front, self.grass.astype(np.float32), 0.0)
            wgt = np.exp(-0.5 * (d / sigma) ** 2) * (g > 0)
            out[a:a + 1000] = wgt.sum(1) / np.maximum(vis.sum(1), n0)
        return out

    def recall(self, params: np.ndarray, sigma: float = 12.0, scale: float = 0.25, n_pts: int = 800) -> np.ndarray:
        """Share of line pixels that lie near the projected model lines of each candidate camera."""
        pts = self.pts[:: max(len(self.pts) // n_pts, 1)] * scale
        hs, ws = int(self.h * scale), int(self.w * scale)
        pi = np.minimum(pts.astype(int), [ws - 1, hs - 1])
        out = np.zeros(len(params))
        for k, P in enumerate(cameras(params, self.w, self.h)):
            H = h_from_p(P)
            canvas = np.zeros((hs, ws), np.uint8)
            for pl in _POLY:
                q, ok = apply_h_front(H, pl)
                ok &= np.abs(q).max(axis=1) < 2e4
                if ok.all():
                    cv2.polylines(canvas, [np.round(q * scale * 4).astype(np.int32)], False, 1, 1, cv2.LINE_8, 2)
            dtm = cv2.distanceTransform((1 - canvas).astype(np.uint8), cv2.DIST_L2, 3) / scale
            out[k] = float(np.exp(-0.5 * (dtm[pi[:, 1], pi[:, 0]] / sigma) ** 2).mean())
        return out

    def chamfer(self, H: np.ndarray, tau: float = 10.0, scale: float = 1.0) -> tuple[float, float, float]:
        """Symmetric truncated chamfer of a ground homography: (cost, model to image px, image to model px).
        scale < 1 draws the model on a smaller canvas for speed; distances stay in full-size pixels."""
        uv, front = apply_h_front(H, MODEL)
        d, vis = self.lookup(uv[None], front[None], self.dt, tau)
        nv = int(vis.sum())
        if nv < 30:
            return 1e6, tau, tau
        dm = np.minimum(d[0][vis[0]], tau)
        hs, ws = int(round(self.h * scale)), int(round(self.w * scale))
        canvas = np.zeros((hs, ws), np.uint8)
        for pl in _POLY:
            q, ok = apply_h_front(H, pl)
            ok &= np.abs(q).max(axis=1) < 2e4
            for run in np.split(np.arange(len(q)), np.nonzero(np.diff(ok.astype(int)))[0] + 1):
                if ok[run[0]] and len(run) > 1:
                    cv2.polylines(canvas, [np.round(q[run] * scale * 4).astype(np.int32)], False, 1, 1, cv2.LINE_8, 2)
        dtm = cv2.distanceTransform((1 - canvas).astype(np.uint8), cv2.DIST_L2, 5) / scale
        pi = np.minimum((self.pts * scale).astype(int), [ws - 1, hs - 1])
        dd = np.minimum(dtm[pi[:, 1], pi[:, 0]], tau)
        return float((dm ** 2).mean() + (dd ** 2).mean()), float(dm.mean()), float(dd.mean())


def nearest_on_segments(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For points p (N, 2): the nearest point on any segment a-b (S, 2), its segment index and parameter."""
    ab = b - a
    s = np.clip(((p[:, None, :] - a[None]) * ab[None]).sum(-1) / np.maximum((ab ** 2).sum(-1), 1e-12)[None], 0, 1)
    near = a[None] + s[..., None] * ab[None]
    j = np.argmin(((near - p[:, None, :]) ** 2).sum(-1), axis=1)
    i = np.arange(len(p))
    return near[i, j], j, s[i, j]


def icp(t: Target, H: np.ndarray, iters: int = 40) -> np.ndarray:
    """Polish a ground homography: pair each line pixel with the nearest point of the projected model
    lines (in pixels), map that point back onto the painted line, refit, repeat."""
    for k in range(iters):
        tau = max(10.0 * (1 - k / (0.6 * iters)), 2.5)
        a, fa = apply_h_front(H, SEG_A)
        b, fb = apply_h_front(H, SEG_B)
        ok = fa & fb
        if ok.sum() < 4:
            break
        near, j, s = nearest_on_segments(t.pts, a[ok], b[ok])
        keep = np.linalg.norm(near - t.pts, axis=1) < tau
        if keep.sum() < 20:
            break
        # The pitch point under the matched pixel: projective lines map to lines, so back-project it.
        m = apply_h(np.linalg.inv(H), near[keep])
        Hn, _ = cv2.findHomography(m, t.pts[keep], 0)
        if Hn is None:
            break
        H = Hn / Hn[2, 2]
    return H


def calibrate(bg: np.ndarray, seed: int = 0, n_search: int = 30000, n_refine: int = 8, good: float = 6.0,
              hops: int = 8) -> Calibration:
    t = Target(bg, seed=seed)
    h, w = bg.shape[:2]
    rng = np.random.default_rng(seed)
    cand = prior(rng, n_search)
    cand = cand[placement(cand, h)[0][:, 1] >= 72.0]

    def fscore(c, sigma):
        p = t.coarse(c, sigma=sigma)
        keep = np.argsort(-p)[:1500]
        r = np.zeros(len(c))
        r[keep] = t.recall(c[keep], sigma=sigma)
        return 2 * p * r / np.maximum(p + r, 1e-9)

    # Precision (model lines on image lines) for every candidate, recall (image lines explained) for the best.
    s = fscore(cand, 12.0)
    top = cand[np.argsort(-s)[: n_refine * 4]]
    # A second, tighter round around the best placements.
    local = np.concatenate([c + rng.normal(0, 1.5 * STEP, (200, 7)) * [1, 1, 1, 1, 1, 1, 0] for c in top[:n_refine * 2]])
    pool = np.concatenate([top, local])
    s2 = fscore(pool, 6.0)
    starts = []
    for i in np.argsort(-s2):
        c = pool[i]
        if all(np.abs(c[:2] - o[:2]).max() > 3 or abs(c[2] - o[2]) > 10 for o in starts):
            starts.append(c)
        if len(starts) >= n_refine:
            break

    def cost(x, tau):
        # Stay a broadcast camera: off the pitch on the near side, above it, looking at the pitch.
        if not (15 < x[2] < 400) or not (0.03 < x[3] < 1.4) or abs(x[4]) > 1.2:
            return 1e6
        if placement(x, h)[0][0, 1] < 72.0:
            return 1e6
        return t.chamfer(h_from_p(cameras(x, w, h)[0]), tau=tau, scale=0.5)[0]

    def refine(x):
        # Wide basin first (a 40 px truncation lets far-off lines pull), then tight.
        x, _ = nelder_mead(lambda v: cost(v, 40.0), x, STEP, iters=250)
        x, _ = nelder_mead(lambda v: cost(v, 10.0), x, STEP / 2, iters=250)
        return x, cost(x, 6.0)

    best = None
    for c in starts:
        x, fx = refine(c)
        if best is None or fx < best[1]:
            best = (x, fx)
        if fx < good / 2:
            break
    # Basin hopping: while the fit is poor, jump from the best fit and refine again.
    for _ in range(hops):
        if best[1] < good:
            break
        jump = best[0] + rng.normal(0, 1, 7) * np.array([4, 4, 25, np.radians(6), np.radians(8), 0.15, 0])
        x, fx = refine(jump)
        if fx < best[1]:
            best = (x, fx)
    x, fx = nelder_mead(lambda v: cost(v, 6.0), best[0], STEP / 4, iters=300)
    P = cameras(x, w, h)[0]
    H0 = h_from_p(P)
    H = icp(t, H0)
    c0, c1 = t.chamfer(H0), t.chamfer(H)
    if c1[0] > c0[0]:
        H = H0
    cost_, m2i, i2m = t.chamfer(H)
    pos, look, fov, _ = placement(x, h)
    return Calibration(H=H / H[2, 2], P=P, params=[round(float(v), 5) for v in x],
                       fit={"cost": round(cost_, 3), "model_to_image_px": round(m2i, 3), "image_to_model_px": round(i2m, 3),
                            "line_px": int(t.lines.sum()), "icp_kept": bool(c1[0] <= c0[0]),
                            "camera": {"pos": [round(float(v), 2) for v in pos[0]], "look": [round(float(v), 2) for v in look[0][:2]],
                                       "fov": round(float(fov[0]), 2)}})
