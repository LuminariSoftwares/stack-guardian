"""Every module's own --selftest passes when run from the package, and the CLI entry point works end to end."""
import json
import subprocess
import sys

import pytest

MODULES = ["finding", "parsers", "core", "state", "cli", "collect", "mcp_server", "multinode", "serve", "fixes"]


@pytest.mark.parametrize("mod", MODULES)
def test_module_selftest(mod):
    r = subprocess.run([sys.executable, "-m", "stack_guardian." + mod, "--selftest"], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert " 0 failed" in r.stdout


def test_doctor_end_to_end(tmp_path):
    checks = tmp_path / "checks.d"
    checks.mkdir()
    (checks / "10_up.py").write_text(
        "from stack_guardian import finding as F\n"
        "def up(s, c):\n"
        "    if not s['nodes']['atlas']['reachable']:\n"
        "        return F.make_finding('ollama.unreachable', 'missing', 'FAIL', 'atlas', 'Ollama does not answer')\n"
        "    return None\n"
        "CHECKS = [('ollama.up', up)]\n", encoding="utf-8")
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"schema": 1, "nodes": {"atlas": {"reachable": False}}}), encoding="utf-8")
    state = tmp_path / "state.json"
    args = [sys.executable, "-m", "stack_guardian", "doctor", "--checks-dir", str(checks), "--snapshot", str(snap), "--state", str(state), "--json"]
    first = subprocess.run(args, capture_output=True, text=True, timeout=60)
    second = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert first.returncode == 2 and second.returncode == 2, first.stderr + second.stderr
    assert json.loads(first.stdout)["findings"][0]["confidence"] == "LIKELY"
    assert json.loads(second.stdout)["findings"][0]["confidence"] == "VERIFIED"


def test_usage_exit_3():
    r = subprocess.run([sys.executable, "-m", "stack_guardian"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 3


def test_doctor_nodes_live_shape(tmp_path):
    """--nodes collects a snapshot itself; a dead endpoint becomes an ollama.unreachable FAIL through the shipped rules."""
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"ghost": {"ollama": ["http://127.0.0.1:9"]}}), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "stack_guardian", "doctor", "--nodes", str(nodes), "--state", str(tmp_path / "s.json"), "--json"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stderr
    codes = [f["code"] for f in json.loads(r.stdout)["findings"]]
    assert codes == ["ollama.unreachable"], codes


def test_doctor_bad_snapshot_json_is_a_crash_not_usage(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "stack_guardian", "doctor", "--snapshot", str(bad), "--state", str(tmp_path / "s.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 4 and "crash" in r.stderr


def test_doctor_needs_one_source():
    r = subprocess.run([sys.executable, "-m", "stack_guardian", "doctor"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 3


def test_mcp_stdio_end_to_end(tmp_path):
    """P43 S5: the server answers initialize, lists exactly three read-only tools, and a stack_health call
    returns findings from the real doctor (a node file pointing at a closed port -> ollama.unreachable)."""
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"lab": {"ollama": ["http://127.0.0.1:9"]}}), encoding="utf-8")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "stack_health", "arguments": {}}}]
    r = subprocess.run([sys.executable, "-m", "stack_guardian.mcp_server", "--nodes", str(nodes), "--state", str(tmp_path / "st.json")],
                       input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, timeout=120)
    out = {o["id"]: o for o in (json.loads(x) for x in r.stdout.splitlines() if x.strip())}
    assert sorted(out) == [1, 2, 3], r.stdout + r.stderr
    assert [t["name"] for t in out[2]["result"]["tools"]] == ["stack_health", "stack_snapshot", "explain"]
    body = json.loads(out[3]["result"]["content"][0]["text"])
    assert any(f["code"] == "ollama.unreachable" and f["node"] == "lab" for f in body["findings"]), body


def test_dsh_plugin_selftest():
    """P43 S6: the DSH plugin (dsh/index.js) passes its own node selftest."""
    import os
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([node, os.path.join(here, "dsh", "index.js"), "--selftest"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert " 0 failed" in r.stdout
