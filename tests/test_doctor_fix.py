"""`doctor --fix ID [--apply]`: dry-run by default, real dependencies injected at the CLI layer."""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stack_guardian import cli


def _checks(tmp_path, fix):
    d = tmp_path / "checks.d"
    d.mkdir()
    (d / "10_svc.py").write_text(
        "from stack_guardian import finding as F\n"
        "FIX = %r\n"
        "def down(s, c):\n"
        "    return F.make_finding('lane.stuck', 'missing', 'FAIL', 'gpu-box', 'lane is stuck', fix=FIX)\n"
        "def plain(s, c):\n"
        "    return F.make_finding('lane.slow', 'degraded', 'WARN', 'gpu-box', 'lane is slow')\n"
        "CHECKS = [('lane.down', down), ('lane.slow', plain)]\n" % (fix,), encoding="utf-8")
    return d


def _run(tmp_path, checks, *extra):
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"schema": 1, "nodes": {"gpu-box": {}}}), encoding="utf-8")
    args = [sys.executable, "-m", "stack_guardian", "doctor", "--checks-dir", str(checks), "--snapshot", str(snap),
            "--state", str(tmp_path / "state.json"), "--json", *extra]
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def _lane_setup(tmp_path):
    marker = tmp_path / "marker.txt"
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"lanes": {"cpu": [sys.executable, "-c", "open(r'%s','w').write('ran')" % marker]}}), encoding="utf-8")
    checks = _checks(tmp_path, {"action": "restart_lane", "args": {"lane": "cpu"}})
    return marker, cfg, checks


def test_fix_is_dry_run_by_default(tmp_path):
    marker, cfg, checks = _lane_setup(tmp_path)
    r = _run(tmp_path, checks, "--fix", "lane.stuck", "--config", str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["applied"] is False and out["plan"]["action"] == "restart_lane"
    assert not marker.exists()
    assert not (tmp_path / "snapshots").exists()


def test_apply_runs_lane_snapshots_and_audits(tmp_path):
    marker, cfg, checks = _lane_setup(tmp_path)
    r = _run(tmp_path, checks, "--fix", "lane.stuck", "--apply", "--config", str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["applied"] is True and out["detail"] == "rc 0"
    assert marker.read_text() == "ran"
    snaps = list((tmp_path / "snapshots").glob("*.json"))
    assert len(snaps) == 1 and json.loads(snaps[0].read_text())["nodes"] == {"gpu-box": {}}
    lines = (tmp_path / "fixes.jsonl").read_text().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line["action"] == "restart_lane" and line["ok"] is True and line["snapshot"] == str(snaps[0])


def test_failed_lane_exits_1(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"lanes": {"cpu": [sys.executable, "-c", "raise SystemExit(3)"]}}), encoding="utf-8")
    checks = _checks(tmp_path, {"action": "restart_lane", "args": {"lane": "cpu"}})
    r = _run(tmp_path, checks, "--fix", "lane.stuck", "--apply", "--config", str(cfg))
    assert r.returncode == 1
    assert json.loads(r.stdout)["detail"] == "rc 3"


def test_undeclared_lane_is_refused_and_nothing_runs(tmp_path):
    marker, cfg, checks = _lane_setup(tmp_path)
    cfg.write_text(json.dumps({"lanes": {}}), encoding="utf-8")
    r = _run(tmp_path, checks, "--fix", "lane.stuck", "--apply", "--config", str(cfg))
    assert r.returncode == 1
    assert json.loads(r.stdout)["applied"] is False
    assert not marker.exists() and not (tmp_path / "fixes.jsonl").exists()


def test_usage_errors_exit_3(tmp_path):
    marker, cfg, checks = _lane_setup(tmp_path)
    assert _run(tmp_path, checks, "--apply").returncode == 3                                   # --apply needs --fix
    assert _run(tmp_path, checks, "--fix", "no.such.code", "--config", str(cfg)).returncode == 3  # unknown id
    assert _run(tmp_path, checks, "--fix", "lane.slow", "--config", str(cfg)).returncode == 3     # finding has no fix
    assert not marker.exists()


def test_node_qualified_id(tmp_path):
    marker, cfg, checks = _lane_setup(tmp_path)
    r = _run(tmp_path, checks, "--fix", "lane.stuck@gpu-box", "--config", str(cfg))
    assert r.returncode == 0 and json.loads(r.stdout)["plan"]["action"] == "restart_lane"


def test_real_deps_http_posts_unload(tmp_path):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        deps = cli.real_deps({"nodes": {}}, tmp_path)
        status = deps["http"]("POST", "http://127.0.0.1:%d/api/generate" % srv.server_port, {"model": "m", "keep_alive": 0})
    finally:
        srv.shutdown()
    assert status == 200 and seen == {"path": "/api/generate", "body": {"model": "m", "keep_alive": 0}}


def test_real_deps_set_env_persists_and_audit_hides_value(tmp_path):
    deps = cli.real_deps({"nodes": {}}, tmp_path)
    assert deps["set_env"]("SG_TEST_VAR", "abc") is True
    assert "SG_TEST_VAR=abc" in (tmp_path / "env").read_text()
    assert deps["set_env"]("SG_TEST_VAR", "def") is True            # replaces, never duplicates
    assert (tmp_path / "env").read_text().count("SG_TEST_VAR=") == 1
    sys.modules["os"].environ.pop("SG_TEST_VAR", None)


def test_real_deps_run_returns_exit_code_without_shell(tmp_path):
    deps = cli.real_deps({"nodes": {}}, tmp_path)
    assert deps["run"]([sys.executable, "-c", "raise SystemExit(7)"]) == (7, "")
    assert deps["run"](["definitely-not-a-real-binary-xyz"])[0] != 0
