"""The painted lines of a 105 x 68 pitch, as polylines in metres.

Line centres follow the painting rule the renderer uses: the outer lines' outer edges sit on the
boundary, so their centres are half a line width inside it.
"""

from __future__ import annotations

import numpy as np

from . import LENGTH, WIDTH

LINE_W = 0.12
INSET = LINE_W / 2
CIRCLE_R = 9.15
BOX_D, BOX_HALF = 16.5, 20.16
SIX_D, SIX_HALF = 5.5, 9.16
SPOT = 11.0


def _arc(cx, cy, r, a0, a1, step=0.5):
    n = max(int(abs(a1 - a0) * r / step), 8)
    a = np.linspace(a0, a1, n)
    return np.column_stack([cx + r * np.cos(a), cy + r * np.sin(a)])


def _seg(a, b):
    return np.array([a, b], float)


def polylines() -> list[np.ndarray]:
    """Every painted line as an (n, 2) polyline in pitch metres."""
    L, W, i = LENGTH, WIDTH, INSET
    out = [
        _seg((i, i), (L - i, i)), _seg((i, W - i), (L - i, W - i)),
        _seg((i, i), (i, W - i)), _seg((L - i, i), (L - i, W - i)),
        _seg((L / 2, 0), (L / 2, W)),
        _arc(L / 2, W / 2, CIRCLE_R, 0, 2 * np.pi),
    ]
    a = np.arccos((BOX_D - SPOT) / CIRCLE_R)
    for side in (0, 1):
        sx = (lambda x: L - x) if side else (lambda x: x)
        for d, half in ((BOX_D, BOX_HALF), (SIX_D, SIX_HALF)):
            out.append(np.array([(sx(0), W / 2 - half), (sx(d), W / 2 - half), (sx(d), W / 2 + half), (sx(0), W / 2 + half)], float))
        if side:
            out.append(_arc(L - SPOT, W / 2, CIRCLE_R, np.pi - a, np.pi + a))
        else:
            out.append(_arc(SPOT, W / 2, CIRCLE_R, -a, a))
    return out


def sample(step: float = 0.5) -> np.ndarray:
    """Points every `step` metres along every painted line."""
    pts = []
    for pl in polylines():
        for a, b in zip(pl[:-1], pl[1:]):
            n = max(int(np.ceil(np.linalg.norm(b - a) / step)), 1)
            t = np.arange(n) / n
            pts.append(a[None, :] + (b - a)[None, :] * t[:, None])
        pts.append(pl[-1:])
    return np.concatenate(pts)


def grass_outline(margin: float = 8.0) -> np.ndarray:
    """The green ground the renderer lays around the pitch: the pitch plus a margin all round."""
    return np.array([(-margin, -margin), (LENGTH + margin, -margin), (LENGTH + margin, WIDTH + margin), (-margin, WIDTH + margin)], float)
