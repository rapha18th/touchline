"""Command line: generate matches and the realism report.

    python -m touchline_engine generate --seed 7 --out ../data/matches
    python -m touchline_engine realism --n 20 --out ../reports/realism.json
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import export, realism
from .sim import Match


def main() -> None:
    ap = argparse.ArgumentParser(prog="touchline_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="play matches and write meta, events and tracking")
    g.add_argument("--seed", type=int, default=1)
    g.add_argument("--count", type=int, default=1)
    g.add_argument("--out", default="data/matches")
    r = sub.add_parser("realism", help="score generated matches against league base rates")
    r.add_argument("--n", type=int, default=20)
    r.add_argument("--start", type=int, default=1)
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.cmd == "generate":
        for seed in range(a.seed, a.seed + a.count):
            t0 = time.perf_counter()
            m = Match(seed).play()
            path = export.write(m, Path(a.out) / export.match_id(seed))
            home, away = m.teams
            print(f"{path}  {home.name} {m.score[0]}-{m.score[1]} {away.name}  "
                  f"{len(m.events)} events  {sum(m.period_frames)} frames  {time.perf_counter() - t0:.0f}s")
    else:
        rep = realism.run(list(range(a.start, a.start + a.n)), workers=a.workers)
        print(realism.table(rep))
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            realism.save(rep, a.out)


if __name__ == "__main__":
    main()
