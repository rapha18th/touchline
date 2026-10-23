"""Fictional clubs, squads, attributes and formations.

Every name is generated from syllable banks, so no real player or club appears. A seed
fixes the league: the same seed always yields the same clubs and the same people.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

FIRST = [
    "Ade", "Tomas", "Kofi", "Luca", "Rui", "Mateo", "Jonah", "Idris", "Noah", "Elias", "Kai", "Sami",
    "Theo", "Bruno", "Yusuf", "Diego", "Oskar", "Femi", "Malik", "Arlo", "Joel", "Nico", "Tariq",
    "Emeka", "Liam", "Rafael", "Jules", "Ibrahim", "Callum", "Marek", "Tendai", "Sipho", "Kenji",
    "Hugo", "Aaron", "Dario", "Felix", "Omar", "Lewin", "Ruben", "Zane", "Ilan", "Kwame", "Milo",
]
LAST_A = ["Ok", "Mar", "Bel", "Dra", "Hal", "Ken", "Lor", "Mens", "Nda", "Ost", "Pel", "Qua", "Ros",
          "Sav", "Tor", "Vel", "Wes", "Yar", "Zim", "Cal", "Fen", "Gar", "Ish", "Jov", "Kar", "Lun"]
LAST_B = ["ah", "ane", "ard", "ek", "ell", "enko", "ers", "ez", "ford", "ic", "ido", "in", "ley",
          "lin", "o", "ola", "ova", "ski", "son", "ton", "ura", "well", "wu", "yi", "ani", "ette"]

CITIES = [
    ("Harbourne", "HAR"), ("Kestrel Vale", "KES"), ("Northgate", "NTG"), ("Ashbury", "ASH"),
    ("Corran", "COR"), ("Lowmoor", "LOW"), ("Redwater", "RED"), ("Saltbridge", "SAL"),
    ("Thornfield", "THO"), ("Whitcombe", "WHI"), ("Elmstead", "ELM"), ("Fallowmere", "FAL"),
]
SUFFIX = ["Athletic", "United", "Rovers", "City", "Albion", "Town", "Wanderers", "FC"]
KITS = [
    ("#c8102e", "#ffffff"), ("#1d428a", "#ffffff"), ("#00704a", "#f2c400"), ("#f28c28", "#111111"),
    ("#6a1b9a", "#ffffff"), ("#111111", "#e5e5e5"), ("#e4e4e4", "#b3122e"), ("#0b8fd0", "#0b1f3a"),
]

# Formation slots: (role, x01 from own goal line to front line, y01 across the pitch), GK first.
FORMATIONS = {
    "4-3-3": [
        ("GK", 0.0, 0.5),
        ("RB", 0.05, 0.12), ("CB", 0.0, 0.37), ("CB", 0.0, 0.63), ("LB", 0.05, 0.88),
        ("CM", 0.42, 0.5), ("CM", 0.55, 0.3), ("CM", 0.55, 0.7),
        ("RW", 0.92, 0.12), ("ST", 1.0, 0.5), ("LW", 0.92, 0.88),
    ],
    "4-2-3-1": [
        ("GK", 0.0, 0.5),
        ("RB", 0.05, 0.12), ("CB", 0.0, 0.37), ("CB", 0.0, 0.63), ("LB", 0.05, 0.88),
        ("DM", 0.35, 0.38), ("DM", 0.35, 0.62),
        ("RW", 0.75, 0.14), ("AM", 0.75, 0.5), ("LW", 0.75, 0.86), ("ST", 1.0, 0.5),
    ],
    "3-5-2": [
        ("GK", 0.0, 0.5),
        ("CB", 0.0, 0.25), ("CB", 0.0, 0.5), ("CB", 0.0, 0.75),
        ("RWB", 0.5, 0.06), ("CM", 0.45, 0.33), ("DM", 0.33, 0.5), ("CM", 0.45, 0.67), ("LWB", 0.5, 0.94),
        ("ST", 1.0, 0.4), ("ST", 1.0, 0.6),
    ],
}

ROLE_GROUP = {"GK": "GK", "CB": "DEF", "RB": "DEF", "LB": "DEF", "RWB": "DEF", "LWB": "DEF",
              "DM": "MID", "CM": "MID", "AM": "MID", "RW": "ATT", "LW": "ATT", "ST": "ATT"}


@dataclass
class Player:
    id: str
    name: str
    short: str
    number: int
    role: str
    pace: float        # top speed, m/s
    passing: float     # 0..1
    finishing: float
    dribbling: float
    tackling: float
    stamina: float
    heading: float

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class Style:
    press: float       # 0 deep block .. 1 high press
    directness: float  # 0 patient .. 1 direct
    width: float       # 0 narrow .. 1 wide
    tempo: float       # 0 slow .. 1 fast ball circulation
    risk: float        # 0 safe .. 1 brave in the final third

    def as_dict(self) -> dict:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


@dataclass
class Team:
    id: str
    name: str
    short: str
    kit: tuple[str, str]
    formation: str
    style: Style
    strength: float
    players: list[Player] = field(default_factory=list)  # 11 starters in slot order, then 7 subs

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "short": self.short,
            "kit": {"primary": self.kit[0], "secondary": self.kit[1]},
            "formation": self.formation, "style": self.style.as_dict(), "strength": round(self.strength, 3),
            "starters": [p.id for p in self.players[:11]], "bench": [p.id for p in self.players[11:]],
            "players": [p.as_dict() for p in self.players],
        }


def _name(rng: np.random.Generator, used: set[str]) -> tuple[str, str]:
    while True:
        first = FIRST[rng.integers(len(FIRST))]
        last = LAST_A[rng.integers(len(LAST_A))] + LAST_B[rng.integers(len(LAST_B))]
        last = last[0].upper() + last[1:]
        if last not in used:
            used.add(last)
            return f"{first} {last}", last


def _player(rng, team_id: str, idx: int, role: str, number: int, strength: float, used: set[str]) -> Player:
    name, short = _name(rng, used)
    g = ROLE_GROUP[role]
    base = 0.45 + 0.25 * strength
    r = lambda lo=-0.12, hi=0.12: float(np.clip(base + rng.uniform(lo, hi), 0.15, 0.97))
    pace = {"GK": 7.2, "DEF": 8.6, "MID": 8.5, "ATT": 9.0}[g] + rng.normal(0, 0.3) + 0.3 * strength
    return Player(
        id=f"{team_id}-{idx:02d}", name=name, short=short, number=number, role=role,
        pace=float(np.clip(pace, 6.8, 9.9)),
        passing=r() + (0.08 if g == "MID" else 0.0),
        finishing=r() + (0.12 if g == "ATT" else -0.1 if g in ("DEF", "GK") else 0.0),
        dribbling=r() + (0.1 if g == "ATT" else 0.0),
        tackling=r() + (0.12 if g == "DEF" else -0.15 if g == "ATT" else 0.0),
        stamina=r(),
        heading=r() + (0.1 if role in ("CB", "ST") else 0.0),
    )


def make_team(rng: np.random.Generator, team_id: str, city: tuple[str, str], kit, used: set[str]) -> Team:
    formation = list(FORMATIONS)[rng.integers(len(FORMATIONS))]
    strength = float(rng.uniform(0.3, 0.7))
    style = Style(*[float(rng.uniform(0.15, 0.9)) for _ in range(5)])
    team = Team(
        id=team_id, name=f"{city[0]} {SUFFIX[rng.integers(len(SUFFIX))]}", short=city[1], kit=kit,
        formation=formation, style=style, strength=strength,
    )
    numbers = iter(rng.permutation(np.arange(2, 40))[:17].tolist())
    for i, (role, _, _) in enumerate(FORMATIONS[formation]):
        team.players.append(_player(rng, team_id, i, role, 1 if role == "GK" else next(numbers), strength, used))
    bench_roles = ["GK", "CB", "RB", "CM", "CM", "RW", "ST"]
    for k, role in enumerate(bench_roles):
        team.players.append(_player(rng, team_id, 11 + k, role, 13 if role == "GK" else next(numbers), strength - 0.08, used))
    return team


def make_fixture(seed: int) -> tuple[Team, Team]:
    """Two fictional clubs for one match, both fixed by the seed."""
    rng = np.random.default_rng([seed, 0xC1A55])
    used: set[str] = set()
    ci = rng.choice(len(CITIES), 2, replace=False)
    ki = rng.choice(len(KITS), 2, replace=False)
    home = make_team(rng, "home", CITIES[ci[0]], KITS[ki[0]], used)
    away = make_team(rng, "away", CITIES[ci[1]], KITS[ki[1]], used)
    return home, away
