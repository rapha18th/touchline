"""Ball and player candidates in one frame, against the clip's empty-pitch background."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .geometry import apply_h


def hex_bgr(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)], float)


def lab(bgr) -> np.ndarray:
    """CIE Lab of BGR colours (OpenCV's 8-bit scaling undone), shape (..., 3)."""
    a = np.clip(np.asarray(bgr, np.float32), 0, 255).reshape(-1, 1, 3) / 255.0
    out = cv2.cvtColor(a, cv2.COLOR_BGR2LAB).reshape(-1, 3)
    return out.reshape(np.shape(bgr)[:-1] + (3,))


@dataclass
class Blob:
    u: float          # foot point (bottom centre) for players, centre for the ball
    v: float
    area: int
    w: int
    h: int
    colour: np.ndarray | None = None   # median BGR of the body
    label: str = ""                    # home, away, home_gk, away_gk, other
    score: float = 0.0


class Detector:
    """Foreground by background difference; the ball is a small round white blob, a player is an
    upright blob whose body colour is nearest one of the four kits."""

    def __init__(self, bg: np.ndarray, kits: dict, H: np.ndarray, fg_thresh: int = 28):
        self.bg8 = bg
        self.H = H
        self.Hinv = np.linalg.inv(H)
        self.fg_thresh = fg_thresh
        self.labels = ["home", "away", "home_gk", "away_gk"]
        refs = [kits["home"]["primary"], kits["away"]["primary"], kits["home"]["keeper"], kits["away"]["keeper"]]
        self.ref_lab = lab(np.array([hex_bgr(c) for c in refs]))
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    def px_per_m(self, u: float, v: float) -> float:
        """Image scale at a pixel on the ground: pixels per metre across the image."""
        g = apply_h(self.Hinv, [[u, v]])[0]
        a = apply_h(self.H, [g - [0.5, 0], g + [0.5, 0], g - [0, 0.5], g + [0, 0.5]])
        return float(max(np.linalg.norm(a[1] - a[0]), np.linalg.norm(a[3] - a[2])))

    def foreground(self, frame: np.ndarray) -> np.ndarray:
        """Pixels that changed against the empty pitch, less the shadows: a shadow darkens every channel
        by about the same factor, a kit changes the hue."""
        d = cv2.absdiff(frame, self.bg8)
        diff = d[..., 0].astype(np.uint16) + d[..., 1] + d[..., 2]
        ys, xs = np.nonzero(diff > self.fg_thresh * 1.4)
        ratio = (frame[ys, xs].astype(np.float32) + 1.0) / (self.bg8[ys, xs].astype(np.float32) + 1.0)
        hi, lo = ratio.max(axis=1), ratio.min(axis=1)
        keep = ~((hi < 0.85) & (lo > 0.4) & (hi - lo < 0.18))
        fg = np.zeros(diff.shape, np.uint8)
        fg[ys[keep], xs[keep]] = 1
        return cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.kernel)

    def detect(self, frame: np.ndarray) -> tuple[list[Blob], list[Blob], np.ndarray]:
        fg = self.foreground(frame)
        b, g, r = frame[..., 0].astype(np.int16), frame[..., 1].astype(np.int16), frame[..., 2].astype(np.int16)
        mn = np.minimum(np.minimum(b, g), r)
        mx = np.maximum(np.maximum(b, g), r)
        white = ((mn > 150) & (mx - mn < 45)).astype(np.uint8) & fg
        n, lab_img, stats, cents = cv2.connectedComponentsWithStats(fg, connectivity=8)
        players: list[Blob] = []
        big = np.zeros(n, bool)
        for k in range(1, n):
            x, y, w, h, area = stats[k]
            if area < 40 or h < 1.3 * w or h < 12:
                continue
            big[k] = True
            body = frame[y + h // 5: y + (3 * h) // 4, x: x + w]
            m = lab_img[y + h // 5: y + (3 * h) // 4, x: x + w] == k
            if m.sum() < 6:
                continue
            col = np.median(body[m], axis=0)
            d = np.linalg.norm(self.ref_lab - lab(col[None])[0], axis=1)
            j = int(np.argmin(d))
            players.append(Blob(u=x + w / 2.0, v=float(y + h - 1), area=int(area), w=int(w), h=int(h), colour=col,
                                label=self.labels[j] if d[j] < 45 else "other", score=float(d[j])))
        balls: list[Blob] = []
        nb, lab_b, st_b, c_b = cv2.connectedComponentsWithStats(white, connectivity=8)
        for k in range(1, nb):
            x, y, w, h, area = st_b[k]
            if area < 3 or area > 150 or w > 2.5 * h + 2 or h > 2.5 * w + 2:
                continue
            u, v = c_b[k]
            r_exp = 0.22 * self.px_per_m(u, v + 6)
            size = np.sqrt(area / np.pi)
            parent = lab_img[int(round(v)), int(round(u))]
            attached = bool(parent > 0 and big[parent])
            shape = min(w, h) / max(w, h)
            s = shape - abs(np.log(max(size, 0.5) / max(r_exp, 0.5))) - (0.6 if attached else 0.0)
            balls.append(Blob(u=float(u), v=float(v), area=int(area), w=int(w), h=int(h), label="ball", score=float(s)))
        return balls, players, fg
