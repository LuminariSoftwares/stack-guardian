# Changelog

## Unreleased

- **Renamed MCP tool `explain` to `stack_explain`** so all three tools share the `stack_` prefix (Glama scored Naming
  Consistency 3/5 for the mix). `tools/list` advertises only the new name; a `tools/call` for `explain` still works
  as a deprecated alias.
- **Fix:** a `tools/call` whose `name` was not a string (for example a list) raised `TypeError` and ended the stdio
  server; it now returns a tool error result.
- **MCP tool definitions:** every tool description now says when to use it, when to use each sibling instead, its
  behaviour (read-only, network or not, the 10 s snapshot reuse) and the JSON it returns. Every parameter declares
  `Optional.`/`Required.`, a default where optional, `maxLength`, `examples`, and for `code` a `pattern`; both input
  schemas set `additionalProperties: false`. This server does not validate arguments against the schemas, so nothing
  it accepted before is rejected; a client that validates on its side may now refuse a malformed `code` earlier.
- **`stack_explain` covers `check.crashed`**, the WARN a crashing rule produces, which `stack_health` could report
  but `explain` answered as "unknown code".
- **Tests:** `tests/test_tool_definitions.py` fails if a tool or parameter loses its description, type, example,
  default or constraints, if a shipped finding code has no `stack_explain` entry, or if the README tool reference is stale.
- **README** reorganised: 60-second quick start, architecture diagram and design decisions, client configs for Claude
  Desktop, Claude Code and any stdio client, a tool reference generated from the live schemas
  (`scripts/tool_reference.py`), limits, how it is tested, credits.
- **Docs:** six architecture decision records in `docs/adr/`.
- **Dockerfile** (stdio MCP server as a non-root user, example nodes file baked in) and a CI job that builds it and
  lists its tools.

## stack-guardian 0.2.1 (2026-10-14)

- **Fix:** the MCP Registry name is `io.github.LuminariSoftwares/stack-guardian` (the registry matches the GitHub owner's case); 0.2.0 carried the lower-case name and could not be published there.

## stack-guardian 0.2.0 / dsh-stack-guardian 0.1.0-alpha.2 (2026-10-14)

- **`stack-guardian mcp`** starts the MCP server (same as `stack-guardian-mcp`), so `uvx stack-guardian mcp` works; `server.json` for the official MCP Registry.

- **OpenAI-compatible nodes:** `"kind": "openai"` reads LM Studio, vLLM and llama.cpp server from `/v1/models`
  into the Ollama endpoint shape, so every shipped rule applies.
- **`model.expiring`** (WARN) from `/api/ps` `expires_at`; INFO when `/api/ps` is unavailable.
- **`doctor --watch N`:** prints only findings that changed state; `--json` gives one object per change.
- **`GET /metrics`** on `stack-guardian-serve`, Prometheus text format.
- **`--ttft`:** opt-in 1-token time-to-first-token probe, with the INFO rule `model.slow_ttft`.
- **MCP tool annotations:** `stack_health`, `stack_snapshot` and `explain` declare a `title` and
  `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` (all read-only; only `explain` stays off the network).
- **MCP Registry markers:** `mcp-name: io.github.luminarisoftwares/stack-guardian` in the README (and so in the PyPI
  description) and `mcpName` in `dsh/package.json`, ready for a registry listing.
- **Fix:** an Ollama endpoint whose `/api/ps` answered 4xx/5xx was reported as `ollama.unreachable`; it is now
  reachable with a `ps_error`.
- **Fix:** `latency_ms` and `ttft_ms` are timed with `perf_counter`; on Windows the monotonic clock ticks every
  ~16 ms, so short timings came out rounded or under-reported.

## stack-guardian 0.1.0 / dsh-stack-guardian 0.1.0-alpha.1 (2026-10-10)

First public release.

- **`doctor`** collects a snapshot of your nodes (Ollama endpoints and loaded models, NVIDIA GPUs, tailnet peers),
  runs the rules and grades each finding: one sample is at most `LIKELY`, the same finding twice in a row is
  `VERIFIED`. Exit codes `0` OK/INFO, `1` WARN, `2` FAIL, `3` usage, `4` crash.
- **Shipped rules:** `node.unreachable`, `ollama.unreachable`, `model.not_loaded`, `vram.unattributed`,
  `gpu.unavailable`, `tailnet.unavailable`, `tailnet.peer_offline`, `tailnet.relayed`, one finding per affected node.
  A rule that raises becomes a `check.crashed` WARN.
- **`doctor --fix ID`:** typed fixes (`unload_model`, `restart_lane`, `set_env`), dry run by default, `--apply` to
  run, with a snapshot and an audit line for every applied fix.
- **`stack-guardian-mcp`:** MCP server over stdio with `stack_health`, `stack_snapshot` and `explain`. It grades
  against saved history without adding to it, and reuses a snapshot for 10 s.
- **`stack-guardian-serve`:** read-only `GET /snapshot`, loopback unless `--allow-remote`.
- **DSH plugin** (`dsh-stack-guardian` on npm): one start-of-session line on WARN or FAIL.
- **Packaging:** the shipped rules (`checks.d`) are included in the wheel; CI installs the built wheel in a clean
  environment and runs the example snapshot, so a release without its rules fails CI.
