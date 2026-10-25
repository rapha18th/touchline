"""Kicks from the ball's path: when the ball leaves a player, where it goes, how fast, and whether it was
a pass or a shot.

The ball is followed in pixels. A kick starts when its ground speed jumps and holds a straight line. The
flight ends when a player takes the ball, when the ball stops, or when its image path breaks. Each flight is
then fitted twice through the camera: rolling on the ground (z fixed at the ball's radius, a quadratic path)
or in the air (a straight path in plan, a parabola in height, starting from the ground at the kick). The
model with the smaller pixel residual wins. Distance and speed come from the chosen model.
"""

from __future__ import annotations

import numpy as np

from .geometry import ground_at_height

BALL_Z = 0.22            # the rendered ball's centre when it rests on the grass
KICK_SPEED = 10.0        # m/s held over the onset window
GOAL_Y, GOAL_HALF = 34.0, 3.66


def ground_track(ball_px: list, P: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(T, 2) ground positions of the ball as if it lay on the grass, NaN where unseen; and pixels."""
    T = len(ball_px)
    uv = np.full((T, 2), np.nan)
    for t, b in enumerate(ball_px):
        if b is not None:
            uv[t] = b
    g = np.full((T, 2), np.nan)
    ok = ~np.isnan(uv[:, 0])
    if ok.any():
        g[ok] = ground_at_height(P, uv[ok], BALL_Z)
    return g, uv


def fill_short(a: np.ndarray, max_gap: int = 4) -> np.ndarray:
    """Linear fill of NaN runs no longer than max_gap between two known rows."""
    a = a.copy()
    ok = np.nonzero(~np.isnan(a[:, 0]))[0]
    for s, e in zip(ok[:-1], ok[1:]):
        if 1 < e - s <= max_gap + 1:
            w = (np.arange(s + 1, e) - s) / (e - s)
            a[s + 1:e] = a[s] * (1 - w[:, None]) + a[e] * w[:, None]
    return a


def _burst(g: np.ndarray, step: np.ndarray, t: int, k: int, fps: float, speed: float, straight: float,
           even: float) -> bool:
    seg = g[t:t + k + 1]
    st = step[t + 1:t + k + 1]
    if np.isnan(seg).any():
        return False
    disp = float(np.linalg.norm(seg[-1] - seg[0]))
    return disp / (k / fps) >= speed and disp >= straight * float(st.sum()) and st.min() >= even * disp / k


def next_onset(g: np.ndarray, fps: float, start: int = 0) -> int | None:
    """The first kick frame at or after start: the ball sits there and then moves fast, evenly and in a
    straight line, for five steps at KICK_SPEED or for three steps at a shot's pace (a blocked shot
    flies only a few frames)."""
    T = len(g)
    step = np.r_[np.nan, np.linalg.norm(np.diff(g, axis=0), axis=1)]        # step[t]: t-1 to t
    for t in range(start, T - 3):
        if (t < T - 5 and _burst(g, step, t, 5, fps, KICK_SPEED, 0.9, 0.4)) or _burst(g, step, t, 3, fps, 16.0, 0.9, 0.5):
            # The kick frame: walk back while the step before is still part of the same burst.
            t0 = t
            while t0 > start and not np.isnan(step[t0]) and step[t0] > 0.7 * step[t + 1] and t - t0 < 3:
                t0 -= 1
            return t0
    return None


def _players_at(players: list, t: int) -> np.ndarray:
    if t < 0 or t >= len(players) or not players[t]:
        return np.zeros((0, 2))
    return np.array([[p[0], p[1]] for p in players[t]], float)


def _nearest_player(players: list, t: int, xy, reach: float):
    best, bd = None, reach
    for p in players[t] if 0 <= t < len(players) else []:
        d = float(np.hypot(p[0] - xy[0], p[1] - xy[1]))
        if d < bd:
            best, bd = p, d
    return best, bd


def flight_end(t0: int, g: np.ndarray, uv: np.ndarray, players: list, fps: float, max_s: float = 4.0) -> int:
    """Last frame of the flight that starts at t0.

    The kick frame itself is left out of the image-path check: at the kick the ball is still at the
    kicker's feet, a little off the line it then flies on."""
    T = len(g)
    stop = min(T - 1, t0 + int(max_s * fps))
    kicker, _ = _nearest_player(players, t0, g[t0] if not np.isnan(g[t0, 0]) else g[min(t0 + 1, T - 1)], 2.5)
    seen: list[int] = []
    prev = t0 if not np.isnan(g[t0, 0]) else None
    strikes, last_good, gap, peak = 0, t0, 0, 0.0
    for t in range(t0 + 1, stop + 1):
        if np.isnan(uv[t, 0]):
            gap += 1
            if gap > 5:
                return _bridge(t0, last_good, g, uv, fps, stop)
            continue
        gap = 0
        # Image path: predict this pixel from a quadratic through the last observed flight frames.
        if len(seen) >= 3:
            ts = np.array(seen[-8:], float)
            A = np.column_stack([np.ones_like(ts), ts - t, (ts - t) ** 2])
            coef, *_ = np.linalg.lstsq(A, uv[seen[-8:]], rcond=None)
            lastv = np.linalg.norm(uv[seen[-1]] - uv[seen[-2]]) / max(seen[-1] - seen[-2], 1)
            if np.linalg.norm(uv[t] - coef[0]) > 2.5 + 0.35 * lastv * (t - seen[-1]):
                strikes += 1
                if strikes >= 2:
                    return last_good
                continue
        strikes = 0
        # Ground speed: a ball that has stopped is no longer in flight.
        if prev is not None:
            v = float(np.linalg.norm(g[t] - g[prev])) * fps / (t - prev)
            peak = max(peak, v)
            if t - t0 >= 3 and v < max(1.5, 0.12 * peak):
                return last_good
        seen.append(t)
        prev = t
        last_good = t
        # Reception: a player other than the kicker is at the ball and the ball stays with them.
        if t - t0 >= 3:
            p, _ = _nearest_player(players, t, g[t], 1.2)
            if p is not None and (kicker is None or p[3] != kicker[3] or t - t0 > 12):
                # ... and moves with them: close, and at about their pace.
                stay, last = 0, (g[t], np.array(p[:2]))
                for s_ in range(t + 1, min(t + 4, T)):
                    q = [r for r in players[s_] if r[3] == p[3]]
                    if not q:
                        continue
                    pq = np.array(q[0][:2])
                    if np.isnan(g[s_, 0]):
                        stay += 1
                        continue
                    rel = np.linalg.norm((g[s_] - last[0]) - (pq - last[1]))
                    if np.hypot(*(g[s_] - pq)) < 1.4 and rel < 0.35 * (s_ - t):
                        stay += 1
                if stay >= 2:
                    # The ball arrives where the player stands: end on the nearest approach.
                    best, bd = t, np.inf
                    for s_ in range(t, min(t + 5, T)):
                        q = [r for r in players[s_] if r[3] == p[3]]
                        if q and not np.isnan(uv[s_, 0]):
                            d = float(np.hypot(g[s_, 0] - q[0][0], g[s_, 1] - q[0][1]))
                            if d < bd:
                                best, bd = s_, d
                    return best
    return last_good


def _bridge(t0: int, last: int, g: np.ndarray, uv: np.ndarray, fps: float, stop: int) -> int:
    """The ball went out of sight in flight (behind a player). If it comes back within 1.5 s on the line it
    was travelling, ahead of where it vanished, the flight ran on to there."""
    seen = [t for t in range(t0 + 1, last + 1) if not np.isnan(g[t, 0])]
    if len(seen) < 2:
        return last
    a, b = g[seen[0]], g[last]
    d = b - a
    n = float(np.linalg.norm(d))
    if n < 0.5:
        return last
    d /= n
    for t in range(last + 1, min(stop, last + int(1.5 * fps)) + 1):
        if np.isnan(uv[t, 0]) or np.isnan(g[t, 0]):
            continue
        r = g[t] - b
        along, across = float(r @ d), float(abs(r[0] * d[1] - r[1] * d[0]))
        return t if (along > 0 and across < 1.5 + 0.1 * along) else last
    return last


def _rows(P: np.ndarray, uv: np.ndarray):
    """Per observation, the two projective constraint rows (P0 - u P2) and (P1 - v P2), (n, 2, 4)."""
    return np.stack([P[0][None] - uv[:, :1] * P[2][None], P[1][None] - uv[:, 1:2] * P[2][None]], axis=1)


def fit_ground(P: np.ndarray, tau: np.ndarray, uv: np.ndarray) -> tuple[np.ndarray, float]:
    """Rolling ball: z = BALL_Z, x and y quadratic in time. Returns coefficients (3, 2) and pixel RMS."""
    R = _rows(P, uv)                                     # (n, 2, 4)
    basis = np.column_stack([np.ones_like(tau), tau, tau ** 2])          # (n, 3)
    A = np.concatenate([R[:, :, 0:1] * basis[:, None, :], R[:, :, 1:2] * basis[:, None, :]], axis=2).reshape(-1, 6)
    b = -(R[:, :, 2] * BALL_Z + R[:, :, 3]).reshape(-1)
    w = np.ones(len(A))
    for _ in range(3):
        th, *_ = np.linalg.lstsq(A * w[:, None], b * w, rcond=None)
        X = _eval_ground(th, tau)
        depth = np.c_[X, np.ones(len(X))] @ P[2]
        w = np.repeat(1.0 / np.maximum(np.abs(depth), 1e-6), 2)
    return th.reshape(2, 3).T, _rms(P, X, uv)


def _eval_ground(th, tau):
    c = np.asarray(th).reshape(2, 3)
    basis = np.column_stack([np.ones_like(tau), tau, tau ** 2])
    return np.column_stack([basis @ c[0], basis @ c[1], np.full(len(tau), BALL_Z)])


def fit_air(P: np.ndarray, tau: np.ndarray, uv: np.ndarray) -> tuple[np.ndarray, float]:
    """Ball in the air: x, y linear in time, z = BALL_Z + e t + k t^2 (on the grass at the kick, t = 0).
    Returns [ax, ay, bx, by, e, k] and pixel RMS."""
    R = _rows(P, uv)
    one = np.ones_like(tau)
    cols = [R[:, :, 0] * one[:, None], R[:, :, 1] * one[:, None], R[:, :, 0] * tau[:, None], R[:, :, 1] * tau[:, None],
            R[:, :, 2] * tau[:, None], R[:, :, 2] * (tau ** 2)[:, None]]
    A = np.stack(cols, axis=2).reshape(-1, 6)
    b = -(R[:, :, 2] * BALL_Z + R[:, :, 3]).reshape(-1)
    w = np.ones(len(A))
    for _ in range(3):
        th, *_ = np.linalg.lstsq(A * w[:, None], b * w, rcond=None)
        X = eval_air(th, tau)
        depth = np.c_[X, np.ones(len(X))] @ P[2]
        w = np.repeat(1.0 / np.maximum(np.abs(depth), 1e-6), 2)
    return th, _rms(P, X, uv)


def eval_air(th, tau):
    ax, ay, bx, by, e, k = th
    return np.column_stack([ax + bx * tau, ay + by * tau, BALL_Z + e * tau + k * tau ** 2])


def _rms(P, X, uv) -> float:
    q = np.c_[X, np.ones(len(X))] @ P.T
    return float(np.sqrt(np.mean(np.sum((q[:, :2] / q[:, 2:3] - uv) ** 2, axis=1))))


def release_speed(g: np.ndarray, tau: np.ndarray, n: int = 8) -> float:
    """Speed at the kick from a quadratic through the first n ground positions."""
    m = min(n, len(tau))
    if m < 3:
        return float(np.linalg.norm(g[-1] - g[0]) / max(tau[-1] - tau[0], 1e-6))
    A = np.column_stack([np.ones(m), tau[:m], tau[:m] ** 2]) if m >= 4 else np.column_stack([np.ones(m), tau[:m]])
    c, *_ = np.linalg.lstsq(A, g[:m], rcond=None)
    return float(np.linalg.norm(c[1]))


def keeper_sides(players: list) -> dict:
    """Median x of each keeper over the clip, where seen."""
    xs = {"home_gk": [], "away_gk": []}
    for fr in players:
        for p in fr:
            if p[2] in xs:
                xs[p[2]].append(p[0])
    return {k: float(np.median(v)) for k, v in xs.items() if len(v) >= 5}


def classify(start, vel, end, speed: float, kicker: str | None, receiver: str | None, sides: dict,
             aerial_apex: float = 0.0) -> str:
    """Shot if the ball heads at a goal from within 40 m, fast, and the kicker is not playing it back to their
    own keeper or onto a teammate."""
    vx, vy = vel
    if abs(vx) < 1e-6:
        return "pass"
    goal_x = 105.0 if vx > 0 else 0.0
    dx = goal_x - start[0]
    if abs(dx) > 40:
        return "pass"
    y_cross = start[1] + dx / vx * vy
    # Headers reach the goal slower: allow them from close in.
    if abs(y_cross - GOAL_Y) > 11 or speed < (9 if abs(dx) < 18 else 14):
        return "pass"
    team = kicker.replace("_gk", "") if kicker else None
    own = sides.get(f"{team}_gk") if team else None
    if own is not None and abs(own - goal_x) < 30:
        return "pass"
    if aerial_apex > 4.5:
        return "pass"                        # a lofted ball into the box is a cross, not a shot
    rteam = receiver.replace("_gk", "") if receiver else None
    if rteam is not None and rteam == team and abs(goal_x - end[0]) > 8:
        return "pass"
    return "shot"


def detect_events(obs: dict, P, H=None) -> dict:
    P = np.asarray(P, float)
    fps = float(obs["fps"])
    players = obs["players"]
    g, uv = ground_track(obs["ball_px"], P)
    gf = fill_short(g)
    uvf = fill_short(uv)
    sides = keeper_sides(players)
    kicks = []
    pos = 0
    while True:
        t0 = next_onset(gf, fps, pos)
        if t0 is None:
            break
        t1 = flight_end(t0, gf, uvf, players, fps)
        pos = max(t1, t0 + 3)
        fr = np.array([t for t in range(t0 + 1, t1 + 1) if not np.isnan(uv[t, 0])])
        if len(fr) < 3:
            continue
        tau = (fr - t0) / fps
        cg, rg = fit_ground(P, tau, uv[fr])
        ca, ra = fit_air(P, tau, uv[fr])
        tt = np.linspace(0, (t1 - t0) / fps, 50)
        za = eval_air(ca, tt)[:, 2]
        # In the air only if that model explains the pixels clearly better and is physically sane.
        aerial = bool(len(fr) >= 6 and ra < 0.7 * rg and 0.6 < za.max() < 30 and ca[5] < 0
                      and np.hypot(ca[2], ca[3]) < 45)
        T1 = (t1 - t0) / fps
        if aerial:
            start = np.array([ca[0], ca[1]])
            end = start + np.array([ca[2], ca[3]]) * T1
            vel = np.array([ca[2], ca[3]])
            speed = float(np.linalg.norm(vel))
            height = float(za.max())
        else:
            gx = g[fr]
            start = cg[0]
            end = gx[-1]
            speed = release_speed(gx, tau)
            vel = cg[1] if np.linalg.norm(cg[1]) > 0 else end - start
            height = BALL_Z
        kp, _ = _nearest_player(players, t0, start, 2.5)
        rp, _ = _nearest_player(players, t1, end, 2.0)
        kicker = kp[2] if kp else None
        receiver = rp[2] if rp else None
        kind = classify(start, vel, end, speed, kicker, receiver, sides, height if aerial else 0.0)
        kicks.append({"frame": int(t0), "end": int(t1), "type": kind, "aerial": aerial,
                      "length_m": round(float(np.linalg.norm(end - start)), 2), "speed_kmh": round(speed * 3.6, 1),
                      "max_height_m": round(height, 2), "start": [round(float(v), 2) for v in start],
                      "end_xy": [round(float(v), 2) for v in end], "kicker": kicker, "receiver": receiver,
                      "fit_px": {"ground": round(rg, 2), "air": round(ra, 2)}, "n_obs": int(len(fr)),
                      **({"air": [round(float(v), 4) for v in ca]} if aerial else {})})
    return {"kicks": kicks, "keepers": sides}
