# Changelog

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
