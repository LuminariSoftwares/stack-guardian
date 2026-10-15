# Changelog

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
