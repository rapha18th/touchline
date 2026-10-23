"""Live match state: stats, explainers, cues and moments from events and tracking as they arrive.

Everything here is deterministic and fast (well under a millisecond per event), so the
screen stays live while the agent crew works on the moments that deserve words.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

import numpy as np

from .difficulty import PassDifficulty, features
from .match_io import FPS, LENGTH, WIDTH, Match, clock, match_seconds, minute

SPRINT_KMH = 30.0
TAG_COOLDOWN_S = 40.0
WINDOW_S = 300.0


@dataclass
class Moment:
    kind: str
    match_s: float
    frame: int
    period: int
    priority: int
    facts: dict
    sources: list[int]
    team: str | None = None
    player: str | None = None


@dataclass
class Team:
    goals: int = 0
    shots: int = 0
    sot: int = 0
    xg: float = 0.0
    passes: int = 0
    complete: int = 0
    pass_m: float = 0.0
    final_third_entries: int = 0
    box_entries: int = 0
    pressures: int = 0
    tackles_won: int = 0
    interceptions: int = 0
    fouls: int = 0
    corners: int = 0
    yellows: int = 0
    reds: int = 0
    possession_s: float = 0.0
    def_actions_high: int = 0     # tackles, interceptions, fouls in the opponent's first 60%
    opp_passes_high: int = 0      # opponent passes in their own first 60%


@dataclass
class Player:
    passes: int = 0
    complete: int = 0
    pass_m: float = 0.0
    hard_complete: int = 0
    hardest: float = 0.0
    hardest_ev: int | None = None
    shots: int = 0
    goals: int = 0
    xg: float = 0.0
    key_passes: int = 0
    tackles: int = 0
    interceptions: int = 0
    touches: int = 0
    dist_m: float = 0.0
    top_kmh: float = 0.0
    sprints: int = 0
    last_tag_s: float = -1e9
    km_tagged: int = 0


@dataclass
class LiveState:
    match: Match
    model: PassDifficulty = field(default_factory=PassDifficulty)
    period: int = 1
    t: float = 0.0
    frame: int = 0
    score: list[int] = field(default_factory=lambda: [0, 0])

    def __post_init__(self):
        self.teams = {"home": Team(), "away": Team()}
        self.players: dict[str, Player] = defaultdict(Player)
        self.window: deque = deque()          # (match_s, kind, team, value) for rolling explainers
        self.last_ball_event: dict | None = None
        self.chain_passes = defaultdict(int)
        self.chain_team: dict[int, str] = {}
        self.last_momentum_label: str | None = None
        self.last_stat_s = 0.0
        self.slot_player: dict[int, str] = {}
        for side, base in (("home", 0), ("away", 11)):
            for i, pid in enumerate(self.match.meta[side]["starters"]):
                self.slot_player[base + i] = pid
        self.prev_frame_xy: np.ndarray | None = None
        self.speed = np.zeros(22)
        self.cue_n = 0
        self.difficulty_log: list[tuple[float, float, int]] = []   # (estimated completion, engine truth, event id)

    # ----- helpers ----------------------------------------------------------------------

    @property
    def match_s(self) -> float:
        return match_seconds(self.period, self.t)

    def other(self, team: str) -> str:
        return "away" if team == "home" else "home"

    def cue(self, kind: str, *, priority: int, ttl: float, payload: dict, sources: list[int],
            personas=("all",), team=None, player=None, metric=None, anchor=None) -> dict:
        self.cue_n += 1
        return {
            "schema": "touchline.cue/1", "id": f"{self.match.id}-c{self.cue_n:05d}", "match_id": self.match.id,
            "kind": kind, "period": self.period, "match_s": round(self.match_s, 2), "frame": self.frame,
            "clock": clock(self.period, self.t), "priority": priority, "ttl_s": ttl,
            "audience": {"personas": list(personas), "team": team, "player": player, "metric": metric},
            "lang": "en", "anchor": anchor, "payload": payload, "sources": sources, "verified": True,
            "author": "state",
        }

    def attack_xy(self, e: dict, which: str = "start") -> tuple[float, float]:
        x, y = (e["x"], e["y"]) if which == "start" else (e["end_x"], e["end_y"])
        return self.match.to_attack(e["team"], e["period"], x, y)

    # ----- events -----------------------------------------------------------------------

    def apply(self, e: dict) -> tuple[list[dict], list[Moment]]:
        self.period = e["period"]
        self.t = e["t_end"] if e.get("t_end") is not None else e["t"]
        self.frame = e["frame"]
        cues: list[dict] = []
        moments: list[Moment] = []
        typ, team, pid = e["type"], e["team"], e["player"]
        T = self.teams.get(team) if team else None
        P = self.players[pid] if pid else None
        name = self.match.name(pid)

        self.possession_time(e)
        if typ == "substitution":
            self.slot_player[e["q"]["slot"]] = e["q"]["on"]
            cues.append(self.cue("milestone", priority=1, ttl=8, sources=[e["id"]], team=team, payload={
                "text": f"{self.match.short(e['q']['on'])} on for {self.match.short(e['q']['off'])}", "type": "substitution"}))

        if typ == "pass" and T is not None:
            self.on_pass(e, T, P, name, cues, moments)
        elif typ == "shot" and T is not None:
            self.on_shot(e, T, P, name, cues, moments)
        elif typ == "goal" and T is not None:
            self.score = list(e["score"])
            T.goals += 1
            P.goals += 1
            shot = self.match.events[e["q"]["shot"]]
            assist = self.assist_for(shot)
            facts = {"scorer": name, "team": self.match.team_name[team], "minute": minute(self.period, self.t),
                     "score": self.score_line(), "xg": shot["q"]["xg"], "distance_m": shot["q"]["distance_m"],
                     "body_part": shot["q"]["body_part"], "shot_speed_kmh": shot["q"]["shot_speed_kmh"],
                     "set_piece": shot["q"].get("set_piece"), "scorer_goals": P.goals,
                     "assist": self.match.name(assist["player"]) if assist else None,
                     "team_xg": round(T.xg, 2), "opp_xg": round(self.teams[self.other(team)].xg, 2)}
            src = [e["id"], shot["id"]] + ([assist["id"]] if assist else [])
            cues.append(self.cue("scorebug", priority=3, ttl=12, sources=src, payload={
                "home": self.score[0], "away": self.score[1], "event": "goal", "scorer": self.match.short(pid), "minute": facts["minute"]}))
            moments.append(Moment("goal", self.match_s, self.frame, self.period, 3, facts, src, team, pid))
            if P.goals in (2, 3):
                moments.append(Moment("milestone", self.match_s, self.frame, self.period, 2,
                                      {"text_hint": "brace" if P.goals == 2 else "hat-trick", "player": name, "goals": P.goals}, src, team, pid))
        elif typ == "save" and team:
            shot = self.match.events[e["q"]["shot"]]
            if shot["q"]["xg"] >= 0.15:
                facts = {"keeper": name, "shooter": self.match.name(shot["player"]), "xg": shot["q"]["xg"],
                         "distance_m": shot["q"]["distance_m"], "shot_speed_kmh": shot["q"]["shot_speed_kmh"],
                         "minute": minute(self.period, self.t), "score": self.score_line()}
                moments.append(Moment("big_save", self.match_s, self.frame, self.period, 2, facts, [e["id"], shot["id"]], team, pid))
        elif typ == "pressure" and T is not None:
            T.pressures += 1
        elif typ == "tackle" and e["outcome"] == "won" and T is not None:
            T.tackles_won += 1
            P.tackles += 1
            self.turnover(e, team)
            self.high_action(e, team)
        elif typ == "interception" and T is not None:
            T.interceptions += 1
            P.interceptions += 1
            self.turnover(e, team)
            self.high_action(e, team)
        elif typ == "ball_recovery" and T is not None:
            self.turnover(e, team)
        elif typ == "foul" and T is not None:
            T.fouls += 1
            self.high_action(e, team)
            self.window.append((self.match_s, "foul", team, 1))
        elif typ == "corner" and T is not None:
            T.corners += 1
        elif typ == "card" and T is not None:
            if e["outcome"] == "yellow":
                T.yellows += 1
            else:
                T.reds += 1
                moments.append(Moment("red_card", self.match_s, self.frame, self.period, 3, {
                    "player": name, "team": self.match.team_name[team], "minute": minute(self.period, self.t),
                    "second_yellow": e["q"].get("second_yellow", False), "score": self.score_line()}, [e["id"]], team, pid))
            cues.append(self.cue("milestone", priority=2, ttl=8, sources=[e["id"]], team=team, player=pid, payload={
                "type": f"{e['outcome']}_card", "text": f"{self.match.short(pid)} {e['outcome']} card"}))
        elif typ == "carry" and P is not None:
            P.touches += 1
            if e["end_x"] is not None:
                sx, sy = self.attack_xy(e)
                ex, ey = self.attack_xy(e, "end")
                if sx <= 70 < ex:
                    T.final_third_entries += 1
                if not self.in_box(sx, sy) and self.in_box(ex, ey):
                    T.box_entries += 1
                    self.window.append((self.match_s, "box_entry", team, 1))
        elif typ == "period_end":
            moments.append(self.recap_moment("half_time" if e["q"]["period"] == 1 else "full_time", e))
        if team in self.teams and typ in ("pass", "carry", "shot", "dribble"):
            self.last_ball_event = e

        cues += self.periodic()
        return cues, moments

    def on_pass(self, e, T, P, name, cues, moments):
        T.passes += 1
        P.passes += 1
        P.touches += 1
        L = e["q"].get("length_m", 0.0)
        T.pass_m += L
        P.pass_m += L
        done = e["outcome"] == "complete"
        sx, sy = self.attack_xy(e)
        ex, ey = self.attack_xy(e, "end")
        if sx < 0.6 * LENGTH:
            self.teams[self.other(e["team"])].opp_passes_high += 1
        self.chain_passes[e["possession"]] += 1
        self.chain_team[e["possession"]] = e["team"]
        if done:
            T.complete += 1
            P.complete += 1
            if sx <= 70 < ex:
                T.final_third_entries += 1
            if not self.in_box(sx, sy) and self.in_box(ex, ey):
                T.box_entries += 1
                self.window.append((self.match_s, "box_entry", e["team"], 1))
        self.window.append((self.match_s, "pass", e["team"], 1 if done else 0))
        x = features(self.match, e)
        if x is None:
            return
        p_hat = self.model.completion(x)
        diff = 1 - p_hat
        truth = e.get("truth", {}).get("p_success")
        if truth is not None:
            self.difficulty_log.append((p_hat, truth, e["id"]))
        e.setdefault("live", {})["difficulty"] = round(diff, 3)
        if done:
            if diff > 0.45:
                P.hard_complete += 1
            if diff > P.hardest:
                P.hardest, P.hardest_ev = diff, e["id"]
        notable = done and (diff > 0.55 or (L > 38 and diff > 0.3) or (self.in_box(ex, ey) and diff > 0.4))
        if notable:
            reasons = [r for r, _ in self.model.explain(x)]
            payload = {"player": self.match.short(e["player"]), "distance_m": round(L, 1),
                       "ball_speed_kmh": e["q"].get("ball_speed_kmh"), "difficulty": round(diff, 2),
                       "accuracy_pct": round(100 * P.complete / max(P.passes, 1)),
                       "passes": P.passes, "why_hard": reasons, "height": e["q"].get("height"),
                       "recipient": self.match.short(e["q"].get("recipient"))}
            cues.append(self.cue("pass_card", priority=1, ttl=6, sources=[e["id"]], team=e["team"], player=e["player"],
                                 personas=("analyst", "studio"), metric="pass_difficulty",
                                 anchor={"x": e["end_x"], "y": e["end_y"], "player": e["q"].get("recipient")}, payload=payload))
            if diff > 0.7:
                moments.append(Moment("hard_pass", self.match_s, self.frame, self.period, 1, {
                    "passer": name, "recipient": self.match.name(e["q"].get("recipient")), "distance_m": round(L, 1),
                    "difficulty": round(diff, 2), "why_hard": reasons, "minute": minute(self.period, self.t),
                    "ball_speed_kmh": e["q"].get("ball_speed_kmh")}, [e["id"]], e["team"], e["player"]))

    def on_shot(self, e, T, P, name, cues, moments):
        T.shots += 1
        P.shots += 1
        xg = e["q"]["xg"]
        T.xg += xg
        P.xg += xg
        on_target = e["outcome"] in ("goal", "saved")
        T.sot += on_target
        self.window.append((self.match_s, "xg", e["team"], xg))
        prev = self.last_ball_event
        if prev and prev["type"] == "pass" and prev["team"] == e["team"] and prev["outcome"] == "complete":
            self.players[prev["player"]].key_passes += 1
        payload = {"player": self.match.short(e["player"]), "xg": round(xg, 2), "distance_m": e["q"]["distance_m"],
                   "shot_speed_kmh": e["q"]["shot_speed_kmh"], "body_part": e["q"]["body_part"], "outcome": e["outcome"],
                   "under_pressure": e["q"]["under_pressure"], "team_xg": round(T.xg, 2), "player_xg": round(P.xg, 2)}
        cues.append(self.cue("shot_card", priority=2, ttl=7, sources=[e["id"]], team=e["team"], player=e["player"],
                             anchor={"x": e["x"], "y": e["y"], "player": e["player"]}, payload=payload))
        if xg >= 0.2 and e["outcome"] != "goal":
            moments.append(Moment("big_chance", self.match_s, self.frame, self.period, 2, {
                "shooter": name, "team": self.match.team_name[e["team"]], "xg": round(xg, 2), "outcome": e["outcome"],
                "distance_m": e["q"]["distance_m"], "minute": minute(self.period, self.t), "score": self.score_line(),
                "body_part": e["q"]["body_part"]}, [e["id"]], e["team"], e["player"]))

    def assist_for(self, shot: dict) -> dict | None:
        for k in range(shot["id"] - 1, max(shot["id"] - 6, -1), -1):
            p = self.match.events[k]
            if p["type"] == "pass" and p["team"] == shot["team"] and p["outcome"] == "complete" and p["q"].get("recipient") == shot["player"]:
                return p
            if p["team"] not in (shot["team"], None) and p["type"] not in ("pressure",):
                return None
        return None

    def turnover(self, e, team):
        self.window.append((self.match_s, "turnover", team, 1))

    def high_action(self, e, team):
        if e["x"] is None:
            return
        # A defensive action high up the pitch: inside the opponent's first 60%.
        ox, _ = self.match.to_attack(self.other(team), e["period"], e["x"], e["y"])
        if ox < 0.6 * LENGTH:
            self.teams[team].def_actions_high += 1
            self.window.append((self.match_s, "press_win", team, 1))

    def possession_time(self, e):
        prev = self.last_ball_event
        if prev and e["team"] == prev["team"] and e["period"] == prev["period"]:
            dt = (e["t"] - (prev["t_end"] or prev["t"]))
            if 0 < dt < 20:
                self.teams[prev["team"]].possession_s += dt + ((prev["t_end"] or prev["t"]) - prev["t"])

    @staticmethod
    def in_box(x, y) -> bool:
        return x > LENGTH - 16.5 and abs(y - WIDTH / 2) < 20.16

    def score_line(self) -> str:
        m = self.match
        return f"{m.team_name['home']} {self.score[0]}-{self.score[1]} {m.team_name['away']}"

    # ----- explainers -------------------------------------------------------------------

    def explain(self) -> dict:
        """Control vs chaos, pressure and rhythm over the last five minutes."""
        now = self.match_s
        while self.window and now - self.window[0][0] > WINDOW_S:
            self.window.popleft()
        w = list(self.window)
        mins = max(min(now, WINDOW_S) / 60, 1.0)
        turnovers = sum(1 for _, k, _, _ in w if k == "turnover")
        fouls = sum(1 for _, k, _, _ in w if k == "foul")
        passes = [(t, v) for _, k, t, v in w if k == "pass"]
        comp = sum(v for _, v in passes) / max(len(passes), 1)
        by_team = {s: sum(1 for t, _ in passes if t == s) for s in ("home", "away")}
        xg = {s: sum(v for _, k, t, v in w if k == "xg" and t == s) for s in ("home", "away")}
        box = {s: sum(1 for _, k, t, _ in w if k == "box_entry" and t == s) for s in ("home", "away")}
        press = {s: sum(1 for _, k, t, _ in w if k == "press_win" and t == s) for s in ("home", "away")}
        chains = [n for n in list(self.chain_passes.values())[-30:]]
        mean_chain = float(np.mean(chains)) if chains else 0.0
        # Chaos rises with turnovers and fouls per minute; control rises with long, clean moves.
        chaos = (turnovers + 1.5 * fouls) / mins
        control = mean_chain * comp
        index = float(100 / (1 + np.exp(-(control - 1.1 * chaos + 0.8))))
        share = {s: by_team[s] / max(sum(by_team.values()), 1) for s in by_team}
        threat = {s: xg[s] + 0.04 * box[s] for s in xg}
        lead = max(threat, key=threat.get)
        if index >= 60:
            label = f"control:{max(share, key=share.get)}"
        elif index <= 35:
            label = "chaos"
        else:
            label = "balanced"
        tempo = len(passes) / mins
        reasons = []
        if turnovers >= 6:
            reasons.append(f"{turnovers} turnovers in the last {int(mins)} minutes")
        if mean_chain >= 5:
            reasons.append(f"moves averaging {mean_chain:.1f} passes")
        top = max(share, key=share.get)
        if share[top] >= 0.62:
            reasons.append(f"{self.match.team_name[top]} have {round(100 * share[top])}% of the passes")
        for s in ("home", "away"):
            if press[s] >= 4:
                reasons.append(f"{self.match.team_name[s]} won the ball high {press[s]} times")
        return {
            "index": round(index, 1), "label": label, "tempo_passes_per_min": round(tempo, 1),
            "completion": round(comp, 3), "turnovers": turnovers, "fouls": fouls, "mean_chain": round(mean_chain, 2),
            "pass_share": {s: round(v, 3) for s, v in share.items()}, "threat": {s: round(v, 2) for s, v in threat.items()},
            "momentum": lead if threat[lead] > 0.2 and threat[lead] > 2 * threat[self.other(lead)] else None,
            "press_wins": press, "reasons": reasons, "window_min": round(mins, 1),
        }

    def ppda(self, team: str) -> float | None:
        T = self.teams[team]
        return round(T.opp_passes_high / T.def_actions_high, 1) if T.def_actions_high else None

    def periodic(self) -> list[dict]:
        out = []
        if self.match_s - self.last_stat_s >= 120:
            self.last_stat_s = self.match_s
            ex = self.explain()
            label = ex["label"]
            if label != self.last_momentum_label:
                self.last_momentum_label = label
                out.append(self.cue("momentum", priority=1, ttl=10, sources=[], payload=ex, personas=("all",)))
            h, a = self.teams["home"], self.teams["away"]
            tot = max(h.possession_s + a.possession_s, 1)
            out.append(self.cue("stat", priority=0, ttl=8, sources=[], personas=("analyst", "studio"), payload={
                "possession_pct": {"home": round(100 * h.possession_s / tot), "away": round(100 * a.possession_s / tot)},
                "xg": {"home": round(h.xg, 2), "away": round(a.xg, 2)},
                "shots": {"home": h.shots, "away": a.shots},
                "pass_completion_pct": {"home": round(100 * h.complete / max(h.passes, 1)), "away": round(100 * a.complete / max(a.passes, 1))},
                "ppda": {"home": self.ppda("home"), "away": self.ppda("away")},
            }))
        return out

    def momentum_moment(self) -> Moment | None:
        ex = self.explain()
        if ex["label"] == "chaos" or ex["momentum"]:
            return Moment("momentum", self.match_s, self.frame, self.period, 1, ex, [], ex["momentum"], None)
        return None

    def recap_moment(self, kind: str, e: dict) -> Moment:
        h, a = self.teams["home"], self.teams["away"]
        tot = max(h.possession_s + a.possession_s, 1)
        scorers = [ev for ev in self.match.events[: e["id"]] if ev["type"] == "goal"]
        best = sorted(self.players.items(), key=lambda kv: -(kv[1].xg + 0.3 * kv[1].key_passes + 0.05 * kv[1].hard_complete))[:3]
        facts = {
            "score": self.score_line(), "kind": kind,
            "goals": [{"player": self.match.name(g["player"]), "team": self.match.team_name[g["team"]],
                       "minute": minute(g["period"], g["t"]), "event": g["id"]} for g in scorers],
            "xg": {"home": round(h.xg, 2), "away": round(a.xg, 2)},
            "shots": {"home": h.shots, "away": a.shots}, "on_target": {"home": h.sot, "away": a.sot},
            "possession_pct": {"home": round(100 * h.possession_s / tot), "away": round(100 * a.possession_s / tot)},
            "passes": {"home": h.passes, "away": a.passes},
            "pass_completion_pct": {"home": round(100 * h.complete / max(h.passes, 1)), "away": round(100 * a.complete / max(a.passes, 1))},
            "ppda": {"home": self.ppda("home"), "away": self.ppda("away")},
            "standouts": [{"player": self.match.name(pid), "xg": round(p.xg, 2), "key_passes": p.key_passes,
                           "hard_passes": p.hard_complete, "goals": p.goals, "top_kmh": round(p.top_kmh, 1),
                           "km": round(p.dist_m / 1000, 1)} for pid, p in best],
            "teams": {"home": self.match.team_name["home"], "away": self.match.team_name["away"]},
        }
        return Moment(kind, self.match_s, self.frame, self.period, 3, facts, [g["id"] for g in scorers] + [e["id"]])

    # ----- tracking ---------------------------------------------------------------------

    def track(self, frame_idx: int, xy: np.ndarray, period: int, t: float) -> list[dict]:
        """Feed one tracking sample (taken every few frames). Updates distance and speed; emits
        a player tag when someone near the ball hits sprint speed or passes a distance landmark."""
        self.frame = frame_idx
        self.period = period
        self.t = t
        cues = []
        players = xy[:22].astype(float)
        if self.prev_frame_xy is not None and self.prev_frame_xy[1] == period:
            dt = (frame_idx - self.prev_frame_xy[2]) / FPS
            step = np.linalg.norm(players - self.prev_frame_xy[0], axis=1)
            step[step > 12 * dt + 0.5] = 0           # substitutions and send-offs jump
            sp = step / max(dt, 1e-6) * 3.6
            self.speed = 0.6 * self.speed + 0.4 * sp
            ball = xy[22]
            for s in range(22):
                pid = self.slot_player.get(s)
                if pid is None:
                    continue
                P = self.players[pid]
                P.dist_m += float(step[s])
                kmh = float(self.speed[s])
                if kmh > P.top_kmh:
                    P.top_kmh = kmh
                near_ball = float(np.linalg.norm(players[s] - ball)) < 15
                if kmh >= SPRINT_KMH and near_ball and self.match_s - P.last_tag_s > TAG_COOLDOWN_S:
                    P.sprints += 1
                    P.last_tag_s = self.match_s
                    cues.append(self.cue("player_tag", priority=1, ttl=4, sources=[], player=pid,
                                         team=self.match.players[pid]["team"], metric="speed",
                                         anchor={"x": float(players[s][0]), "y": float(players[s][1]), "player": pid},
                                         payload={"player": self.match.short(pid), "number": self.match.players[pid]["number"],
                                                  "speed_kmh": round(kmh, 1), "top_kmh": round(P.top_kmh, 1)}))
                km = int(P.dist_m // 5000) * 5
                if km > P.km_tagged and km > 0:
                    P.km_tagged = km
                    cues.append(self.cue("player_tag", priority=0, ttl=4, sources=[], player=pid,
                                         team=self.match.players[pid]["team"], metric="distance",
                                         anchor={"x": float(players[s][0]), "y": float(players[s][1]), "player": pid},
                                         payload={"player": self.match.short(pid), "number": self.match.players[pid]["number"],
                                                  "distance_km": km}))
        self.prev_frame_xy = (players, period, frame_idx)
        return cues

    def snapshot(self) -> dict:
        return {
            "match_id": self.match.id, "period": self.period, "clock": clock(self.period, self.t),
            "match_s": round(self.match_s, 1), "score": list(self.score),
            "teams": {s: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in vars(T).items()} for s, T in self.teams.items()},
            "explain": self.explain(),
        }
