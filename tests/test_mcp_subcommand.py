"""`stack-guardian mcp` starts the same MCP server as `stack-guardian-mcp` (the MCP Registry launches `uvx stack-guardian mcp`)."""
import json
import subprocess
import sys
from pathlib import Path

import stack_guardian

NODES = Path(__file__).resolve().parent.parent / "examples" / "nodes.json"


def test_mcp_subcommand_speaks_mcp():
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    r = subprocess.run([sys.executable, "-m", "stack_guardian", "mcp", "--nodes", str(NODES)],
                       input=req + "\n", capture_output=True, text=True, timeout=60)
    first = json.loads(r.stdout.splitlines()[0])
    assert first["result"]["serverInfo"] == {"name": "stack-guardian", "version": stack_guardian.__version__}


def test_mcp_subcommand_usage_without_nodes():
    r = subprocess.run([sys.executable, "-m", "stack_guardian", "mcp"], capture_output=True, text=True, timeout=60,
                       env={"PATH": ""} if sys.platform != "win32" else None)
    assert r.returncode == 3 and "--nodes" in r.stderr


def test_versions_agree():
    import re
    from stack_guardian import mcp_server
    py = re.search(r'^version = "([^"]+)"', (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert mcp_server.VERSION == stack_guardian.__version__ == py
