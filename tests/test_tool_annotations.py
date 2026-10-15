"""Every MCP tool declares the spec's tool annotations and a title; the values are checked over the real stdio protocol."""
import json
import subprocess
import sys

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _tools_over_stdio(tmp_path):
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"gpu-box": {"ollama": ["http://127.0.0.1:9"]}}), encoding="utf-8")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    r = subprocess.run([sys.executable, "-m", "stack_guardian.mcp_server", "--nodes", str(nodes)],
                       input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, timeout=60)
    replies = {o["id"]: o for o in (json.loads(x) for x in r.stdout.splitlines() if x.strip())}
    return replies[2]["result"]["tools"]


def test_every_tool_declares_all_hints_and_a_title(tmp_path):
    tools = _tools_over_stdio(tmp_path)
    assert tools, "no tools listed"
    for tool in tools:
        ann = tool.get("annotations")
        assert isinstance(ann, dict), tool["name"]
        for hint in HINTS:
            assert isinstance(ann.get(hint), bool), (tool["name"], hint)
        assert isinstance(tool.get("title"), str) and tool["title"].strip(), tool["name"]
        assert ann.get("title") == tool["title"], tool["name"]


def test_hint_values_are_truthful(tmp_path):
    by_name = {t["name"]: t["annotations"] for t in _tools_over_stdio(tmp_path)}
    # nothing this server exposes writes or deletes: every tool only reports
    for name, ann in by_name.items():
        assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False, name
    # stack_health and stack_snapshot probe the nodes over the network; explain is a local table lookup
    assert by_name["stack_health"]["openWorldHint"] is True
    assert by_name["stack_snapshot"]["openWorldHint"] is True
    assert by_name["explain"]["openWorldHint"] is False
