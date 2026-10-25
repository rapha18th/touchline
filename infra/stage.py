"""Stage a minimal Docker build context for the Touchline image.

The Dockerfile is the manifest. Every path it COPYs from the context is staged and nothing else is,
so the context cannot drift from the image. data/matches is narrowed to the showcase matches named
by the Dockerfile's SEEDS argument (TL-000007, TL-000008 and TL-000011 today): the ones whose
recorded broadcasts ship in broadcasts/.

    python infra/stage.py                       # stage into %TEMP%/touchline-build, print its path
    python infra/stage.py --out DIR             # stage somewhere else
    python infra/stage.py --matches DIR         # read the matches from DIR
    python infra/stage.py --allow-missing       # stage without missing matches; the build generates them

The staged path is the only line on stdout, so a shell can capture it:  STAGE=$(python infra/stage.py)
The summary goes to stderr: file count, bytes on disk and the gzip size az acr build uploads.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MATCHES = "data/matches"
MARKER = ".touchline-stage.json"
JUNK = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.egg-info", ".pytest_cache", ".mypy_cache",
                              ".ruff_cache", "node_modules", ".DS_Store", "Thumbs.db")
# Files inside copied folders that never ship: the vision capture harness drives pitch.js frame by frame.
NOT_SHIPPED = {"web/capture.html"}


def instructions(dockerfile: Path) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, with line continuations joined and comments dropped."""
    out, buf = [], ""
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        word, _, rest = buf.partition(" ")
        out.append((word.upper(), rest.strip()))
        buf = ""
    return out


def copy_sources(dockerfile: Path) -> list[str]:
    """Every context path the Dockerfile COPYs or ADDs (multi-stage --from copies and URLs excluded)."""
    srcs: list[str] = []
    for word, rest in instructions(dockerfile):
        if word not in ("COPY", "ADD"):
            continue
        tokens = shlex.split(rest)
        flags = [t for t in tokens if t.startswith("--")]
        if any(f.startswith("--from") for f in flags):
            continue
        args = [t for t in tokens if not t.startswith("--")]
        if args and args[0].startswith("["):
            args = json.loads(rest[rest.index("["):])
        for s in args[:-1]:
            if re.match(r"^[a-z]+://", s):
                continue
            s = str(PurePosixPath(s.replace("\\", "/")))
            if s not in srcs:
                srcs.append(s)
    return srcs


def showcase_ids(dockerfile: Path) -> list[str]:
    """Match ids from the Dockerfile's ARG SEEDS default."""
    for word, rest in instructions(dockerfile):
        m = re.match(r'^SEEDS=["\']?([0-9 ]*)["\']?$', rest) if word == "ARG" else None
        if m:
            return [f"TL-{int(s):06d}" for s in m.group(1).split()]
    return []


def main_checkout() -> Path | None:
    """The main checkout when this is a git worktree (data/ lives there only)."""
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        return Path(common).parent
    except (OSError, subprocess.CalledProcessError):
        return None


def find_matches(given: str | None, ids: list[str]) -> Path | None:
    if given:
        return Path(given)
    candidates = [os.environ.get("TOUCHLINE_DATA"), ROOT / MATCHES]
    main = main_checkout()
    if main:
        candidates.append(main / MATCHES)
    for c in candidates:
        if c and all((Path(c) / i / "events.json").exists() for i in ids):
            return Path(c)
    return next((Path(c) for c in candidates if c and Path(c).is_dir()), None)


def prepare(out: Path) -> None:
    """Empty the stage directory, refusing to touch a folder this script did not create."""
    if out.exists():
        if any(out.iterdir()) and not (out / MARKER).exists():
            sys.exit(f"refusing to clear {out}: it is not empty and was not staged by infra/stage.py")
        shutil.rmtree(out)
    out.mkdir(parents=True)


def stage_path(src: str, out: Path) -> None:
    from_path = ROOT / src
    if any(ch in src for ch in "*?["):
        hits = sorted(ROOT.glob(src))
        if not hits:
            sys.exit(f"Dockerfile copies {src}, which matches nothing in {ROOT}")
        for h in hits:
            stage_path(h.relative_to(ROOT).as_posix(), out)
        return
    if not from_path.exists():
        sys.exit(f"Dockerfile copies {src}, which is missing from {ROOT}")
    dest = out / src
    dest.parent.mkdir(parents=True, exist_ok=True)
    if from_path.is_dir():
        shutil.copytree(from_path, dest, ignore=JUNK, dirs_exist_ok=True)
        for rel in NOT_SHIPPED:
            if PurePosixPath(rel).is_relative_to(PurePosixPath(src)):
                (out / rel).unlink(missing_ok=True)
    else:
        shutil.copy2(from_path, dest)


def stage_matches(source: Path | None, ids: list[str], out: Path, allow_missing: bool) -> list[dict]:
    dest = out / MATCHES
    dest.mkdir(parents=True, exist_ok=True)
    staged, missing = [], []
    for mid in ids:
        src = source / mid if source else None
        if not src or not (src / "events.json").exists() or not (src / "tracking.npz").exists():
            missing.append(mid)
            continue
        shutil.copytree(src, dest / mid, ignore=JUNK)
        digest = hashlib.sha256((src / "events.json").read_bytes()).hexdigest()[:16]
        staged.append({"id": mid, "events_sha256_16": digest})
    if missing and not allow_missing:
        sys.exit(f"showcase matches missing from {source}: {', '.join(missing)} "
                 "(pass --matches DIR, set TOUCHLINE_DATA, or --allow-missing to let the build generate them)")
    if missing:
        print(f"warning: {', '.join(missing)} not staged; the image build will generate them", file=sys.stderr)
    if not any(dest.iterdir()):
        (dest / ".keep").write_text("", encoding="utf-8")   # COPY needs the folder to exist
    return staged


class _Count:
    def __init__(self) -> None:
        self.n = 0

    def write(self, b: bytes) -> int:
        self.n += len(b)
        return len(b)

    def flush(self) -> None:
        pass


def gzip_size(out: Path) -> int:
    """Bytes of a gzip tarball of the stage, the way az acr build packs a local folder for upload."""
    sink = _Count()
    with gzip.GzipFile(fileobj=sink, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tar:
            for p in sorted(out.rglob("*")):
                tar.add(p, arcname=p.relative_to(out).as_posix(), recursive=False)
    return sink.n


def git_rev() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return rev + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "touchline-build"))
    ap.add_argument("--dockerfile", default=str(ROOT / "Dockerfile"))
    ap.add_argument("--matches", help="folder holding TL-xxxxxx match folders (default: TOUCHLINE_DATA, "
                                      "data/matches here, then data/matches in the main checkout)")
    ap.add_argument("--ids", nargs="*", help="match ids to stage (default: the Dockerfile's SEEDS)")
    ap.add_argument("--allow-missing", action="store_true", help="stage without matches the source lacks")
    ap.add_argument("--no-measure", action="store_true", help="skip the gzip size measurement")
    a = ap.parse_args()

    dockerfile = Path(a.dockerfile)
    ids = a.ids if a.ids is not None else showcase_ids(dockerfile)
    out = Path(a.out).resolve()
    prepare(out)
    shutil.copy2(dockerfile, out / "Dockerfile")
    srcs = copy_sources(dockerfile)
    for src in srcs:
        if src == MATCHES or src.startswith(MATCHES + "/"):
            continue
        stage_path(src, out)
    source = find_matches(a.matches, ids)
    matches = stage_matches(source, ids, out, a.allow_missing) if any(
        s == MATCHES or s.startswith(MATCHES + "/") for s in srcs) else []

    for b in sorted((out / "broadcasts").glob("*.cues.jsonl")):
        if b.name.split(".")[0] not in ids:
            print(f"warning: {b.name} is staged but its match is not among {' '.join(ids)}", file=sys.stderr)

    files = [p for p in out.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    by_top: dict[str, int] = {}
    for p in files:
        top = p.relative_to(out).parts[0]
        by_top[top] = by_top.get(top, 0) + p.stat().st_size
    manifest = {
        "rev": git_rev(), "dockerfile_copies": srcs, "matches_source": str(source) if source else None,
        "matches": matches, "files": len(files), "bytes": total, "bytes_by_top": by_top,
    }
    if not a.no_measure:
        manifest["gzip_bytes"] = gzip_size(out)
    (out / MARKER).write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    print(f"staged {len(files)} files, {mb(total)} on disk"
          + (f", {mb(manifest['gzip_bytes'])} gzipped" if "gzip_bytes" in manifest else ""), file=sys.stderr)
    for top, n in sorted(by_top.items(), key=lambda kv: -kv[1]):
        print(f"  {top:<12} {mb(n):>9}", file=sys.stderr)
    print(f"  matches from {source}: {', '.join(m['id'] for m in matches) or 'none'}", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    main()
