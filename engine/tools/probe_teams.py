"""Per-team breakdown of shots, xG and passing against style and strength, for tuning."""

import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from touchline_engine.sim import Match  # noqa: E402


def one(seed: int) -> list[str]:
    m = Match(seed).play()
    rows = []
    for t in (0, 1):
        tid = m.teams[t].id
        shots = [e for e in m.events if e["type"] == "shot" and e["team"] == tid]
        passes = [e for e in m.events if e["type"] == "pass" and e["team"] == tid]
        comp = sum(e["outcome"] == "complete" for e in passes) / max(len(passes), 1)
        s = m.teams[t].style
        rows.append(
            f"{seed:3d} {tid:4s} {m.teams[t].formation:7s} str {m.teams[t].strength:.2f} "
            f"press {s.press:.2f} dir {s.directness:.2f} risk {s.risk:.2f} tempo {s.tempo:.2f} | "
            f"shots {len(shots):3d} xg {sum(e['q']['xg'] for e in shots):5.2f} goals {m.score[t]:2d} "
            f"passes {len(passes):4d} comp {comp:.2f} poss {m.possessions[t]}"
        )
    return rows


if __name__ == "__main__":
    seeds = range(int(sys.argv[1]) if len(sys.argv) > 1 else 1, int(sys.argv[2]) if len(sys.argv) > 2 else 11)
    with ProcessPoolExecutor(10) as ex:
        for rows in ex.map(one, seeds):
            print("\n".join(rows))
