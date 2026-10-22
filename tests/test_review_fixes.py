"""Regression tests for the 2026-10-10 review: each test failed on main 999ea1e before its fix."""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from stack_guardian import cli, collect, core, mcp_server, multinode, parsers, serve, state

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
DEAD = "http://127.0.0.1:9"  # nothing listens on the discard port: connection refused at once


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("STACK_GUARDIAN_")}
    env.update(extra)
    return env


def _sg(*args, env=None):
    return subprocess.run([sys.executable, "-m", "stack_guardian", *args], capture_output=True, text=True,
                          timeout=60, env=env or _env(), cwd=str(ROOT))


def _codes(findings):
    return [(f["code"], f["node"], f["level"]) for f in findings]


def _shipped(snapshot, ctx=None):
    return core.run_all(core.load_checks(cli.DEFAULT_CHECKS_DIR), snapshot, ctx or {})


# 1. doctor falls back to $STACK_GUARDIAN_NODES, like the MCP server, so the DSH plugin's default args work
def test_doctor_uses_nodes_env_when_no_source_given(tmp_path):
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"gpu-box": {"ollama": [DEAD]}}), encoding="utf-8")
    r = _sg("doctor", "--json", "--quiet", env=_env(STACK_GUARDIAN_NODES=str(nodes),
                                                     STACK_GUARDIAN_STATE=str(tmp_path / "s.json")))
    assert r.returncode == 2, r.stdout + r.stderr
    assert ("ollama.unreachable", "gpu-box", "FAIL") in _codes(json.loads(r.stdout)["findings"])


def test_doctor_without_source_or_env_is_still_usage():
    r = _sg("doctor", "--json", "--quiet")
    assert r.returncode == 3 and "STACK_GUARDIAN_NODES" in r.stderr


# 2. a node whose collector timed out or raised is a FAIL, not a silent OK
def test_down_node_is_a_fail(tmp_path):
    def collect_one(name, cfg):
        raise ConnectionError("refused")
    snap = multinode.pull({"gpu-box": {}}, collect_one, timeout_s=2, now="T")
    findings, code = cli.doctor(cli.DEFAULT_CHECKS_DIR, snap, str(tmp_path / "s.json"))
    assert code == 2
    assert ("node.unreachable", "gpu-box", "FAIL") in _codes(findings)


# 3. stack_health grades against existing state without recording, and caches for a short TTL
def _dead_nodes(tmp_path):
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"gpu-box": {"ollama": [DEAD]}}), encoding="utf-8")
    return str(nodes)


def _health(tools):
    return [f for f in tools["stack_health"]({})["findings"] if f["code"] == "ollama.unreachable"][0]


def test_mcp_health_does_not_record_samples(tmp_path):
    st = tmp_path / "state.json"
    tools = mcp_server.default_tools(_dead_nodes(tmp_path), state_path=str(st), ttl_s=0)
    first, second = _health(tools), _health(tools)
    assert first["confidence"] == "LIKELY" and second["confidence"] == "LIKELY"
    assert not st.exists()


def test_mcp_health_without_state_flag_writes_nothing(tmp_path, monkeypatch):
    home_state = tmp_path / "home" / "state.json"
    monkeypatch.setenv("STACK_GUARDIAN_STATE", str(home_state))
    tools = mcp_server.default_tools(_dead_nodes(tmp_path), ttl_s=0)
    _health(tools)
    assert not home_state.exists()


def test_mcp_health_grades_against_existing_state(tmp_path):
    st = tmp_path / "state.json"
    s = state.record_run(state.fresh(), "gpu-box", [{"code": "ollama.unreachable", "level": "FAIL", "node": "gpu-box"}], "t0")
    state.save(s, st)
    before = st.read_text()
    tools = mcp_server.default_tools(_dead_nodes(tmp_path), state_path=str(st), ttl_s=0)
    assert _health(tools)["confidence"] == "VERIFIED"
    assert st.read_text() == before


def test_mcp_health_ttl_cache(tmp_path, monkeypatch):
    calls = []
    now = [100.0]
    monkeypatch.setattr(collect, "snapshot", lambda nodes, **kw: calls.append(1) or {"schema": 1, "taken_at": "T", "nodes": {}})
    tools = mcp_server.default_tools(_dead_nodes(tmp_path), state_path=str(tmp_path / "s.json"), clock=lambda: now[0])
    tools["stack_health"]({})
    tools["stack_health"]({})
    tools["stack_snapshot"]({})
    assert len(calls) == 1
    now[0] += 11
    tools["stack_health"]({})
    assert len(calls) == 2


# 4. tools/call with non-dict params is a JSON-RPC -32602, not a dead server
def test_tools_call_non_dict_params_is_invalid_params():
    tools = mcp_server.make_tools(lambda: [], lambda: {"nodes": {}})
    for params in (["stack_health"], "x", 7, {"name": "stack_health", "arguments": ["x"]}):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, tools)
        assert r["error"]["code"] == -32602, params
    out = []
    lines = ['{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1]}', '{"jsonrpc":"2.0","id":2,"method":"ping"}']
    assert mcp_server.serve(lines, out.append, tools) == 2
    assert json.loads(out[1]) == {"jsonrpc": "2.0", "id": 2, "result": {}}


# 5. concurrent state saves never collide on a shared temp file
def test_state_save_concurrent(tmp_path):
    path = tmp_path / "state.json"
    errors = []

    def worker(i):
        for j in range(30):
            try:
                state.save({"schema": 1, "nodes": {"n%d" % i: {"runs": [{"at": str(j), "codes": {}}]}}}, path)
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert state.load(path)["schema"] == 1
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# 6. one finding per failing node: each gets its own streak, node= filter and code@node
def test_one_finding_per_failing_node(tmp_path):
    snap = {"schema": 1, "nodes": {
        "gpu-box": {"reachable": False, "ollama": [{"url": "http://a:11434", "error": "refused", "running": []}]},
        "gpu-box-2": {"reachable": False, "ollama": [{"url": "http://b:11434", "error": "refused", "running": []}]}}}
    st = str(tmp_path / "s.json")
    cli.doctor(cli.DEFAULT_CHECKS_DIR, snap, st)
    findings, _ = cli.doctor(cli.DEFAULT_CHECKS_DIR, snap, st)
    hits = [f for f in findings if f["code"] == "ollama.unreachable"]
    assert sorted(f["node"] for f in hits) == ["gpu-box", "gpu-box-2"]
    assert all(f["confidence"] == "VERIFIED" for f in hits)
    assert [f["node"] for f in mcp_server.filter_node(hits, "gpu-box-2")] == ["gpu-box-2"]
    assert cli.pick_fix_finding(findings, "ollama.unreachable@gpu-box-2")[1] == "finding ollama.unreachable@gpu-box-2 carries no fix"


# 7. a missing tailscale/nvidia-smi is INFO, never a false FAIL; all-[N/A] process memory is not "unattributed"
def _none(argv, timeout=15):
    return None


def test_missing_tailscale_is_info_not_peer_offline():
    tail = collect.collect_tailnet("gpu-box-2", run=_none)
    assert tail["peer"] is None and tail["error"]
    down = parsers.parse_tailscale(json.dumps({"BackendState": "Stopped", "Peer": None}), "gpu-box-2")
    assert down["peer"] is None and down["error"]
    findings = _shipped({"nodes": {"gpu-box-2": {"reachable": True, "ollama": [], "tailnet": tail}}})
    assert _codes(findings) == [("tailnet.unavailable", "gpu-box-2", "INFO")]


def test_missing_nvidia_smi_is_info():
    snap = collect.snapshot({"gpu-box": {"local": True}}, run=_none, now="T")
    node = snap["nodes"]["gpu-box"]
    assert node["gpus"] == [] and node["gpu_error"]
    assert ("gpu.unavailable", "gpu-box", "INFO") in _codes(_shipped(snap))


def test_all_na_process_memory_skips_vram_unattributed():
    gpus = parsers.parse_nvsmi("0, GPU-a, RTX 3060, 12288, 9000",
                               "GPU-a, 11, C:\\Windows\\explorer.exe, [N/A]\nGPU-a, 12, C:\\x\\ollama.exe, [N/A]\n")
    assert gpus[0]["unattributed_mib"] is None
    assert _shipped({"nodes": {"gpu-box": {"reachable": True, "ollama": [], "gpus": gpus}}}) == []


# 8. a lane timeout is 124 (not 127) and stderr reaches the audit
def test_lane_timeout_is_124_and_keeps_stderr(tmp_path):
    deps = cli.real_deps({"nodes": {}}, tmp_path, run_timeout=1)
    rc, err = deps["run"]([sys.executable, "-c", "import sys,time; sys.stderr.write('slow-lane'); sys.stderr.flush(); time.sleep(10)"])
    assert rc == 124
    rc, err = deps["run"]([sys.executable, "-c", "import sys; sys.stderr.write('x' * 500 + 'boom-tail'); sys.exit(3)"])
    assert rc == 3 and err.endswith("boom-tail") and len(err) <= 200
    rc, _ = deps["run"](["definitely-not-a-real-binary-xyz"])
    assert rc == 127


def test_lane_stderr_in_audit_detail(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"lanes": {"cpu": [sys.executable, "-c", "import sys; sys.stderr.write('lane-broke'); sys.exit(3)"]}}),
                   encoding="utf-8")
    checks = tmp_path / "checks.d"
    checks.mkdir()
    (checks / "10_lane.py").write_text(
        "from stack_guardian import finding as F\n"
        "def down(s, c):\n"
        "    return F.make_finding('lane.stuck', 'missing', 'FAIL', 'gpu-box', 'stuck', fix={'action': 'restart_lane', 'args': {'lane': 'cpu'}})\n"
        "CHECKS = [('lane.stuck', down)]\n", encoding="utf-8")
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"schema": 1, "nodes": {"gpu-box": {}}}), encoding="utf-8")
    r = _sg("doctor", "--checks-dir", str(checks), "--snapshot", str(snap), "--state", str(tmp_path / "s.json"),
            "--json", "--fix", "lane.stuck", "--apply", "--config", str(cfg))
    assert r.returncode == 1, r.stdout + r.stderr
    audit = json.loads((tmp_path / "fixes.jsonl").read_text().splitlines()[0])
    assert audit["detail"].startswith("rc 3") and "lane-broke" in audit["detail"]


# 9. small items
def test_description_no_longer_mentions_harness_logs():
    import stack_guardian
    assert "harness logs" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "harness logs" not in (stack_guardian.__doc__ or "")


def test_serve_405_sends_allow_get():
    srv = serve.make_server(lambda: {"nodes": {}})
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/snapshot" % srv.server_port, data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("POST was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405 and exc.headers.get("Allow") == "GET"
    finally:
        srv.shutdown()
        srv.server_close()


def test_state_load_oserror_discards_with_one_line(tmp_path, capsys):
    assert state.load(tmp_path) == state.fresh()  # a directory: IsADirectoryError / PermissionError
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert state.load(bad) == state.fresh()
    assert state.load(tmp_path / "absent.json") == state.fresh()  # a missing file is not a discard
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2 and all(line.startswith("stack-guardian: state") for line in err), err


# 10. the demo snapshot exercises every shipped rule; the docs lead with the offline command
def test_example_snapshot_fires_every_shipped_rule(tmp_path):
    snap = json.loads((EX / "snapshot.json").read_text(encoding="utf-8"))
    ctx = json.loads((EX / "ctx.json").read_text(encoding="utf-8"))
    shipped = {check_id for check_id, _ in core.load_checks(cli.DEFAULT_CHECKS_DIR)}
    fired = {f["code"] for f in _shipped(snap, ctx)}
    assert shipped <= fired, sorted(shipped - fired)


def test_readme_offline_first_agents_section_and_serve_warning():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    first_run = readme.index("stack-guardian doctor ")
    assert readme[first_run:].startswith("stack-guardian doctor --snapshot"), "the first command shown is the offline one"
    assert first_run < readme.index("stack-guardian doctor --nodes")
    agents = readme.index("## For coding agents")
    assert agents < readme.index("## What it does")
    assert "stack-guardian-mcp" in readme[agents:] and "dsh/" in readme[agents:]
    assert "no auth: bind to localhost or a tailnet IP only" in readme


def test_check_file_with_syntax_error_is_crashed_warn(tmp_path):
    checks = tmp_path / "checks.d"
    checks.mkdir()
    (checks / "10_broken.py").write_text("def broken(:\n", encoding="utf-8")
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"schema": 1, "nodes": {}}), encoding="utf-8")
    r = _sg("doctor", "--checks-dir", str(checks), "--snapshot", str(snap), "--state", str(tmp_path / "s.json"), "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    assert _codes(json.loads(r.stdout)["findings"]) == [("check.crashed", "", "WARN")]


def test_every_shipped_rule_has_an_explanation():
    shipped = {check_id for check_id, _ in core.load_checks(cli.DEFAULT_CHECKS_DIR)}
    assert shipped <= set(mcp_server.EXPLAIN), sorted(shipped - set(mcp_server.EXPLAIN))
