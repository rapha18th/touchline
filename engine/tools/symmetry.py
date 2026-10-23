"""Side-bias check: play each fixture twice with the clubs swapped and compare by side.

With home advantage worth only a small strength bump, xG for the home side and the away
side should come out close; a large gap points to a geometry bug in one direction.
"""

import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from touchline_engine.sim import Match  # noqa: E402
from touchline_engine.squads import make_fixture  # noqa: E402


def side_stats(m: Match) -> list[tuple[float, int, int]]:
    out = []
    for t in (0, 1):
        tid = m.teams[t].id
        shots = [e for e in m.events if e["type"] == "shot" and e["team"] == tid]
        out.append((sum(e["q"]["xg"] for e in shots), len(shots), m.score[t]))
    return out


def one(seed: int):
    a, b = make_fixture(seed)
    normal = side_stats(Match(seed, a, b).play())
    a, b = make_fixture(seed)
    a.id, b.id = "away", "home"
    for p in a.players:
        p.id = "away" + p.id[4:]
    for p in b.players:
        p.id = "home" + p.id[4:]
    swapped = side_stats(Match(seed, b, a).play())
    return normal, swapped


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    with ProcessPoolExecutor(12) as ex:
        res = list(ex.map(one, range(1, n + 1)))
    hx = sum(r[0][0][0] + r[1][0][0] for r in res)
    ax = sum(r[0][1][0] + r[1][1][0] for r in res)
    hs = sum(r[0][0][1] + r[1][0][1] for r in res)
    as_ = sum(r[0][1][1] + r[1][1][1] for r in res)
    hg = sum(r[0][0][2] + r[1][0][2] for r in res)
    ag = sum(r[0][1][2] + r[1][1][2] for r in res)
    m = 2 * n
    print(f"{m} matches  home xG {hx / m:.2f} shots {hs / m:.1f} goals {hg / m:.2f}  |  away xG {ax / m:.2f} shots {as_ / m:.1f} goals {ag / m:.2f}")
