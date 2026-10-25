"""infra/stage.py: the staged build context holds what the Dockerfile copies and nothing more."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("stage", ROOT / "infra" / "stage.py")
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


def test_the_dockerfile_names_the_three_showcase_matches():
    assert stage.showcase_ids(ROOT / "Dockerfile") == ["TL-000007", "TL-000008", "TL-000011"]


def test_copy_sources_are_the_dockerfile_copies():
    srcs = stage.copy_sources(ROOT / "Dockerfile")
    assert "data/matches" in srcs and "web" in srcs and "schema" in srcs and "broadcasts" in srcs
    assert all(not s.startswith((".", "reports", "scripts", "infra")) for s in srcs)


def test_copy_sources_reads_flags_json_form_and_continuations(tmp_path):
    df = tmp_path / "Dockerfile"
    df.write_text("FROM x AS a\nCOPY --chown=1:1 a b /dst/\nCOPY --from=a /x /y\n"
                  'COPY ["c d", "/e"]\nADD https://example.com/f /g\nCOPY h \\\n  /i\n', encoding="utf-8")
    assert stage.copy_sources(df) == ["a", "b", "c d", "h"]


def run_stage(tmp_path: Path, matches: Path) -> tuple[Path, dict]:
    out = tmp_path / "ctx"
    got = subprocess.run([sys.executable, str(ROOT / "infra" / "stage.py"), "--out", str(out), "--matches",
                          str(matches), "--allow-missing"], capture_output=True, text=True, check=True)
    assert got.stdout.strip() == str(out.resolve())
    return out, json.loads((out / stage.MARKER).read_text(encoding="utf-8"))


def fake_match(root: Path, mid: str) -> None:
    (root / mid).mkdir(parents=True)
    (root / mid / "events.json").write_text("[]", encoding="utf-8")
    (root / mid / "tracking.npz").write_bytes(b"0" * 10)
    (root / mid / "meta.json").write_text("{}", encoding="utf-8")


def test_stage_takes_only_the_showcase_matches(tmp_path):
    src = tmp_path / "matches"
    for mid in ("TL-000007", "TL-000008", "TL-000009", "TL-000011", "TL-000012"):
        fake_match(src, mid)
    out, manifest = run_stage(tmp_path, src)
    tops = sorted(p.name for p in out.iterdir())
    assert tops == sorted([stage.MARKER, "Dockerfile", "broadcasts", "data", "engine", "schema", "services", "web"])
    assert sorted(p.name for p in (out / "data" / "matches").iterdir()) == ["TL-000007", "TL-000008", "TL-000011"]
    assert [m["id"] for m in manifest["matches"]] == ["TL-000007", "TL-000008", "TL-000011"]
    assert manifest["gzip_bytes"] > 0
    assert not list(out.rglob("__pycache__"))


def test_stage_without_matches_keeps_the_folder_the_dockerfile_copies(tmp_path):
    out, manifest = run_stage(tmp_path, tmp_path / "empty")
    assert manifest["matches"] == []
    assert (out / "data" / "matches" / ".keep").exists()


def test_stage_refuses_to_clear_a_folder_it_did_not_make(tmp_path):
    out = tmp_path / "precious"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(SystemExit):
        stage.prepare(out)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "mine"
