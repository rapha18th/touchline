"""Pitch geometry, the shot model and the possession value surface.

All maths here works in an attacking frame: the team in question attacks the goal at
x = LENGTH, its own goal sits at x = 0, y runs 0..WIDTH. `to_abs` maps that frame onto the
absolute pitch, where the home side attacks +x in period 1.
"""

from __future__ import annotations

import math

import numpy as np

LENGTH = 105.0
WIDTH = 68.0
GOAL_HALF = 3.66
GOAL_Y = WIDTH / 2
BOX_DEPTH = 16.5
BOX_HALF = 20.16
SIX_DEPTH = 5.5
PENALTY_SPOT = 11.0
FPS = 25


def to_abs(attacks_positive: bool, x, y):
    """Attacking-frame coordinates to absolute pitch coordinates."""
    if attacks_positive:
        return x, y
    return LENGTH - x, WIDTH - y


def goal_angle(x: float, y: float) -> float:
    """Angle in radians subtended by the goal mouth from (x, y), attacking frame."""
    dx = LENGTH - x
    a1 = math.atan2(GOAL_Y + GOAL_HALF - y, dx)
    a2 = math.atan2(GOAL_Y - GOAL_HALF - y, dx)
    return abs(a1 - a2)


def goal_distance(x: float, y: float) -> float:
    return math.hypot(LENGTH - x, GOAL_Y - y)


def xg(x: float, y: float, *, header: bool = False, pressure: float = 0.0, skill: float = 0.5) -> float:
    """Chance a shot from (x, y) scores.

    Distance and visible angle drive the logit. Headers cost about a third of the chance,
    a defender on top of the shooter (pressure 1) costs about a third more, and finishing
    skill moves the logit by up to 0.3 either way.
    """
    d = goal_distance(x, y)
    a = goal_angle(x, y)
    logit = -0.6 - 0.12 * d + 1.3 * a
    if header:
        logit -= 0.8
    logit -= 0.55 * pressure
    logit += 0.6 * (skill - 0.5)
    return 1.0 / (1.0 + math.exp(-logit))


def in_box(x: float, y: float) -> bool:
    return x >= LENGTH - BOX_DEPTH and abs(y - GOAL_Y) <= BOX_HALF


def _build_value_grid(nx: int = 106, ny: int = 69) -> np.ndarray:
    """Possession value by location: the chance this possession ends in a goal.

    A smooth surface shaped like published expected-threat maps: near zero in the own
    half, rising through the final third, peaking in the six-yard box.
    """
    grid = np.zeros((nx, ny))
    for i in range(nx):
        for j in range(ny):
            x, y = i * LENGTH / (nx - 1), j * WIDTH / (ny - 1)
            shot = xg(x, y) if x > LENGTH * 0.55 else 0.0
            build = 0.006 + 0.06 * (x / LENGTH) ** 2.2
            grid[i, j] = build + 0.3 * shot
    return grid


VALUE = _build_value_grid()


def value(x: float, y: float) -> float:
    i = int(round(min(max(x, 0.0), LENGTH) / LENGTH * (VALUE.shape[0] - 1)))
    j = int(round(min(max(y, 0.0), WIDTH) / WIDTH * (VALUE.shape[1] - 1)))
    return float(VALUE[i, j])


def clamp_pitch(x: float, y: float, margin: float = 0.3) -> tuple[float, float]:
    return min(max(x, margin), LENGTH - margin), min(max(y, margin), WIDTH - margin)


def zone(x: float, y: float) -> str:
    """Coarse zone name in the attacking frame, used in event qualifiers."""
    third = "defensive" if x < LENGTH / 3 else "middle" if x < 2 * LENGTH / 3 else "final"
    lane = "left" if y > WIDTH * 0.66 else "right" if y < WIDTH * 0.34 else "central"
    return f"{third}_{lane}"
