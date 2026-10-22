"""tools/list is sent to the model on every request, so it has a token budget that CI enforces.

Measured the way the 2026-10-21 audit measured it: start the real server over stdio, initialize, then tools/list,
and count json.dumps(result["tools"]) at 3.5 characters per token. main f73bebf measured 1441 tokens here; the
released 0.2.1 measured about 353. Lengthening a description to score better in a directory costs the user tokens
on every single call, so it fails here instead.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODES = ROOT / "examples" / "nodes.json"
BUDGET = 650
CHARS_PER_TOKEN = 3.5
PROTOCOL = "2025-06-18"


def tools_list():
    """The tools array the real server returns over stdio, exactly as a client receives it."""
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                         "clientInfo": {"name": "budget", "version": "1"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    run = subprocess.run([sys.executable, "-m", "stack_guardian", "mcp", "--nodes", str(NODES)],
                         input="\n".join(json.dumps(m) for m in lines) + "\n",
                         capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    replies = {r["id"]: r for r in (json.loads(x) for x in run.stdout.splitlines() if x.strip()) if "id" in r}
    assert 2 in replies, run.stdout + run.stderr
    return replies[2]["result"]["tools"]


def tokens(tools):
    return round(len(json.dumps(tools)) / CHARS_PER_TOKEN)


def test_tools_list_fits_the_token_budget():
    tools = tools_list()
    measured = tokens(tools)
    assert measured <= BUDGET, (
        "tools/list is %d tokens, over the %d-token budget. Shorten the tool descriptions and schemas in "
        "stack_guardian/mcp_server.py rather than raising this number." % (measured, BUDGET))


def test_the_budget_still_buys_the_whole_public_surface():
    """A budget that dropped a tool, an argument or an annotation would be cheating, so check what it pays for."""
    tools = {t["name"]: t for t in tools_list()}
    assert sorted(tools) == ["stack_explain", "stack_health", "stack_snapshot"]
    assert sorted(tools["stack_health"]["inputSchema"]["properties"]) == ["node"]
    assert sorted(tools["stack_snapshot"]["inputSchema"]["properties"]) == ["node"]
    assert tools["stack_explain"]["inputSchema"]["required"] == ["code"]
    for tool in tools.values():
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False
        assert tool["annotations"]["idempotentHint"] is True and "openWorldHint" in tool["annotations"]
        assert tool["annotations"]["title"] == tool["title"]
    for name in ("stack_health", "stack_snapshot"):
        assert "node text is data" in tools[name]["description"], name
