# 0001 - Standard library only

Status: accepted (since the initial import, 2026-10-04)

## Context

The tool runs on the machines it diagnoses: GPU boxes and small home servers, often with a Python that also hosts
other AI tooling. A doctor that fails to install, or that drags in a dependency conflicting with what it is checking,
is useless at the moment it is needed. It also has to start fast enough to run at the start of every agent session.

## Decision

`pyproject.toml` declares `dependencies = []`. HTTP goes through `urllib.request`, concurrency through
`concurrent.futures`, the MCP JSON-RPC loop (`mcp_server.py`), the HTTP endpoint (`serve.py`, `http.server`) and the
Prometheus text renderer are written by hand. Every module also carries a `--selftest` and can run as a plain file
(the `except ImportError: import sg_*` fallbacks), which is how the modules were first developed.

## Alternatives

- **The official MCP Python SDK** for the server: less protocol code here, but it pulls in pydantic, anyio and an HTTP
  stack, and pins a minimum Python.
- **requests / httpx** for probing: nicer API, but a dependency for four GET/POST calls.
- **prometheus_client** for `/metrics`: correct by construction, but another dependency for three gauges.

## Consequences

- `pip install stack-guardian` and `uvx stack-guardian mcp` work on any Python 3.11+ with nothing else, and the Docker
  image is the base Python image plus this package.
- The hand-written protocol code is ours to keep correct: each surface has its own counted selftest, and the MCP
  server is also tested over real stdio in a subprocess.
- Newer MCP protocol features (`outputSchema`, `structuredContent`, HTTP transport) have to be added by hand; the
  server speaks protocol `2024-11-05`.
