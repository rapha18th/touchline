"""A small Nelder-Mead minimiser, so the package needs only numpy and OpenCV."""

from __future__ import annotations

from typing import Callable

import numpy as np


def nelder_mead(f: Callable[[np.ndarray], float], x0, step, iters: int = 600, tol: float = 1e-6) -> tuple[np.ndarray, float]:
    """Minimise f from x0 with an initial simplex of the given per-axis steps. Returns (x, f(x))."""
    x0 = np.asarray(x0, float)
    n = len(x0)
    pts = [x0] + [x0 + np.eye(n)[i] * np.asarray(step, float)[i] if np.ndim(step) else x0 + np.eye(n)[i] * step for i in range(n)]
    S = np.array(pts)
    F = np.array([f(p) for p in S])
    for _ in range(iters):
        o = np.argsort(F)
        S, F = S[o], F[o]
        if abs(F[-1] - F[0]) <= tol * (abs(F[0]) + tol):
            break
        c = S[:-1].mean(axis=0)
        xr = c + (c - S[-1])
        fr = f(xr)
        if fr < F[0]:
            xe = c + 2.0 * (c - S[-1])
            fe = f(xe)
            S[-1], F[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < F[-2]:
            S[-1], F[-1] = xr, fr
        else:
            xc = c + 0.5 * (S[-1] - c) if fr >= F[-1] else c + 0.5 * (xr - c)
            fc = f(xc)
            if fc < min(fr, F[-1]):
                S[-1], F[-1] = xc, fc
            else:
                S[1:] = S[0] + 0.5 * (S[1:] - S[0])
                F[1:] = [f(p) for p in S[1:]]
    i = int(np.argmin(F))
    return S[i], float(F[i])
