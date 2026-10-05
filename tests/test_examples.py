"""The examples/ folder is runnable as documented in the README."""
import json
import subprocess
import sys
from pathlib import Path

EX = Path(__file__).resolve().parent.parent / "examples"


def _doctor(tmp_path, *extra):
    args = [sys.executable, "-m", "stack_guardian", "doctor", "--state", str(tmp_path / "state.json"), "--json", *extra]
    return subprocess.run(args, capture_output=True, text=True, timeout=60, cwd=str(EX.parent))


def test_example_files_are_valid_json():
    for name in ("nodes.json", "ctx.json", "snapshot.json", "lanes.json"):
        json.loads((EX / name).read_text(encoding="utf-8"))


def test_shipped_rules_on_example_snapshot(tmp_path):
    r = _doctor(tmp_path, "--snapshot", str(EX / "snapshot.json"), "--ctx", str(EX / "ctx.json"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert json.loads(r.stdout)["findings"][0]["code"] == "ollama.unreachable"


def test_example_fix_dry_run_then_apply(tmp_path):
    lanes = json.loads((EX / "lanes.json").read_text(encoding="utf-8"))
    lanes["lanes"]["atlas_ollama"][0] = sys.executable  # a bare "python" may not be on PATH in CI or on Windows
    cfg = tmp_path / "lanes.json"
    cfg.write_text(json.dumps(lanes), encoding="utf-8")
    base = ("--checks-dir", str(EX / "checks.d"), "--snapshot", str(EX / "snapshot.json"),
            "--fix", "atlas.stuck", "--config", str(cfg))
    dry = _doctor(tmp_path, *base)
    assert dry.returncode == 0 and json.loads(dry.stdout)["applied"] is False
    done = _doctor(tmp_path, *base, "--apply")
    assert done.returncode == 0 and json.loads(done.stdout)["detail"] == "rc 0"
    assert len((tmp_path / "fixes.jsonl").read_text().splitlines()) == 1
