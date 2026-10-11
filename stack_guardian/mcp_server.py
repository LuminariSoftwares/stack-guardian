"""Read-only MCP stdio server for the stack_guardian package.

Speaks JSON-RPC 2.0, one JSON object per line on stdin/stdout, and exposes three
read-only tools: stack_health, stack_snapshot and explain. Everything above
default_tools() is pure and package-free; default_tools() is the only place that
imports the rest of stack_guardian.
"""

import json
import os
import sys
import time

PROTOCOL = "2024-11-05"
SERVER_NAME = "stack-guardian"
VERSION = "0.1.0.dev0"
_NODE = {"type": "object", "properties": {"node": {"type": "string", "description": "one node name; omit for all"}}}
TOOL_SPECS = [
    {"name": "stack_health", "description": "Graded health findings (FAIL/WARN/INFO/OK) for the AI stack's nodes, with what to try.",
     "inputSchema": _NODE},
    {"name": "stack_snapshot", "description": "The raw snapshot the checks read: Ollama endpoints, loaded models, GPUs, tailnet.",
     "inputSchema": _NODE},
    {"name": "explain", "description": "What a finding code means, what to try, and what evidence would settle it.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
]
EXPLAIN = {
    "node.unreachable": {"summary": "The collector got nothing from the node: it timed out or raised.",
                         "try": ["check the machine is on and reachable", "raise the collection timeout if it is just slow"],
                         "evidence_needed": ["the node's error in stack_snapshot"]},
    "ollama.unreachable": {"summary": "No Ollama endpoint on the node answered.",
                           "try": ["check the Ollama service is running on that node", "check the URL and port in the nodes file"],
                           "evidence_needed": ["the endpoint's /api/version from the node itself"]},
    "model.not_loaded": {"summary": "A model the stack expects to be resident is not loaded.",
                         "try": ["load it with a short generate", "check whether another job evicted it"],
                         "evidence_needed": ["/api/ps on that endpoint"]},
    "vram.unattributed": {"summary": "GPU memory is in use by something that is not a known model process.",
                          "try": ["list GPU processes with nvidia-smi", "close the program holding the memory"],
                          "evidence_needed": ["nvidia-smi --query-compute-apps output"]},
    "gpu.unavailable": {"summary": "nvidia-smi is missing or failed, so the GPUs could not be read.",
                        "try": ["run nvidia-smi on that node", "install or repair the NVIDIA driver"],
                        "evidence_needed": ["nvidia-smi output on that node"]},
    "tailnet.unavailable": {"summary": "tailscale is missing or tailscaled is down, so peer state is unknown (not offline).",
                            "try": ["run tailscale status", "start tailscaled"],
                            "evidence_needed": ["tailscale status --json on this machine"]},
    "tailnet.peer_offline": {"summary": "The tailnet reports the peer offline.",
                             "try": ["check the machine is on and tailscale is up", "tailscale ping the peer"],
                             "evidence_needed": ["tailscale status --json on both ends"]},
    "tailnet.relayed": {"summary": "The peer is reachable only through a DERP relay, not directly.",
                        "try": ["check firewall / UDP 41641", "tailscale netcheck on both ends"],
                        "evidence_needed": ["tailscale ping output showing via DERP or direct"]},
}


def explain(code):
    """Return the explanation for a finding code, as fresh (copied) containers."""
    known = EXPLAIN.get(code)
    if known is None:
        return {"summary": "unknown code %r" % code,
                "try": ["run stack_health to list the current codes"],
                "evidence_needed": []}
    return {"summary": known["summary"],
            "try": list(known["try"]),
            "evidence_needed": list(known["evidence_needed"])}


def filter_node(findings, node):
    """Keep the findings for one node, or all of them when node is falsy."""
    if not node:
        return list(findings)
    return [f for f in findings if f.get("node") == node]


def _text_result(text, is_error):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(msg, tools):
    """Answer one JSON-RPC message; return None when there is nothing to say."""
    if not isinstance(msg, dict) or "method" not in msg:
        if isinstance(msg, dict) and "id" in msg:
            return {"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32600, "message": "invalid request"}}
        return None
    if "id" not in msg:
        return None

    method = msg["method"]
    rid = msg["id"]
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER_NAME, "version": VERSION}}
    elif method == "tools/list":
        result = {"tools": TOOL_SPECS}
    elif method == "ping":
        result = {}
    elif method == "tools/call":
        params = msg.get("params") or {}
        args = (params.get("arguments") or {}) if isinstance(params, dict) else None
        if not isinstance(args, dict):
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": "invalid params: params and arguments must be objects"}}
        name = params.get("name")
        if name not in tools:
            result = _text_result("unknown tool: %s" % name, True)
        else:
            try:
                out = tools[name](args)
            except Exception as exc:  # noqa: BLE001 -- reported to the caller as a tool error
                result = _text_result("%s: %s" % (type(exc).__name__, exc), True)
            else:
                result = _text_result(json.dumps(out, indent=1, default=str), False)
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(lines, write, tools):
    """Read lines, write one JSON reply per answerable line; return the reply count."""
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg, tools)
        if reply is not None:
            write(json.dumps(reply))
            count += 1
    return count


LEVELS = ("FAIL", "WARN", "INFO", "OK")


def health_body(findings, node):
    """The stack_health reply: counts per level and a status, so a clean run is never an empty-looking result."""
    counts = {lvl: 0 for lvl in LEVELS}
    for f in findings:
        lvl = str(f.get("level") or "").upper()
        if lvl in counts:
            counts[lvl] += 1
    status = "fail" if counts["FAIL"] else ("warn" if counts["WARN"] else "ok")
    return {"node": node or "all", "status": status, "counts": counts, "findings": list(findings)}


def make_tools(health_fn, snapshot_fn):
    """Bind the pure protocol code to two data callables plus explain."""
    def stack_health(a):
        node = a.get("node") or ""
        return health_body(filter_node(health_fn(), node), node)

    def stack_snapshot(a):
        s = snapshot_fn()
        node = a.get("node") or ""
        if not node:
            return s
        nodes = s.get("nodes") or {}
        if node not in nodes:
            return {"error": "no node %r; known: %s" % (node, ", ".join(sorted(nodes)))}
        return {"taken_at": s.get("taken_at"), "nodes": {node: nodes[node]}}

    def explain_tool(a):
        return explain(str(a.get("code") or ""))

    return {"stack_health": stack_health, "stack_snapshot": stack_snapshot, "explain": explain_tool}


def default_tools(nodes_path, ctx_path=None, state_path=None, ttl_s=10.0, clock=time.monotonic):
    """Bind the real collector and doctor. A snapshot is reused for ttl_s seconds; stack_health grades against the
    saved state without recording a sample, so repeated calls never turn LIKELY into VERIFIED and never write state."""
    from . import cli as sg_cli
    from . import collect as sg_collect

    def read(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    cache = {"at": None, "snapshot": None}

    def snapshot():
        now = clock()
        if cache["at"] is None or now - cache["at"] >= ttl_s:
            cache["snapshot"], cache["at"] = sg_collect.snapshot(read(nodes_path)), now
        return cache["snapshot"]

    def health():
        return sg_cli.doctor(sg_cli.DEFAULT_CHECKS_DIR, snapshot(), state_path,
                             ctx=read(ctx_path) if ctx_path else {}, record=False)[0]

    return make_tools(health, snapshot)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()

    def opt(name):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    nodes = opt("--nodes") or os.environ.get("STACK_GUARDIAN_NODES")
    try:
        ttl = float(opt("--ttl") or 10)
    except ValueError:
        ttl = None
    if not nodes or ttl is None:
        print("stack-guardian-mcp: usage: --nodes FILE (or STACK_GUARDIAN_NODES) [--ctx FILE] [--state FILE] [--ttl SECONDS]",
              file=sys.stderr)
        return 3

    def write(line):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    serve(sys.stdin, write, default_tools(nodes, opt("--ctx"), opt("--state"), ttl_s=ttl))
    return 0


def _selftest():
    calls = {"h": 0}
    F = [{"code": "ollama.unreachable", "node": "gpu-box", "level": "FAIL"}, {"code": "vram.unattributed", "node": "gpu-box-2", "level": "WARN"}]
    S = {"taken_at": "t", "nodes": {"gpu-box": {"reachable": False}, "gpu-box-2": {"reachable": True}}}

    def health():
        calls["h"] += 1
        return list(F)
    tools = make_tools(health, lambda: S)

    def req(i, method, params=None):
        m = {"jsonrpc": "2.0", "id": i, "method": method}
        if params is not None:
            m["params"] = params
        return m

    def text_of(resp):
        return resp["result"]["content"][0]["text"]

    def c_initialize():
        r = handle(req(1, "initialize", {}), tools)
        return r["id"] == 1 and r["result"]["protocolVersion"] == PROTOCOL and r["result"]["serverInfo"]["name"] == "stack-guardian"

    def c_tools_list():
        r = handle(req(2, "tools/list"), tools)
        return [t["name"] for t in r["result"]["tools"]] == ["stack_health", "stack_snapshot", "explain"]

    def c_notification_silent():
        return handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, tools) is None

    def c_unknown_method():
        r = handle(req(3, "resources/list"), tools)
        return r["error"]["code"] == -32601 and "id" in r and "result" not in r

    def c_invalid_request():
        return handle({"jsonrpc": "2.0", "id": 9}, tools)["error"]["code"] == -32600 and handle("x", tools) is None

    def c_health_all():
        r = handle(req(4, "tools/call", {"name": "stack_health", "arguments": {}}), tools)
        body = json.loads(text_of(r))
        return r["result"]["isError"] is False and body["node"] == "all" and len(body["findings"]) == 2

    def c_health_node():
        body = json.loads(text_of(handle(req(5, "tools/call", {"name": "stack_health", "arguments": {"node": "gpu-box-2"}}), tools)))
        return [f["code"] for f in body["findings"]] == ["vram.unattributed"] and body["node"] == "gpu-box-2"

    def c_snapshot_node_and_unknown():
        a = json.loads(text_of(handle(req(6, "tools/call", {"name": "stack_snapshot", "arguments": {"node": "gpu-box"}}), tools)))
        b = json.loads(text_of(handle(req(7, "tools/call", {"name": "stack_snapshot", "arguments": {"node": "zz"}}), tools)))
        return a == {"taken_at": "t", "nodes": {"gpu-box": {"reachable": False}}} and b["error"].startswith("no node 'zz'; known: gpu-box, gpu-box-2")

    def c_explain_known_and_unknown():
        k = explain("tailnet.relayed")
        u = explain("nope")
        return set(k) == {"summary", "try", "evidence_needed"} and k["try"] and u["summary"] == "unknown code 'nope'"

    def c_explain_returns_copies():
        explain("model.not_loaded")["try"].append("x")
        return "x" not in EXPLAIN["model.not_loaded"]["try"]

    def c_unknown_tool_is_error_result():
        r = handle(req(8, "tools/call", {"name": "fix_everything"}), tools)
        return r["result"]["isError"] is True and text_of(r) == "unknown tool: fix_everything"

    def c_tool_exception_is_error_result():
        bad = make_tools(lambda: 1 / 0, lambda: S)
        r = handle(req(10, "tools/call", {"name": "stack_health", "arguments": {}}), bad)
        return r["result"]["isError"] is True and text_of(r).startswith("ZeroDivisionError")

    def c_serve_lines():
        out = []
        n = serve(['{"jsonrpc":"2.0","id":1,"method":"ping"}', "", "not json",
                   '{"jsonrpc":"2.0","method":"notifications/initialized"}'], out.append, tools)
        parsed = [json.loads(x) for x in out]
        return n == 2 and parsed[0] == {"jsonrpc": "2.0", "id": 1, "result": {}} and parsed[1]["error"]["code"] == -32700

    def c_health_body_counts_and_status():
        clean = health_body([], "")
        mixed = health_body(F, "")
        warn_only = health_body([F[1], {"code": "x", "node": "n", "level": "info"}], "gpu-box-2")
        return (clean == {"node": "all", "status": "ok", "counts": {"FAIL": 0, "WARN": 0, "INFO": 0, "OK": 0}, "findings": []}
                and mixed["status"] == "fail" and mixed["counts"]["FAIL"] == 1 and mixed["counts"]["WARN"] == 1
                and warn_only["status"] == "warn" and warn_only["counts"]["INFO"] == 1 and warn_only["node"] == "gpu-box-2")

    def c_read_only_specs():
        return all(t["name"] in ("stack_health", "stack_snapshot", "explain") for t in TOOL_SPECS) and len(TOOL_SPECS) == 3

    checks = [("initialize", c_initialize), ("tools_list", c_tools_list), ("notification_silent", c_notification_silent),
              ("unknown_method", c_unknown_method), ("invalid_request", c_invalid_request), ("health_all", c_health_all),
              ("health_node", c_health_node), ("snapshot_node_and_unknown", c_snapshot_node_and_unknown),
              ("explain_known_and_unknown", c_explain_known_and_unknown), ("explain_returns_copies", c_explain_returns_copies),
              ("unknown_tool_is_error_result", c_unknown_tool_is_error_result),
              ("tool_exception_is_error_result", c_tool_exception_is_error_result), ("serve_lines", c_serve_lines),
              ("health_body_counts_and_status", c_health_body_counts_and_status), ("read_only_specs", c_read_only_specs)]
    passed = failed = 0
    for name, fn in checks:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("FAIL %s -- raised %s: %s" % (name, type(exc).__name__, exc))
        else:
            print(("ok %s" if ok else "FAIL %s") % name)
        passed += ok
        failed += not ok
    print("mcp_server selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
    return 0 if failed == 0 and checks else 1


if __name__ == "__main__":
    sys.exit(main())