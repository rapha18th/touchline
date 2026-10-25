"""The match engine.

A 25 Hz loop moves 22 players and the ball. Off the ball, players hold a formation shape
that slides with the ball, press it, or run in behind; on the ball, the carrier weighs
passes, carries, shots, crosses and clearances by expected possession value and picks one
through a softmax shaped by the team's style. Passes and shots resolve from physical
races (who reaches the lane first), so outcomes, events and tracking stay consistent.

Every event carries a `truth` block with the engine's own probabilities (pass success,
shot xG). Downstream services estimate these from tracking alone; the truth block grades
them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import pitch as P
from .squads import FORMATIONS, ROLE_GROUP, Team, make_fixture

DT = 1.0 / P.FPS
N = 22
EASE = 1.4   # ground passes decelerate: u = 1 - (1 - tau) ** EASE

# League-level calibration dials. engine/tools/calibrate.py tunes them against the realism
# report through a gate; docs/REALISM.md records how each value was reached.
LOSS = 0.04                   # value of a possession given away, before the opponent's chance
RESIST_FINAL_THIRD = 0.7      # pass success multiplier for targets in the final third
RESIST_SHOOTING_RANGE = 0.28  # pass and carry success multiplier for targets within 20 m of goal
BOX_RESIST = 0.7              # extra pass success multiplier for targets in the box
SHOT_VALUE = 0.76              # a shot's worth per unit of xG; the risk dial adds the rest
SHOT_RANGE = 30.0             # metres: the longest shot a carrier considers
HEADER_SHOT = 0.55            # chance a won header near goal becomes a shot
SAVED_SHARE = 0.37             # share of unblocked misses that the keeper saves
FINISH_LOGIT = -0.22            # shift on every chance's xG logit: the league's finishing level
BLOCK_SCALE = 1.1             # multiplier on the chance that bodies in the lane block a shot
FOUL_RATE = 1.03               # multiplier on the chance a challenge or tackle is a foul
CORNER_DEFLECT = 1.6          # multiplier on the chance a saved, blocked or missed shot goes behind for a corner
RESTART_PACE = 1.2            # multiplier on the dead time before throw-ins, goal kicks, corners, free kicks
SUB_STOP_S = 0.0              # seconds a substitution window adds to the stoppage
LATE_STALL = 0.0              # extra share of dead time when the restarting side leads after the hour
GOAL_STOP_S = 62.0            # mean seconds from a goal to the kickoff
TREATMENT = 0.12              # chance a foul stops play for treatment
YELLOW_SHARE = 0.21           # chance a foul by an unbooked player is booked
RED_STRAIGHT = 0.002          # chance a foul is a straight red
RED_SECOND = 0.035            # chance a foul by a booked player is a second yellow

DIALS = ("LOSS", "RESIST_FINAL_THIRD", "RESIST_SHOOTING_RANGE", "BOX_RESIST", "SHOT_VALUE", "SHOT_RANGE",
         "HEADER_SHOT", "SAVED_SHARE", "FINISH_LOGIT", "BLOCK_SCALE", "FOUL_RATE", "CORNER_DEFLECT", "RESTART_PACE", "SUB_STOP_S", "LATE_STALL", "GOAL_STOP_S", "TREATMENT",
         "YELLOW_SHARE", "RED_STRAIGHT", "RED_SECOND")


def dials() -> dict:
    """The current dial vector."""
    return {k: globals()[k] for k in DIALS}


def set_dials(values: dict) -> None:
    """Set dials by name. Unknown names raise, so a typo never runs silently on defaults."""
    unknown = sorted(set(values) - set(DIALS))
    if unknown:
        raise KeyError(f"unknown dials: {unknown}")
    for k, v in values.items():
        globals()[k] = float(v)


def _dials_from_env():
    """TOUCHLINE_DIALS='{"RESIST_SHOOTING_RANGE": 0.3}' overrides the dials for calibration sweeps."""
    import json
    import os
    raw = os.environ.get("TOUCHLINE_DIALS")
    if raw:
        set_dials(json.loads(raw))


_dials_from_env()


@dataclass
class Flight:
    kind: str            # pass, cross, clear, shot, loose
    start: np.ndarray    # xyz
    end: np.ndarray      # xyz
    f0: int
    frames: int
    height: float
    ease: float          # >1 decelerates along the ground
    resolve: dict        # what happens on arrival


class Match:
    def __init__(self, seed: int, home: Team | None = None, away: Team | None = None):
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        if home is None or away is None:
            home, away = make_fixture(seed)
        self.teams = [home, away]
        self.events: list[dict] = []
        self.frames: list[np.ndarray] = []
        self.period = 1
        self.f = 0                  # frame within the period
        self.gf = 0                 # global frame
        self.score = [0, 0]
        self.period_frames: list[int] = []

        self.team_of = np.array([0] * 11 + [1] * 11)
        self.roster = [t.players[i] for t in self.teams for i in range(11)]
        self.bench = [list(t.players[11:]) for t in self.teams]
        self.slot_role = [r for t in self.teams for (r, _, _) in FORMATIONS[t.formation]]
        self.slot_x01 = np.array([x for t in self.teams for (_, x, _) in FORMATIONS[t.formation]])
        self.slot_y01 = np.array([y for t in self.teams for (_, _, y) in FORMATIONS[t.formation]])
        self.is_gk = np.array([r == "GK" for r in self.slot_role])
        self.active = np.ones(N, bool)
        self.minutes_on = np.zeros(N)   # seconds on the pitch, drives fatigue
        self.yellow: dict[str, int] = {}
        self.subs_made = [0, 0]
        self.sub_windows = [0, 0]

        self.pos = np.zeros((N, 2))
        self.vel = np.zeros((N, 2))
        self.jit = np.zeros((N, 2))
        self.run = np.zeros((N, 2))
        self.run_until = np.zeros(N, int)
        self.stun_until = np.zeros(N, int)
        self.pressed_by: set[int] = set()

        self.ball = np.array([P.LENGTH / 2, P.WIDTH / 2, 0.0])
        self.mode = "dead"          # held, flight, dead
        self.carrier: int | None = None
        self.poss = 0
        self.flight: Flight | None = None
        self.decide_at = 0
        self.carry_target: np.ndarray | None = None
        self.carry_start: tuple[np.ndarray, int] | None = None
        self.receiver: int | None = None
        self.restart: dict | None = None
        self.last_touch_team = 0
        self.momentum = np.zeros(2)
        self.chain = 0              # possession counter
        self.chain_team = -1
        self.possessions = [0, 0]
        self.loose_ball: dict | None = None
        self.carry_plan: dict | None = None
        self.ball_smooth = self.ball[:2].copy()

    # ----- frames and coordinates -------------------------------------------------------

    def attacks_pos(self, team: int) -> bool:
        return (team == 0) == (self.period == 1)

    def tf(self, team: int, xy):
        """Absolute <-> team attacking frame (the map is its own inverse)."""
        xy = np.asarray(xy, float)
        if self.attacks_pos(team):
            return xy.copy()
        out = xy.copy()
        out[..., 0] = P.LENGTH - xy[..., 0]
        out[..., 1] = P.WIDTH - xy[..., 1]
        return out

    def clock(self) -> float:
        return self.f * DT

    def pid(self, slot: int | None) -> str | None:
        return None if slot is None else self.roster[slot].id

    def style(self, team: int):
        s = self.teams[team].style
        press, direct, width, tempo, risk = s.press, s.directness, s.width, s.tempo, s.risk
        mins = (self.period - 1) * 45 + self.clock() / 60
        diff = self.score[team] - self.score[1 - team]
        if mins > 60 and diff < 0:
            press, direct, risk = press + 0.15, direct + 0.15, risk + 0.2
        elif mins > 65 and diff > 0:
            press, tempo, risk = press - 0.15, tempo - 0.1, risk - 0.15
        press += 0.15 * self.momentum[team]
        clip = lambda v: float(min(max(v, 0.0), 1.0))
        return clip(press), clip(direct), clip(width), clip(tempo), clip(risk)

    def strength(self, team: int) -> float:
        return self.teams[team].strength + (0.04 if team == 0 else 0.0)

    # ----- events -----------------------------------------------------------------------

    def emit(self, type_: str, slot: int | None = None, team: int | None = None, *, outcome=None,
             start=None, end=None, t_end=None, q: dict | None = None, truth: dict | None = None) -> dict:
        if team is None and slot is not None:
            team = int(self.team_of[slot])
        ev = {
            "id": len(self.events),
            "period": self.period,
            "t": round(self.clock(), 2),
            "frame": self.gf,
            "team": None if team is None else self.teams[team].id,
            "player": self.pid(slot),
            "type": type_,
            "outcome": outcome,
            "x": None if start is None else round(float(start[0]), 2),
            "y": None if start is None else round(float(start[1]), 2),
            "end_x": None if end is None else round(float(end[0]), 2),
            "end_y": None if end is None else round(float(end[1]), 2),
            "t_end": round(t_end, 2) if t_end is not None else None,
            "possession": self.chain,
            "score": list(self.score),
            "q": q or {},
        }
        if truth:
            ev["truth"] = truth
        self.events.append(ev)
        return ev

    def set_possession(self, team: int):
        if team != self.chain_team:
            self.chain += 1
            self.chain_team = team
            self.possessions[team] += 1
            self.start_runs(team)
        self.poss = team

    # ----- movement ---------------------------------------------------------------------

    def start_runs(self, team: int):
        """Attackers commit to runs in behind for a few seconds when a possession starts."""
        for s in range(N):
            if self.team_of[s] != team or not self.active[s]:
                continue
            g = ROLE_GROUP[self.slot_role[s]]
            p_run = {"ATT": 0.55, "MID": 0.25, "DEF": 0.1, "GK": 0.0}[g]
            if self.rng.random() < p_run:
                ahead = self.rng.uniform(5, 14)
                side = self.rng.normal(0, 5)
                self.run[s] = [ahead, side]
                self.run_until[s] = self.f + int(self.rng.uniform(2, 5) * P.FPS)

    def targets(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        tgt = np.zeros((N, 2))
        urgent = np.zeros(N, bool)
        brisk = np.zeros(N, bool)
        for team in (0, 1):
            idx = np.where(self.team_of == team)[0]
            press, direct, width, tempo, risk = self.style(team)
            bx, by = self.tf(team, self.ball_smooth)
            in_poss = (team == self.poss) and self.mode != "dead" or (self.mode == "dead" and self.restart and self.restart["team"] == team)
            if in_poss:
                # In possession the shape stretches: forwards stay high as outlets even while
                # the ball is played out from the back.
                x_b = min(max(bx - 38 + 8 * direct, 6), 58)
                x_f = min(max(bx + 18, 55 + 10 * direct), 96)
                spread = P.WIDTH * (0.6 + 0.3 * width)
                shift = 0.25
            else:
                # The back line follows the ball, but no higher than the team's pressing style
                # allows: a low block drops to about 30 m, a high press holds near 50 m.
                x_b = min(max(bx - 17 - 11 * (1 - press), 6), 28 + 22 * press)
                x_f = x_b + 33 - 6 * (1 - press)
                spread = 42.0 if bx > 40 else 32.0
                shift = 0.45 if bx > 40 else 0.3
            x = x_b + self.slot_x01[idx] * (x_f - x_b)
            y = P.GOAL_Y + (self.slot_y01[idx] - 0.5) * spread + shift * (by - P.GOAL_Y)
            gk = self.is_gk[idx]
            x[gk] = 2.5 + 0.07 * bx
            y[gk] = P.GOAL_Y + 0.22 * (by - P.GOAL_Y)
            local = np.stack([x, y], 1)
            running = (self.run_until[idx] > self.f) & in_poss
            local[running] += self.run[idx][running]
            local[:, 0] = np.minimum(local[:, 0], 103.0)
            local += self.jit[idx] * (~gk)[:, None]
            local[:, 0] = np.clip(local[:, 0], 0.5, P.LENGTH - 0.5)
            local[:, 1] = np.clip(local[:, 1], 0.5, P.WIDTH - 0.5)
            tgt[idx] = self.tf(team, local)
            urgent[idx[running]] = True

            # Out of possession in the own half: recover at speed, mark runners near the box.
            if not in_poss and self.mode != "dead" and bx < 50:
                brisk[idx[~self.is_gk[idx]]] = True
                if bx < 42:
                    self.mark(team, idx, tgt, urgent)

            # Out of possession: the nearest one or two players go to the ball.
            if not in_poss and self.mode != "dead":
                d = np.linalg.norm(self.pos[idx] - self.ball[:2], axis=1)
                d[~self.active[idx] | self.is_gk[idx]] = 1e9
                order = np.argsort(d)
                # bx: ball distance from this team's own goal line.
                n_press = 2 if (bx > 55 and press > 0.55) or bx < 35 else 1
                engage = d[order[0]] < 10 or bx < 40 + 60 * press
                if engage:
                    for k in range(n_press):
                        s = idx[order[k]]
                        if self.stun_until[s] > self.f:
                            continue
                        goal_abs = self.tf(team, [0.0, P.GOAL_Y])
                        dirg = goal_abs - self.ball[:2]
                        dirg /= np.linalg.norm(dirg) + 1e-9
                        tgt[s] = self.ball[:2] + dirg * (0.8 if k == 0 else 4.0)
                        urgent[s] = True
        if self.mode == "held" and self.carrier is not None and self.carry_target is not None:
            tgt[self.carrier] = self.carry_target
            urgent[self.carrier] = True
        if self.mode == "flight" and self.receiver is not None:
            tgt[self.receiver] = self.flight.end[:2]
            urgent[self.receiver] = True
        if self.mode == "flight" and self.flight is not None:
            chaser = self.flight.resolve.get("chaser")
            if chaser is not None:
                tgt[chaser] = self.flight.resolve.get("meet", self.flight.end[:2])
                urgent[chaser] = True
        if self.mode == "loose" and self.loose_ball is not None:
            for k in ("winner", "rival"):
                s = self.loose_ball[k]
                if s is not None:
                    tgt[s] = self.loose_ball["xy"]
                    urgent[s] = True
        if self.mode == "dead" and self.restart is not None and self.restart["kind"] == "kickoff":
            tgt[:] = self.kickoff_layout()
        if self.mode == "dead" and self.restart is not None:
            taker = self.restart["taker"]
            tgt[taker] = self.restart["spot"]
            urgent[taker] = True
            if self.restart["kind"] == "corner":
                self.corner_shape(tgt)
        return tgt, urgent, brisk

    def mark(self, team, idx, tgt, urgent):
        """Goal-side marking of attackers within 32 m of this team's goal."""
        att = np.where((self.team_of != team) & self.active & ~self.is_gk)[0]
        att_tf = self.tf(team, self.pos[att])
        near = att[att_tf[:, 0] < 32]
        if len(near) == 0:
            return
        goal = self.tf(team, np.array([0.0, P.GOAL_Y]))
        order = near[np.argsort(np.linalg.norm(self.pos[near] - goal, axis=1))]
        free = [s for s in idx if self.active[s] and not self.is_gk[s]]
        for a in order:
            if not free:
                break
            if a == self.carrier:
                continue
            d = [np.linalg.norm(self.pos[s] - self.pos[a]) for s in free]
            k = int(np.argmin(d))
            s = free.pop(k)
            v = goal - self.pos[a]
            v /= np.linalg.norm(v) + 1e-9
            tgt[s] = self.pos[a] + v * 1.6
            if np.linalg.norm(tgt[s] - self.pos[s]) > 4.0:
                urgent[s] = True

    def corner_shape(self, tgt):
        att = self.restart["team"]
        for s in range(N):
            if not self.active[s] or self.is_gk[s] or s == self.restart["taker"]:
                continue
            team = self.team_of[s]
            role = ROLE_GROUP[self.slot_role[s]]
            if team == att and role in ("ATT", "DEF") and self.slot_role[s] in ("ST", "CB", "AM", "LW", "RW"):
                loc = np.array([P.LENGTH - self.rng.uniform(5, 12), P.GOAL_Y + self.rng.uniform(-8, 8)])
                tgt[s] = self.tf(att, loc)
            elif team != att and role in ("DEF", "MID"):
                loc = np.array([P.LENGTH - self.rng.uniform(3, 10), P.GOAL_Y + self.rng.uniform(-9, 9)])
                tgt[s] = self.tf(att, loc)

    def move(self):
        tgt, urgent, brisk = self.targets()
        fatigue = 1.0 - np.array([0.1 * min(m / 5400.0, 1.2) * (1.25 - p.stamina) for m, p in zip(self.minutes_on, self.roster)])
        vmax = np.array([p.pace for p in self.roster]) * fatigue
        cap = np.where(urgent, 0.96, np.where(brisk, 0.62, 0.42)) * vmax
        if self.carrier is not None and self.mode == "held":
            cap[self.carrier] = 0.72 * vmax[self.carrier]
        gain = np.where(urgent, 2.2, np.where(brisk, 1.3, 0.7))
        delta = tgt - self.pos
        dist = np.linalg.norm(delta, axis=1) + 1e-9
        speed = np.minimum(cap, gain * dist)
        desired = delta / dist[:, None] * speed[:, None]
        amax = np.where(urgent, 4.5, np.where(brisk, 3.2, 2.2))
        dv = desired - self.vel
        dvn = np.linalg.norm(dv, axis=1) + 1e-9
        step = np.minimum(dvn, amax * DT)
        self.vel += dv / dvn[:, None] * step[:, None]
        self.vel[~self.active] = 0
        self.pos += self.vel * DT
        self.pos[:, 0] = np.clip(self.pos[:, 0], -2, P.LENGTH + 2)
        self.pos[:, 1] = np.clip(self.pos[:, 1], -2, P.WIDTH + 2)
        # Slow random wander keeps shapes alive.
        self.jit += -self.jit * DT / 6.0 + self.rng.normal(0, 0.8, (N, 2)) * math.sqrt(DT)
        self.minutes_on[self.active] += DT

    # ----- ball -------------------------------------------------------------------------

    def ball_follow(self):
        if self.mode == "held" and self.carrier is not None:
            p = self.pos[self.carrier]
            v = self.vel[self.carrier]
            sp = np.linalg.norm(v)
            ahead = v / sp * 0.7 if sp > 0.3 else np.zeros(2)
            self.ball[:2] = p + ahead
            self.ball[2] = 0.11
        elif self.mode == "flight" and self.flight is not None:
            fl = self.flight
            tau = min((self.f - fl.f0) / max(fl.frames, 1), 1.0)
            u = 1 - (1 - tau) ** fl.ease if fl.ease > 1 else tau
            self.ball[:2] = fl.start[:2] + (fl.end[:2] - fl.start[:2]) * u
            self.ball[2] = fl.start[2] + (fl.end[2] - fl.start[2]) * tau + 4 * fl.height * tau * (1 - tau)

    def launch(self, kind, start2, end3, seconds, height, ease, resolve):
        start = np.array([start2[0], start2[1], 0.11])
        frames = max(int(round(seconds * P.FPS)), 2)
        self.flight = Flight(kind, start, np.asarray(end3, float), self.f, frames, height, ease, resolve)
        self.mode = "flight"
        self.carrier = None
        self.carry_target = None

    # ----- the carrier's choice ---------------------------------------------------------

    def give(self, slot: int, *, first_touch: bool = True):
        team = int(self.team_of[slot])
        self.set_possession(team)
        self.mode = "held"
        self.carrier = slot
        self.receiver = None
        self.flight = None
        self.last_touch_team = team
        self.pressed_by = set()
        self.carry_plan = None
        _, _, _, tempo, _ = self.style(team)
        hold = self.rng.uniform(0.35, 1.1) * (1.35 - 0.6 * tempo) + 0.15
        self.decide_at = self.f + int(hold * P.FPS)
        self.carry_target = self.pos[slot].copy()
        self.carry_start = (self.pos[slot].copy(), self.f)

    def pressure_on(self, slot: int) -> tuple[float, float, int]:
        """0..1 pressure on a player, nearest opponent distance, and that opponent's slot."""
        opp = np.where((self.team_of != self.team_of[slot]) & self.active)[0]
        d = np.linalg.norm(self.pos[opp] - self.pos[slot], axis=1)
        k = int(np.argmin(d))
        dist = float(d[k])
        return float(np.clip((4.0 - dist) / 3.0, 0, 1)), dist, int(opp[k])

    def pass_model(self, team, a, b, lofted, passer, pressure):
        """Success chance of a pass from a to b (team frame), and the likely interceptor."""
        L = float(np.linalg.norm(b - a))
        opp = np.where((self.team_of != team) & self.active)[0]
        o = self.tf(team, self.pos[opp])
        if lofted:
            secs = 0.9 + L / 21.0
        else:
            v0 = min(11.0 + 0.45 * L, 27.0)
            secs = EASE * L / v0
        skill = self.roster[passer].passing
        p_exec = 0.995 - L * 0.0036 * (1.3 - skill) - (0.07 if lofted else 0.0) - 0.12 * pressure
        p_exec += 0.05 * (self.strength(team) - 0.5) + 0.03 * self.momentum[team]
        s = np.linspace(0.12, 0.96, 8) if not lofted else np.array([0.88, 0.94, 1.0])
        pts = a[None, :] + (b - a)[None, :] * s[:, None]
        tb = (s if lofted else 1 - (1 - s) ** (1 / EASE)) * secs
        d = np.linalg.norm(o[:, None, :] - pts[None, :, :], axis=2)
        reach = 1.6 if lofted else 1.0
        to = np.maximum(d - reach, 0) / 6.5 + 0.3
        margin = tb[None, :] - to - 0.25
        p_int = 1 / (1 + np.exp(-6.0 * margin)) * 0.85
        p_each = p_int.max(axis=1)
        survive = float(np.prod(1 - p_each))
        k = int(np.argmax(p_each))
        best_s = float(s[int(np.argmax(p_int[k]))])
        p = max(min(p_exec, 0.995), 0.05) * survive
        near = int(np.sum(np.linalg.norm(o - b, axis=1) < 5.0))
        box = P.in_box(*b)
        crowd = 0.9 if b[0] < 70 else 0.75 if not box else 0.55
        p *= crowd ** near
        if b[0] > 70:
            p *= RESIST_FINAL_THIRD
        if P.goal_distance(*b) < 20:
            p *= RESIST_SHOOTING_RANGE
        if box:
            p *= BOX_RESIST                            # blocks, sliding defenders, the keeper's reach
            if P.goal_distance(*b) < 11:
                p *= 0.7                               # the keeper sweeps balls played near goal
        return p, secs, int(opp[k]), best_s, float(p_each[k])

    def options(self, slot: int):
        team = int(self.team_of[slot])
        press, direct, width, tempo, risk = self.style(team)
        a = self.tf(team, self.pos[slot])
        pressure, near_d, near_o = self.pressure_on(slot)
        opts = []
        mates = np.where((self.team_of == team) & self.active)[0]
        for m in mates:
            if m == slot:
                continue
            b = self.tf(team, self.pos[m])
            lead = self.vel[m] if self.attacks_pos(team) else -self.vel[m]
            L = float(np.linalg.norm(b - a))
            if L < 4 or L > 62:
                continue
            target = b + lead * min(L / 18.0, 1.2)
            target = np.array(P.clamp_pitch(*target, margin=0.8))
            lofted = L > 32
            p, secs, interceptor, s_int, p_int = self.pass_model(team, a, target, lofted, slot, pressure)
            offside = self.offside(team, m, target)
            gain = P.value(*target) - P.value(*a)
            ev = p * P.value(*target) - (1 - p) * (LOSS + 0.6 * P.value(P.LENGTH - target[0], P.WIDTH - target[1]))
            ev += 0.006 * direct * (target[0] - a[0]) / 10.0
            ev += 0.003 * (1 - risk) * (p - 0.85)
            if offside:
                ev -= 0.03
            opts.append(dict(kind="pass", to=m, target=target, p=p, secs=secs, lofted=lofted,
                             interceptor=interceptor, s_int=s_int, offside=offside, ev=ev, gain=gain))
        # Carry into space: toward goal, away from the nearest opponent.
        o_tf = self.tf(team, self.pos[near_o])
        away = a - o_tf
        away /= np.linalg.norm(away) + 1e-9
        fwd = np.array([1.0, 0.0])
        dirv = fwd * 0.7 + away * 0.5 * pressure + np.array([0, self.rng.normal(0, 0.3)])
        dirv /= np.linalg.norm(dirv)
        c = np.array(P.clamp_pitch(*(a + dirv * self.rng.uniform(4, 11)), margin=1.0))
        drib = self.roster[slot].dribbling
        opp_tf = self.tf(team, self.pos[np.where((self.team_of != team) & self.active)[0]])
        seg = c - a
        segL = np.linalg.norm(seg) + 1e-9
        rel = opp_tf - a
        along = np.clip(rel @ seg / segL, 0, segL)
        closest = a + seg / segL * along[:, None]
        dpath = np.linalg.norm(opp_tf - closest, axis=1)
        p_keep = float(np.prod(1 - 0.55 * np.exp(-dpath / 1.8) * (1.15 - drib)))
        p_keep *= 1 - pressure * 0.2 * (1.2 - drib)
        if c[0] > 75:
            p_keep *= 0.82 if not P.in_box(*c) else 0.6
        if P.goal_distance(*c) < 20:
            p_keep *= RESIST_SHOOTING_RANGE
        ev_c = p_keep * P.value(*c) - (1 - p_keep) * (LOSS + 0.6 * P.value(P.LENGTH - c[0], P.WIDTH - c[1]))
        opp_idx = np.where((self.team_of != team) & self.active)[0]
        k = int(np.argmin(dpath + 0.3 * np.abs(along - segL * 0.5)))
        opts.append(dict(kind="carry", target=c, p=p_keep, ev=ev_c, stopper=int(opp_idx[k]),
                         stop_frac=float(np.clip(along[k] / segL, 0.2, 0.9))))
        # Shot.
        dgoal = P.goal_distance(*a)
        if dgoal < SHOT_RANGE and P.goal_angle(*a) > 0.1:
            x = P.xg(*a, pressure=pressure, skill=self.roster[slot].finishing, bias=FINISH_LOGIT)
            # Bodies in the shooting lane block it; a shot that misses usually hands the ball
            # over, so it pays the same loss as a bad pass.
            p_block = self.block_chance(team, a)
            look = x * (1 - p_block)
            ev = look * (SHOT_VALUE + 0.3 * risk) - (1 - look) * 0.75 * LOSS
            opts.append(dict(kind="shot", xg=x, ev=ev, pressure=pressure))
        # Cross from wide in the final third.
        if a[0] > 78 and abs(a[1] - P.GOAL_Y) > 16:
            tgt = np.array([P.LENGTH - self.rng.uniform(5, 13), P.GOAL_Y + self.rng.normal(0, 5)])
            hx = P.xg(*tgt, header=True, bias=FINISH_LOGIT)
            opts.append(dict(kind="cross", target=tgt, ev=0.3 * hx * (0.8 + 0.4 * risk) - 0.7 * 0.8 * LOSS))
        # Clear it under pressure deep in the own half.
        if a[0] < 30 and pressure > 0.55:
            opts.append(dict(kind="clear", ev=-0.5 * LOSS + 0.006 * pressure))
        return opts, pressure

    def offside(self, team, mate, target) -> bool:
        if self.is_gk[mate]:
            return False
        opp = np.where((self.team_of != team) & self.active)[0]
        ox = np.sort(self.tf(team, self.pos[opp])[:, 0])
        line = max(ox[-2], self.tf(team, self.ball[:2])[0], P.LENGTH / 2)
        mx = self.tf(team, self.pos[mate])[0]
        return bool(mx > line + 0.3)

    def choose(self, opts, team):
        press, direct, width, tempo, risk = self.style(team)
        ev = np.array([o["ev"] for o in opts])
        temp = 0.0045 * (0.7 + 0.6 * risk)
        w = np.exp((ev - ev.max()) / temp)
        w /= w.sum()
        return opts[int(self.rng.choice(len(opts), p=w))]

    def decide(self):
        slot = self.carrier
        team = int(self.team_of[slot])
        opts, pressure = self.options(slot)
        o = self.choose(opts, team)
        a_abs = self.pos[slot].copy()
        self.close_carry()
        if o["kind"] == "pass":
            self.do_pass(slot, team, o, pressure)
        elif o["kind"] == "carry":
            self.carry_target = self.tf(team, o["target"])
            self.carry_start = (a_abs, self.f)
            dist = float(np.linalg.norm(o["target"] - self.tf(team, a_abs)))
            dur = int(max(dist / 5.5, 0.6) * P.FPS)
            self.decide_at = self.f + dur
            if self.rng.random() > o["p"]:
                self.carry_plan = {"tackler": o["stopper"], "at": self.f + int(dur * o["stop_frac"]), "p": o["p"]}
        elif o["kind"] == "shot":
            self.do_shot(slot, team, o["xg"], pressure, a_abs)
        elif o["kind"] == "cross":
            self.do_cross(slot, team, o["target"])
        elif o["kind"] == "clear":
            self.do_clear(slot, team)

    def close_carry(self):
        if self.carry_start is None or self.carrier is None:
            return
        p0, f0 = self.carry_start
        p1 = self.pos[self.carrier]
        d = float(np.linalg.norm(p1 - p0))
        if d >= 3.0:
            ev = self.emit("carry", self.carrier, start=p0, end=p1, q={"distance_m": round(d, 1),
                           "duration_s": round((self.f - f0) * DT, 2)})
            ev["t"] = round(f0 * DT, 2)
            ev["t_end"] = round(self.clock(), 2)
        self.carry_start = None

    # ----- passes, crosses, clearances --------------------------------------------------

    def do_pass(self, slot, team, o, pressure, set_piece: str | None = None):
        a_tf = self.tf(team, self.pos[slot])
        target = o["target"]
        L = float(np.linalg.norm(target - a_tf))
        lofted = o["lofted"]
        roll = self.rng.random()
        q = {"length_m": round(L, 1), "height": "high" if lofted else "ground",
             "under_pressure": pressure > 0.4, "recipient": self.pid(o["to"]),
             "zone": P.zone(*a_tf), "end_zone": P.zone(*target)}
        if set_piece:
            q["set_piece"] = set_piece
        tgt_abs = self.tf(team, target)
        q["target_x"], q["target_y"] = round(float(tgt_abs[0]), 2), round(float(tgt_abs[1]), 2)
        v_ground = min(11.0 + 0.45 * L, 27.0)
        speed = (L / o["secs"]) if lofted else v_ground
        q["ball_speed_kmh"] = round(speed * 3.6, 1)
        truth = {"p_success": round(o["p"], 4), "pressure": round(pressure, 3)}
        height = 2.5 + 0.11 * L if lofted else 0.0
        ease = 1.0 if lofted else EASE
        start = self.pos[slot].copy()
        if o.get("offside"):
            end_abs = self.tf(team, target)
            ev = self.emit("pass", slot, outcome="offside", start=start, end=end_abs, q=q, truth=truth,
                           t_end=self.clock() + o["secs"])
            self.launch("pass", start, [*end_abs, 0], o["secs"], height, ease,
                        {"then": "offside", "team": team, "who": o["to"], "ev": ev["id"]})
            self.receiver = o["to"]
            return
        if roll < o["p"]:
            end_abs = self.tf(team, target)
            ev = self.emit("pass", slot, outcome="complete", start=start, end=end_abs, q=q, truth=truth,
                           t_end=self.clock() + o["secs"])
            self.launch("pass", start, [*end_abs, 0.0], o["secs"], height, ease, {"then": "receive", "who": o["to"], "ev": ev["id"]})
            self.receiver = o["to"]
            return
        # Failed: intercepted in the lane, or misplaced.
        if self.rng.random() < 0.6:
            s = o["s_int"]
            meet_tf = a_tf + (target - a_tf) * s
            meet = self.tf(team, meet_tf)
            ev = self.emit("pass", slot, outcome="incomplete", start=start, end=meet, q=q, truth=truth,
                           t_end=self.clock() + o["secs"] * s)
            self.launch("pass", start, [*meet, 0.0], max(o["secs"] * s, 0.2), height * s, ease,
                        {"then": "intercept", "who": o["interceptor"], "ev": ev["id"], "chaser": o["interceptor"], "meet": meet})
            self.receiver = None
            return
        err = self.rng.normal(0, 3.5 + 0.12 * L, 2)
        miss_tf = target + err
        if min(target[1], P.WIDTH - target[1]) < 14 and self.rng.random() < 0.6:
            miss_tf[1] = -1.0 if target[1] < P.GOAL_Y else P.WIDTH + 1.0
        miss = self.tf(team, miss_tf)
        ev = self.emit("pass", slot, outcome="incomplete", start=start, end=miss, q=q, truth=truth,
                       t_end=self.clock() + o["secs"])
        self.launch("pass", start, [*miss, 0.0], o["secs"], height, ease, {"then": "loose", "ev": ev["id"], "team": team})
        self.receiver = None

    def do_cross(self, slot, team, target_tf):
        start = self.pos[slot].copy()
        a_tf = self.tf(team, start)
        L = float(np.linalg.norm(target_tf - a_tf))
        secs = 0.8 + L / 20.0
        end_abs = self.tf(team, target_tf)
        # Aerial duel at the landing point: best attacking header nearby against defenders.
        att = [s for s in np.where((self.team_of == team) & self.active)[0] if s != slot and not self.is_gk[s]]
        dfn = [s for s in np.where((self.team_of != team) & self.active)[0]]
        da = sorted(att, key=lambda s: np.linalg.norm(self.pos[s] - end_abs))[:2]
        dd = sorted(dfn, key=lambda s: np.linalg.norm(self.pos[s] - end_abs))[:2]
        h_att = max(self.roster[s].heading for s in da)
        h_def = max(self.roster[s].heading for s in dd)
        gk_claim = 0.12 if abs(target_tf[1] - P.GOAL_Y) < 6 and target_tf[0] > P.LENGTH - 8 else 0.04
        p_att = float(np.clip(0.27 + 0.45 * (h_att - h_def) - 0.04 * (len(dd) - 1), 0.08, 0.6))
        r = self.rng.random()
        q = {"length_m": round(L, 1), "height": "high", "cross": True, "zone": P.zone(*a_tf),
             "ball_speed_kmh": round(L / secs * 3.6, 1)}
        if r < gk_claim:
            outcome, then, who = "incomplete", "claim", int(np.where((self.team_of != team) & self.is_gk & self.active)[0][0])
        elif r < gk_claim + p_att:
            outcome, then, who = "complete", "header", da[0]
        else:
            outcome, then, who = "incomplete", "clear", dd[0]
        ev = self.emit("pass", slot, outcome=outcome, start=start, end=end_abs,
                       q={**q, "recipient": self.pid(da[0]), "won_by": self.pid(who),
                          "target_x": round(float(end_abs[0]), 2), "target_y": round(float(end_abs[1]), 2)},
                       truth={"p_success": round(p_att, 4)}, t_end=self.clock() + secs)
        self.launch("cross", start, [*end_abs, 1.8], secs, 4.0 + 0.1 * L, 1.0,
                    {"then": then, "who": who, "team": team, "ev": ev["id"], "chaser": who, "meet": end_abs})

    def do_clear(self, slot, team, *, header=False):
        start = self.pos[slot].copy()
        a_tf = self.tf(team, start)
        tgt = np.array([min(a_tf[0] + self.rng.uniform(30, 55), 100), self.rng.uniform(5, 63)])
        if header and a_tf[0] < 20 and self.rng.random() < 0.1:
            spot = self.corner_spot(1 - team, start)
            self.emit("clearance", slot, start=start, end=spot, q={"header": True, "behind": True})
            self.emit("out", None, team, start=spot, q={"restart": "corner"})
            self.dead("corner", 1 - team, spot, self.rng.normal(31, 6))
            return
        out = self.rng.random() < 0.35
        if out:
            tgt[1] = -1.0 if self.rng.random() < 0.5 else P.WIDTH + 1.0
        L = float(np.linalg.norm(tgt - a_tf))
        secs = 1.0 + L / 24.0
        end_abs = self.tf(team, tgt)
        self.emit("clearance", slot, start=start, end=end_abs, q={"length_m": round(L, 1), "header": header})
        self.launch("clear", start, [*end_abs, 0.0], secs, 6.0 + 0.08 * L, 1.0, {"then": "loose", "team": team})

    # ----- shots ------------------------------------------------------------------------

    def block_chance(self, team, a_tf) -> float:
        return min((0.12 + 0.17 * self.lane_defenders(team, a_tf)) * BLOCK_SCALE, min(0.62 * BLOCK_SCALE, 0.8))

    def do_shot(self, slot, team, xg_open, pressure, a_abs, *, header=False, set_piece=None):
        """xg_open is the chance with a clear lane. The shot's xG folds in the bodies between
        the shooter and goal, like a freeze-frame model: xG = xg_open * (1 - p_block)."""
        a_tf = self.tf(team, a_abs)
        p_block = 0.0 if set_piece == "penalty" else self.block_chance(team, a_tf)
        xg_true = xg_open * (1 - p_block)
        r0 = self.rng.random()
        blocked = r0 < p_block
        goal = (not blocked) and self.rng.random() < xg_open
        q = {"distance_m": round(P.goal_distance(*a_tf), 1), "angle_deg": round(math.degrees(P.goal_angle(*a_tf)), 1),
             "body_part": "head" if header else "foot", "under_pressure": pressure > 0.4, "zone": P.zone(*a_tf),
             "xg": round(xg_true, 4)}
        if set_piece:
            q["set_piece"] = set_piece
        if goal:
            outcome = "goal"
        elif blocked:
            outcome = "blocked"
        else:
            # Unblocked misses: about two in five are on target.
            outcome = "saved" if self.rng.random() < SAVED_SHARE else "off_target"
        speed = self.rng.uniform(10, 16) if header else self.rng.uniform(19, 31)
        if outcome in ("goal", "saved"):
            gy = P.GOAL_Y + self.rng.uniform(-3.4, 3.4)
            gz = self.rng.uniform(0.1, 2.3)
            end_tf = np.array([P.LENGTH, gy])
        elif outcome == "off_target":
            side = 1 if self.rng.random() < 0.5 else -1
            gy = P.GOAL_Y + side * self.rng.uniform(3.8, 9.0)
            gz = self.rng.uniform(0.2, 4.0)
            end_tf = np.array([P.LENGTH + 1.0, gy])
        else:
            end_tf = a_tf + (np.array([P.LENGTH, P.GOAL_Y]) - a_tf) * self.rng.uniform(0.08, 0.3)
            gz = self.rng.uniform(0.2, 1.2)
        end_abs = self.tf(team, end_tf)
        L = float(np.linalg.norm(end_tf - a_tf))
        secs = max(L / speed, 0.15)
        q["shot_speed_kmh"] = round(speed * 3.6, 1)
        ev = self.emit("shot", slot, outcome=outcome, start=a_abs, end=end_abs, q=q,
                       truth={"xg": round(xg_true, 4), "pressure": round(pressure, 3)}, t_end=self.clock() + secs)
        self.close_carry()
        self.launch("shot", a_abs, [*end_abs, gz], secs, 0.4 if not header else 0.2, 1.0,
                    {"then": "shot_end", "outcome": outcome, "team": team, "shooter": slot, "ev": ev["id"]})

    def lane_defenders(self, team, a_tf) -> int:
        opp = np.where((self.team_of != team) & self.active & ~self.is_gk)[0]
        o = self.tf(team, self.pos[opp])
        g = np.array([P.LENGTH, P.GOAL_Y])
        v = g - a_tf
        L = np.linalg.norm(v)
        u = v / L
        rel = o - a_tf
        along = rel @ u
        perp = np.abs(rel[:, 0] * u[1] - rel[:, 1] * u[0])
        return int(np.sum((along > 0.5) & (along < L) & (perp < 1.2 + 0.08 * along)))

    # ----- arrivals ---------------------------------------------------------------------

    def arrive(self):
        fl = self.flight
        r = fl.resolve
        then = r["then"]
        end2 = fl.end[:2]
        if fl.kind in ("pass", "clear", "cross") and self.out_of_play(end2):
            self.ball_out(end2, r.get("team", self.last_touch_team))
            return
        if then == "receive":
            if np.linalg.norm(self.pos[r["who"]] - end2) > 6.0:
                self.loose(end2, pass_ev=r["ev"])
            else:
                self.give(r["who"])
        elif then == "offside":
            team = r["team"]
            self.emit("offside", r["who"], start=end2)
            self.dead("free_kick", 1 - team, end2, 18)
        elif then == "intercept":
            who = r["who"]
            if np.linalg.norm(self.pos[who] - end2) > 2.5:
                self.loose(end2)
            else:
                self.emit("interception", who, start=end2)
                self.give(who)
        elif then == "loose":
            self.loose(end2)
        elif then == "claim":
            self.emit("claim", r["who"], start=end2)
            self.give(r["who"])
            self.decide_at = self.f + int(self.rng.uniform(2, 5) * P.FPS)
        elif then == "header":
            who = r["who"]
            team = r["team"]
            a_tf = self.tf(team, end2)
            if P.goal_distance(*a_tf) < 16 and self.rng.random() < HEADER_SHOT:
                x = P.xg(*a_tf, header=True, pressure=0.5, skill=self.roster[who].heading, bias=FINISH_LOGIT)
                self.do_shot(who, team, x, 0.5, end2.copy(), header=True, set_piece=r.get("set_piece"))
            else:
                self.give(who)
        elif then == "clear":
            who = r["who"]
            self.do_clear(who, int(self.team_of[who]), header=True)
        elif then == "shot_end":
            self.shot_end(r, end2)

    def shot_end(self, r, end2):
        team, outcome = r["team"], r["outcome"]
        dfn = 1 - team
        gk = int(np.where((self.team_of == dfn) & self.is_gk & self.active)[0][0])
        if outcome == "goal":
            self.score[team] += 1
            self.emit("goal", r["shooter"], start=end2, q={"shot": r["ev"]})
            self.events[r["ev"]]["score"] = list(self.score)
            var = self.rng.random() < 0.25
            if var:
                self.emit("var_check", None, None, q={"goal": r["ev"], "decision": "goal stands"})
            pause = self.rng.normal(GOAL_STOP_S, 10) + (self.rng.uniform(50, 110) if var else 0)
            self.dead("kickoff", dfn, np.array([P.LENGTH / 2, P.GOAL_Y]), pause)
        elif outcome == "saved":
            self.emit("save", gk, start=end2, q={"shot": r["ev"]})
            u = self.rng.random()
            if u < min(0.25 * CORNER_DEFLECT, 0.6):
                self.emit("out", None, dfn, start=end2, q={"restart": "corner"})
                self.dead("corner", team, self.corner_spot(team, end2), self.rng.normal(31, 6))
            elif u < 0.78:
                self.give(gk)
                self.decide_at = self.f + int(self.rng.uniform(2.5, 6) * P.FPS)
            else:
                spot = self.tf(dfn, self.tf(dfn, end2) + np.array([self.rng.uniform(4, 10), self.rng.normal(0, 6)]))
                self.loose(spot)
        elif outcome == "blocked":
            blocker = self.nearest(dfn, end2)
            self.emit("block", blocker, start=end2, q={"shot": r["ev"]})
            if self.rng.random() < min(0.3 * CORNER_DEFLECT, 0.7):
                self.emit("out", None, dfn, start=end2, q={"restart": "corner"})
                self.dead("corner", team, self.corner_spot(team, end2), self.rng.normal(31, 6))
            else:
                self.loose(end2 + self.rng.normal(0, 4, 2))
        else:
            if self.rng.random() < min(0.08 * CORNER_DEFLECT, 0.3):
                self.emit("out", None, dfn, start=end2, q={"restart": "corner"})
                self.dead("corner", team, self.corner_spot(team, end2), self.rng.normal(31, 6))
            else:
                self.emit("out", None, team, start=end2, q={"restart": "goal_kick"})
                self.dead("goal_kick", dfn, self.tf(dfn, np.array([5.5, P.GOAL_Y + self.rng.uniform(-9, 9)])), self.rng.normal(17, 4))

    def corner_spot(self, team, near):
        t = self.tf(team, near)
        y = 0.3 if t[1] < P.GOAL_Y else P.WIDTH - 0.3
        return self.tf(team, np.array([P.LENGTH - 0.3, y]))

    def nearest(self, team, xy, exclude=()) -> int:
        idx = np.where((self.team_of == team) & self.active)[0]
        idx = np.array([s for s in idx if s not in exclude])
        d = np.linalg.norm(self.pos[idx] - xy, axis=1)
        return int(idx[np.argmin(d)])

    def loose(self, xy, pass_ev: int | None = None):
        """A loose ball goes to whoever wins the race to it."""
        xy = np.array(P.clamp_pitch(*xy, margin=0.5))
        idx = np.where(self.active)[0]
        d = np.linalg.norm(self.pos[idx] - xy, axis=1)
        sp = np.array([self.roster[s].pace for s in idx])
        t = d / (0.85 * sp) + self.rng.exponential(0.25, len(idx))
        order = np.argsort(t)
        w = int(idx[order[0]])
        rival = next((int(idx[k]) for k in order[1:] if self.team_of[idx[k]] != self.team_of[w]), None)
        self.mode = "loose"
        self.carrier = None
        self.receiver = None
        self.flight = None
        self.carry_target = None
        self.ball[:2] = xy
        self.ball[2] = 0.0
        self.loose_ball = {"xy": xy, "winner": w, "rival": rival, "until": self.f + int((t[order[0]] + 2.0) * P.FPS)}
        if pass_ev is not None:
            pe = self.events[pass_ev]
            if self.teams[int(self.team_of[w])].id != pe["team"]:
                pe["outcome"] = "incomplete"

    def loose_step(self):
        lb = self.loose_ball
        w = lb["winner"]
        if np.linalg.norm(self.pos[w] - lb["xy"]) < 1.0 or self.f >= lb["until"]:
            self.emit("ball_recovery", w, start=lb["xy"])
            self.loose_ball = None
            self.give(w)

    def out_of_play(self, xy) -> bool:
        return xy[0] < 0 or xy[0] > P.LENGTH or xy[1] < 0 or xy[1] > P.WIDTH

    def ball_out(self, xy, last_team):
        other = 1 - last_team
        if xy[1] < 0 or xy[1] > P.WIDTH:
            spot = np.array([min(max(xy[0], 1), P.LENGTH - 1), 0.0 if xy[1] < 0 else P.WIDTH])
            self.emit("out", None, last_team, start=spot, q={"restart": "throw_in"})
            self.dead("throw_in", other, spot, self.rng.normal(15, 4))
            return
        # Over a goal line: whose goal line is it?
        att_tf = self.tf(last_team, xy)
        if att_tf[0] > P.LENGTH:
            self.emit("out", None, last_team, start=xy, q={"restart": "goal_kick"})
            spot = self.tf(other, np.array([5.5, P.GOAL_Y + self.rng.uniform(-9, 9)]))
            self.dead("goal_kick", other, spot, self.rng.normal(25, 6))
        else:
            self.emit("out", None, last_team, start=xy, q={"restart": "corner"})
            self.dead("corner", other, self.corner_spot(other, xy), self.rng.normal(31, 6))

    # ----- dead balls and restarts ------------------------------------------------------

    def dead(self, kind, team, spot, seconds):
        spot = np.array(spot, float)
        if kind in ("throw_in", "goal_kick", "corner", "free_kick"):
            seconds *= RESTART_PACE
            # Game management: a side in front after the hour takes its time over restarts.
            mins = (self.period - 1) * 45 + self.clock() / 60
            if mins > 60 and self.score[team] > self.score[1 - team]:
                seconds *= 1 + LATE_STALL
        if kind == "throw_in":
            spot[1] = min(max(spot[1], 0.2), P.WIDTH - 0.2)
        cand = np.where((self.team_of == team) & self.active)[0]
        if kind == "goal_kick":
            taker = int(cand[self.is_gk[cand]][0])
        elif kind == "kickoff":
            taker = self.nearest_role(team, ("ST", "AM", "CM"))
        else:
            cand = cand[~self.is_gk[cand]]
            taker = int(cand[np.argmin(np.linalg.norm(self.pos[cand] - spot, axis=1))])
        self.mode = "dead"
        self.carrier = None
        self.receiver = None
        self.flight = None
        self.carry_target = None
        self.carry_start = None
        self.ball[:2] = spot
        self.ball[2] = 0.11
        self.restart = {"kind": kind, "team": team, "spot": spot, "taker": taker,
                        "at": self.f + int(max(seconds, 3) * P.FPS)}
        self.set_possession(team)

    def nearest_role(self, team, roles) -> int:
        for r in roles:
            for s in range(N):
                if self.team_of[s] == team and self.active[s] and self.slot_role[s] == r:
                    return s
        return int(np.where((self.team_of == team) & self.active & ~self.is_gk)[0][0])

    def take_restart(self):
        r = self.restart
        taker, team, kind, spot = r["taker"], r["team"], r["kind"], r["spot"]
        if not r.get("subs_checked"):
            # Substitutions happen while the ball is dead, and a window holds the restart up.
            r["subs_checked"] = True
            if self.maybe_subs() and SUB_STOP_S > 0:
                r["at"] = self.f + int(SUB_STOP_S * P.FPS)
                return
        if np.linalg.norm(self.pos[taker] - spot) > 1.5 and self.f < r["at"] + 6 * P.FPS:
            return
        self.pos[taker] = spot.copy()
        self.vel[taker] = 0
        self.restart = None
        self.give(taker)
        a_tf = self.tf(team, spot)
        self.emit(kind, taker, start=spot)
        if kind == "corner":
            self.do_cross(taker, team, np.array([P.LENGTH - self.rng.uniform(4, 12), P.GOAL_Y + self.rng.normal(0, 5)]))
            self.flight.resolve["set_piece"] = "corner"
            return
        if kind == "penalty":
            self.do_shot(taker, team, 0.78 + 0.08 * (self.roster[taker].finishing - 0.5), 0.0, spot.copy(), set_piece="penalty")
            return
        if kind == "free_kick" and P.goal_distance(*a_tf) < 30 and abs(a_tf[1] - P.GOAL_Y) < 18 and self.rng.random() < 0.45:
            x = 0.6 * P.xg(*a_tf, skill=self.roster[taker].finishing, bias=FINISH_LOGIT)
            self.do_shot(taker, team, x, 0.0, spot.copy(), set_piece="free_kick")
            return
        if kind == "free_kick" and a_tf[0] > 70 and self.rng.random() < 0.6:
            self.do_cross(taker, team, np.array([P.LENGTH - self.rng.uniform(5, 13), P.GOAL_Y + self.rng.normal(0, 6)]))
            self.flight.resolve["set_piece"] = "free_kick"
            return
        # Short or long first pass from the restart.
        opts, pressure = self.options(taker)
        passes = [o for o in opts if o["kind"] == "pass" and not o["offside"]]
        if kind == "throw_in":
            passes = [o for o in passes if np.linalg.norm(o["target"] - a_tf) < 22] or passes
        if kind == "goal_kick":
            press, direct, *_ = self.style(team)
            long = self.rng.random() < 0.25 + 0.55 * direct
            passes = [o for o in passes if (o["target"][0] > 45) == long] or passes
        if kind == "kickoff":
            passes = [o for o in passes if o["target"][0] < a_tf[0]] or passes
        if not passes:
            self.decide_at = self.f
            return
        o = self.choose(passes, team)
        self.do_pass(taker, team, o, 0.0, set_piece=kind)

    # ----- duels ------------------------------------------------------------------------

    def duels(self):
        """Pressure and tackles on the carrier, checked every frame while he holds the ball."""
        c = self.carrier
        team = int(self.team_of[c])
        opp = np.where((self.team_of != team) & self.active & ~self.is_gk)[0]
        d = np.linalg.norm(self.pos[opp] - self.pos[c], axis=1)
        for k in np.where(d < 3.0)[0]:
            s = int(opp[k])
            if s not in self.pressed_by:
                self.pressed_by.add(s)
                self.emit("pressure", s, start=self.pos[s].copy(), q={"on": self.pid(c), "distance_m": round(float(d[k]), 1)})
        for k in np.where(d < 1.4)[0]:
            s = int(opp[k])
            if self.stun_until[s] > self.f:
                continue
            tack = self.roster[s].tackling
            if self.rng.random() > 0.3 * (0.5 + tack) * DT:
                continue
            drib = self.roster[c].dribbling
            a_tf = self.tf(team, self.pos[c])
            p_foul = (0.08 + 0.04 * self.style(1 - team)[0]) * FOUL_RATE
            if P.in_box(*a_tf):
                p_foul *= 0.3
            if self.rng.random() < p_foul:
                self.foul(s, c, team, a_tf)
                return
            p_win = float(np.clip(0.52 + 0.4 * (tack - drib) + 0.05 * (self.strength(1 - team) - self.strength(team)), 0.15, 0.85))
            if self.rng.random() < p_win:
                self.close_carry()
                self.emit("tackle", s, start=self.pos[c].copy(), outcome="won", q={"on": self.pid(c)},
                          truth={"p_win": round(p_win, 3)})
                near_line = min(self.pos[c][1], P.WIDTH - self.pos[c][1]) < 5
                if near_line and self.rng.random() < 0.45:
                    spot = np.array([self.pos[c][0], 0.0 if self.pos[c][1] < P.GOAL_Y else P.WIDTH])
                    self.emit("out", None, 1 - team, start=spot, q={"restart": "throw_in"})
                    self.dead("throw_in", team, spot, self.rng.normal(9, 3))
                elif self.rng.random() < 0.35:
                    self.loose(self.pos[c] + self.rng.normal(0, 2.5, 2))
                else:
                    self.give(s)
                return
            self.emit("tackle", s, start=self.pos[c].copy(), outcome="lost", q={"on": self.pid(c)}, truth={"p_win": round(p_win, 3)})
            self.emit("dribble", c, start=self.pos[c].copy(), outcome="complete", q={"past": self.pid(s)})
            self.stun_until[s] = self.f + int(1.2 * P.FPS)
            return

    def challenge(self):
        """The defender the carry model picked as the one who stops it arrives and wins it."""
        plan = self.carry_plan
        self.carry_plan = None
        c = self.carrier
        s = plan["tackler"]
        if not self.active[s]:
            return
        team = int(self.team_of[c])
        a_tf = self.tf(team, self.pos[c])
        p_foul = (0.045 + 0.03 * self.style(1 - team)[0]) * FOUL_RATE
        if P.in_box(*a_tf):
            p_foul *= 0.3
        if self.rng.random() < p_foul:
            self.foul(s, c, team, a_tf)
            return
        self.close_carry()
        self.pos[s] = self.pos[s] + np.clip(self.pos[c] - self.pos[s], -1.5, 1.5) * 0.5
        self.emit("tackle", s, start=self.pos[c].copy(), outcome="won", q={"on": self.pid(c)},
                  truth={"p_keep": round(plan["p"], 3)})
        if min(self.pos[c][1], P.WIDTH - self.pos[c][1]) < 6 and self.rng.random() < 0.5:
            spot = np.array([self.pos[c][0], 0.0 if self.pos[c][1] < P.GOAL_Y else P.WIDTH])
            self.emit("out", None, 1 - team, start=spot, q={"restart": "throw_in"})
            self.dead("throw_in", team, spot, self.rng.normal(13, 4))
        elif self.rng.random() < 0.35:
            self.loose(self.pos[c] + self.rng.normal(0, 2.5, 2))
        else:
            self.give(s)

    def foul(self, fouler, fouled, team, a_tf):
        self.close_carry()
        spot = self.pos[fouled].copy()
        self.emit("foul", fouler, start=spot, q={"on": self.pid(fouled)})
        pid = self.pid(fouler)
        r = self.rng.random()
        booked = self.yellow.get(pid, 0) >= 1
        if r < RED_STRAIGHT or (booked and r < RED_STRAIGHT + RED_SECOND):
            self.emit("card", fouler, outcome="red", q={"second_yellow": r >= RED_STRAIGHT})
            self.send_off(fouler)
        elif not booked and r < RED_STRAIGHT + YELLOW_SHARE:
            self.yellow[pid] = self.yellow.get(pid, 0) + 1
            self.emit("card", fouler, outcome="yellow")
        treat = self.rng.random() < TREATMENT
        if treat:
            self.emit("treatment", fouled, q={"seconds": 0})
        extra = self.rng.uniform(50, 140) if treat else 0.0
        if treat:
            self.events[-1]["q"]["seconds"] = round(extra)
        if P.in_box(*a_tf) and self.rng.random() < 0.85:
            spot_tf = np.array([P.LENGTH - P.PENALTY_SPOT, P.GOAL_Y])
            self.dead("penalty", team, self.tf(team, spot_tf), self.rng.normal(60, 10) + extra)
        else:
            self.dead("free_kick", team, spot, self.rng.normal(30, 8) + extra)

    def send_off(self, slot):
        self.active[slot] = False
        side = -3.0 if self.rng.random() < 0.5 else P.WIDTH + 3.0
        self.pos[slot] = [P.LENGTH / 2, side]
        self.vel[slot] = 0

    def maybe_subs(self) -> bool:
        """Bring on fresh legs after 56 minutes; True when either side made a change."""
        mins = (self.period - 1) * 45 + self.clock() / 60
        any_made = False
        for team in (0, 1):
            if mins < 56 or self.subs_made[team] >= 5 or self.sub_windows[team] >= 3:
                continue
            if self.rng.random() > 0.07:
                continue
            k = int(self.rng.integers(1, 3))
            idx = [s for s in range(N) if self.team_of[s] == team and self.active[s] and not self.is_gk[s]]
            tired = sorted(idx, key=lambda s: self.roster[s].stamina - self.minutes_on[s] / 9000.0)[:k]
            made = False
            for s in tired:
                if self.subs_made[team] >= 5:
                    break
                group = ROLE_GROUP[self.slot_role[s]]
                bench = [p for p in self.bench[team] if ROLE_GROUP[p.role] == group] or [p for p in self.bench[team] if p.role != "GK"]
                if not bench:
                    continue
                new = bench[0]
                self.bench[team].remove(new)
                old = self.roster[s]
                self.roster[s] = new
                self.minutes_on[s] = 0
                self.subs_made[team] += 1
                self.emit("substitution", s, team, q={"off": old.id, "on": new.id, "slot": s})
                made = True
            if made:
                self.sub_windows[team] += 1
                any_made = True
        return any_made

    # ----- the loop ---------------------------------------------------------------------

    def kickoff_layout(self) -> np.ndarray:
        out = np.zeros((N, 2))
        for s in range(N):
            team = self.team_of[s]
            x01, y01 = self.slot_x01[s], self.slot_y01[s]
            x = 4.0 if self.is_gk[s] else 14 + x01 * 34
            y = P.GOAL_Y + (y01 - 0.5) * 50
            out[s] = self.tf(team, np.array([min(x, 51.5), y]))
        out[~self.active] = self.pos[~self.active]
        return out

    def kickoff_positions(self, kicking: int):
        self.pos[:] = self.kickoff_layout()
        self.vel[:] = 0
        self.dead("kickoff", kicking, np.array([P.LENGTH / 2, P.GOAL_Y]), 2.0)
        taker = self.restart["taker"]
        self.pos[taker] = [P.LENGTH / 2, P.GOAL_Y]

    def record(self, buf, i):
        buf[i, :N] = self.pos
        buf[i, N, :2] = self.ball[:2]
        buf[i, N + 1, 0] = self.ball[2]
        buf[i, N + 1, 1] = 0

    def play_period(self, period: int, seconds: float, kicking: int):
        self.period = period
        self.f = 0
        self.emit("period_start", None, kicking, q={"period": period})
        self.kickoff_positions(kicking)
        max_frames = int((seconds + 60) * P.FPS)
        buf = np.zeros((max_frames, N + 2, 2), np.float32)
        end_frame = int(seconds * P.FPS)
        while True:
            self.move()
            if self.mode == "held":
                self.duels()
            if self.mode == "held" and self.carry_plan is not None and self.f >= self.carry_plan["at"]:
                self.challenge()
            if self.mode == "loose":
                self.loose_step()
            elif self.mode == "held" and self.f >= self.decide_at:
                self.decide()
            elif self.mode == "flight" and self.f >= self.flight.f0 + self.flight.frames:
                self.arrive()
            elif self.mode == "dead" and self.f >= self.restart["at"]:
                if self.f >= end_frame:
                    break
                self.take_restart()
            self.ball_follow()
            self.ball_smooth += (self.ball[:2] - self.ball_smooth) * (DT / 1.1)
            if self.f % (10 * P.FPS) == 0:
                self.momentum = 0.96 * self.momentum + self.rng.normal(0, 0.12, 2)
                self.momentum = np.clip(self.momentum, -1, 1)
            self.record(buf, self.f)
            self.f += 1
            self.gf += 1
            if self.f >= end_frame and self.mode != "flight":
                break
            if self.f >= max_frames - 1:
                break
        self.close_carry()
        self.emit("period_end", None, None, q={"period": period, "length_s": round(self.clock(), 1)})
        self.period_frames.append(self.f)
        self.frames.append(buf[: self.f])

    def play(self):
        p1 = 45 * 60 + self.rng.uniform(80, 330)
        p2 = 45 * 60 + self.rng.uniform(200, 560)
        first = int(self.rng.integers(2))
        self.play_period(1, p1, first)
        self.play_period(2, p2, 1 - first)
        return self
