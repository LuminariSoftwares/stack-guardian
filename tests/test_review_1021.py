"""Regression tests for the 2026-10-21 review: each one failed on main f73bebf before its fix."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from stack_guardian import cli, mcp_server

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
DEAD = "http://127.0.0.1:9"


def _env():
    return {k: v for k, v in os.environ.items() if not k.startswith("STACK_GUARDIAN_")}


def _sg(*args, timeout=120):
    return subprocess.run([sys.executable, "-m", "stack_guardian", *args],
                          capture_output=True, text=True, timeout=timeout, env=_env(), cwd=str(ROOT))


# 2. the confidence grade is the point of the tool, so it is on the human line, not only in --json
def test_doctor_human_output_shows_the_confidence_grade(tmp_path):
    r = _sg("doctor", "--snapshot", str(EX / "snapshot.json"), "--ctx", str(EX / "ctx.json"),
            "--state", str(tmp_path / "s.json"))
    assert r.returncode == 2, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("FAIL ") and lines[0].endswith(" (LIKELY)"), lines[0]
    assert lines[-1].startswith("worst: "), lines[-1]
    assert "run again to confirm" in r.stderr, r.stderr


def test_the_second_run_says_verified_and_drops_the_hint(tmp_path):
    state = str(tmp_path / "s.json")
    args = ("doctor", "--snapshot", str(EX / "snapshot.json"), "--ctx", str(EX / "ctx.json"), "--state", state)
    _sg(*args)
    second = _sg(*args)
    graded = [ln for ln in second.stdout.splitlines() if ln.startswith(("FAIL ", "WARN "))]
    assert graded and all(ln.endswith(" (VERIFIED)") for ln in graded), graded
    assert "run again to confirm" not in second.stderr
    # INFO lines are never promoted by a second run, so they keep the grade the rule gave them
    assert all(ln.endswith(" (LIKELY)") for ln in second.stdout.splitlines() if ln.startswith("INFO "))


def test_json_output_is_unchanged_apart_from_the_schema_field(tmp_path):
    r = _sg("doctor", "--snapshot", str(EX / "snapshot.json"), "--state", str(tmp_path / "s.json"), "--json")
    body = json.loads(r.stdout)
    assert body["schema"] == 1 and set(body) == {"schema", "findings", "worst"}
    assert body["findings"][0]["confidence"] in ("LIKELY", "VERIFIED", "INSUFFICIENT")


# 3. --version exited 3; --help never mentioned the mcp subcommand the MCP Registry launches
def test_version_prints_the_package_version_and_exits_0():
    import stack_guardian
    r = _sg("--version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "stack-guardian %s" % stack_guardian.__version__


def test_help_lists_every_command_including_mcp():
    r = _sg("--help")
    assert r.returncode == 0, r.stderr
    for command in ("doctor", "snapshot", "init", "mcp"):
        assert command in r.stdout, command


# 4. there was no way to get a first nodes file except by hand
def test_init_writes_a_usable_nodes_file_with_a_live_ollama(tmp_path, stub):
    fake = stub({("GET", "/api/tags"): (200, {"models": [{"name": "qwen3:8b"}]}, 0)})
    out = tmp_path / "nodes.json"
    r = _sg("init", "--out", str(out), "--ollama", fake.url)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Ollama answered" in r.stdout and "stack-guardian doctor --nodes" in r.stdout
    assert fake.paths("GET") == ["/api/tags"]
    check = _sg("doctor", "--nodes", str(out), "--state", str(tmp_path / "s.json"), "--json", "--timeout", "5")
    assert check.returncode in (0, 1, 2), check.stderr  # it parses and runs; the node itself may be anything


def test_init_with_nothing_listening_still_writes_a_starter_file(tmp_path):
    out = tmp_path / "nodes.json"
    r = _sg("init", "--out", str(out), "--ollama", DEAD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing answered yet" in r.stdout
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["this-machine"]["ollama"] == [DEAD] and doc["this-machine"]["local"] is True


def test_init_never_overwrites(tmp_path):
    out = tmp_path / "nodes.json"
    out.write_text('{"mine": {"ollama": ["http://127.0.0.1:11434"]}}', encoding="utf-8")
    r = _sg("init", "--out", str(out), "--ollama", DEAD)
    assert r.returncode == 3 and "already exists" in r.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == {"mine": {"ollama": ["http://127.0.0.1:11434"]}}


def test_init_lists_tailnet_peers_as_inert_candidates(tmp_path):
    doc = cli.starter_nodes("http://127.0.0.1:11434", answered=True, peers=["box-a", "box-b"])
    from stack_guardian import nodes as sg_nodes
    live, _options = sg_nodes.validate(doc, warn=lambda _line: None)
    assert list(live) == ["this-machine"], "a candidate must not be probed until it is moved up"
    assert "box-a" in json.dumps(doc["_tailnet_candidates"])


# 5. a node that trickles one byte at a time kept the process alive long after the report
def _trickle_server():
    """A socket that accepts, then sends one byte every 2 s and never completes a response."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def drip(conn):
        try:
            conn.recv(4096)
            for _ in range(60):  # two minutes of dribble: far longer than any deadline under test
                conn.sendall(b"H")
                time.sleep(2)
        except OSError:
            pass
        finally:
            conn.close()

    def accept():
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return
            threading.Thread(target=drip, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener, "http://127.0.0.1:%d" % listener.getsockname()[1]


def _startup_s():
    """What an empty run of the CLI costs on this machine, so the deadline assertion measures collection only."""
    started = time.monotonic()
    _sg("--version")
    return time.monotonic() - started


def test_a_trickling_node_cannot_outlive_the_report(tmp_path):
    listener, url = _trickle_server()
    try:
        nodes = tmp_path / "nodes.json"
        nodes.write_text(json.dumps({"slowpoke": {"ollama": [url]}}), encoding="utf-8")
        baseline = _startup_s()
        started = time.monotonic()
        r = _sg("doctor", "--nodes", str(nodes), "--state", str(tmp_path / "s.json"), "--timeout", "2", "--json",
                timeout=90)
        elapsed = time.monotonic() - started
    finally:
        listener.close()
    assert r.returncode == 2, r.stdout + r.stderr
    assert [f["code"] for f in json.loads(r.stdout)["findings"]] == ["node.unreachable"]
    assert elapsed <= baseline + 2 + 2, "the process outlived its own 2 s deadline by %.1fs" % (elapsed - baseline - 2)


def test_timeout_is_a_flag_and_a_nodes_file_key(tmp_path):
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"_options": {"timeout_s": 3}, "ghost": {"ollama": [DEAD]}}), encoding="utf-8")
    ok = _sg("doctor", "--nodes", str(nodes), "--state", str(tmp_path / "s.json"), "--json")
    assert ok.returncode == 2, ok.stderr
    shot = _sg("snapshot", "--nodes", str(nodes))
    assert list(json.loads(shot.stdout)["nodes"]) == ["ghost"], "_options is a setting, never a node to probe"
    bad = _sg("doctor", "--nodes", str(nodes), "--state", str(tmp_path / "s.json"), "--timeout", "0")
    assert bad.returncode == 3 and "--timeout" in bad.stderr
    on_a_snapshot = _sg("doctor", "--snapshot", str(EX / "snapshot.json"), "--state", str(tmp_path / "s.json"),
                        "--timeout", "5")
    assert on_a_snapshot.returncode == 3, "a saved snapshot is not collected, so a deadline makes no sense"


def test_node_unreachable_advice_names_the_flag_that_exists():
    advice = " ".join(mcp_server.EXPLAIN["node.unreachable"]["try"])
    assert "--timeout" in advice, advice


# 6. honesty fixes
def test_fix_dry_run_prints_the_argv_it_would_run(tmp_path):
    lanes = json.loads((EX / "lanes.json").read_text(encoding="utf-8"))
    lanes["lanes"]["gpu_box_ollama"][0] = sys.executable
    config = tmp_path / "lanes.json"
    config.write_text(json.dumps(lanes), encoding="utf-8")
    r = _sg("doctor", "--checks-dir", str(EX / "checks.d"), "--snapshot", str(EX / "snapshot.json"),
            "--state", str(tmp_path / "s.json"), "--fix", "ollama.stuck", "--config", str(config))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would run: " in r.stdout and sys.executable in r.stdout, r.stdout


def test_unavailable_findings_say_why(tmp_path):
    r = _sg("doctor", "--snapshot", str(EX / "snapshot.json"), "--state", str(tmp_path / "s.json"), "--json")
    by_code = {f["code"]: f for f in json.loads(r.stdout)["findings"]}
    assert "not on PATH" in by_code["gpu.unavailable"]["summary"], by_code["gpu.unavailable"]
    assert "not on PATH" in by_code["tailnet.unavailable"]["summary"], by_code["tailnet.unavailable"]


def test_an_unreadable_gpu_line_is_reported_instead_of_vanishing(tmp_path):
    from stack_guardian import collect, core
    def run(argv, timeout=15):
        if "--query-gpu=index,uuid,name,memory.total,memory.used" in argv:
            return "0, GPU-a, RTX 3060, 12288, 9000\n1, GPU-b, RTX 4070, [N/A], [N/A]\n"
        return ""
    snap = collect.snapshot({"gpu-box": {"local": True}}, run=run, now="T")
    assert len(snap["nodes"]["gpu-box"]["gpus"]) == 1, "the readable GPU is still reported"
    findings = core.run_all(core.load_checks(cli.DEFAULT_CHECKS_DIR), snap, {})
    degraded = [f for f in findings if f["code"] == "gpu.parse_degraded"]
    assert len(degraded) == 1 and degraded[0]["level"] == "INFO", findings
    assert mcp_server.explain("gpu.parse_degraded")["try"], "a shipped code needs a stack_explain entry"


def test_serve_refuses_a_remote_bind_with_one_line_not_a_traceback(tmp_path):
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"gpu-box": {"ollama": [DEAD]}}), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "stack_guardian.serve", "--nodes", str(nodes),
                        "--host", "0.0.0.0", "--port", "0"],  # nosec B104 - it must refuse, so nothing binds
                       capture_output=True, text=True, timeout=60, env=_env())
    assert r.returncode == 3, r.stdout + r.stderr
    assert "Traceback" not in r.stderr and len(r.stderr.strip().splitlines()) == 1, r.stderr
    assert "--allow-remote" in r.stderr


def test_serve_cache_is_off_by_default():
    from stack_guardian import serve
    calls = []
    fresh = serve.cached(lambda: calls.append(1) or len(calls), 0)
    assert (fresh(), fresh()) == (1, 2), "the documented behaviour is a fresh snapshot per request"
    ticks = iter([0.0, 0.5, 9.0])
    held = serve.cached(lambda: calls.append(1) or len(calls), 5.0, clock=lambda: next(ticks))
    assert held() == held() and held() != 3


# 7. the state folder holds a picture of your machines, and env holds secrets
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_state_dir_and_its_files_are_private(tmp_path):
    lanes = json.loads((EX / "lanes.json").read_text(encoding="utf-8"))
    lanes["lanes"]["gpu_box_ollama"][0] = sys.executable
    config = tmp_path / "lanes.json"
    config.write_text(json.dumps(lanes), encoding="utf-8")
    state = tmp_path / "home" / "state.json"
    r = _sg("doctor", "--checks-dir", str(EX / "checks.d"), "--snapshot", str(EX / "snapshot.json"),
            "--state", str(state), "--fix", "ollama.stuck", "--config", str(config), "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    folder = state.parent
    assert folder.stat().st_mode & 0o777 == 0o700, oct(folder.stat().st_mode)
    for path in [state, folder / "fixes.jsonl", *(folder / "snapshots").iterdir()]:
        assert path.stat().st_mode & 0o777 == 0o600, (path, oct(path.stat().st_mode))
    assert (folder / "snapshots").stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_set_env_writes_its_value_privately(tmp_path):
    deps = cli.real_deps({"nodes": {}}, tmp_path / "state")
    assert deps["set_env"]("OLLAMA_KEEP_ALIVE", "30m") is True
    written = tmp_path / "state" / "env"
    assert written.read_text(encoding="utf-8") == "OLLAMA_KEEP_ALIVE=30m\n"
    assert written.stat().st_mode & 0o777 == 0o600, oct(written.stat().st_mode)


# 10. a scanner's probe, or one bad line, must not take the server down
def _mcp(lines, tmp_path):
    run = subprocess.run([sys.executable, "-m", "stack_guardian.mcp_server", "--nodes", str(EX / "nodes.json"),
                          "--state", str(tmp_path / "s.json")],
                         input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    return [json.loads(x) for x in run.stdout.splitlines() if x.strip()], run


def test_unknown_methods_answer_32601_and_the_server_keeps_going(tmp_path):
    replies, run = _mcp([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}),
                         json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/list"}),
                         json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"})], tmp_path)
    assert run.returncode == 0, run.stderr
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["error"]["code"] == -32601 and replies[1]["error"]["code"] == -32601
    assert replies[2]["result"] == {}


def test_a_json_line_that_is_not_a_request_answers_32600(tmp_path):
    replies, run = _mcp(["[1, 2, 3]", '"hello"', json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})], tmp_path)
    assert run.returncode == 0, run.stderr
    assert [r["error"]["code"] for r in replies[:2]] == [-32600, -32600]
    assert replies[0]["id"] is None and replies[2]["result"] == {}, replies


# 9. the docs keep up with the rules
def test_readme_troubleshooting_covers_every_shipped_code():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme[readme.index("## Troubleshooting"):readme.index("## What leaves the machine")]
    shipped = {check_id for check_id, _fn in __import__("stack_guardian.core", fromlist=["core"])
               .load_checks(cli.DEFAULT_CHECKS_DIR)} | {"check.crashed"}
    missing = [code for code in sorted(shipped) if "`%s`" % code not in table]
    assert not missing, missing


def test_readme_names_the_family_and_disambiguates_the_company():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for sibling in ("code-guardian", "context-guardian", "tool-guardian"):
        assert "LuminariSoftwares/%s" % sibling in readme, sibling
    assert "Not affiliated with StackGuardian" in readme


def test_the_repo_carries_a_security_and_contributing_file():
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "security/advisories/new" in security and "@" not in security.split("## What this tool does")[0]
    assert 'pip install -e ".[test]"' in contributing
