# Changelog

## Unreleased

Review fixes (external review, 2026-10-21). Everything below has a test that was red before the change.

- **Breaking: the nodes file is validated when it is read.** Migration: fix the node the error names, or add
  `"local": true` / `"tailnet_host": "NAME"` to a node that has no endpoints on purpose. Shapes that used to be
  accepted and answered wrongly are now a usage error (exit `3`) naming the node: `"endpoints"` on a node that is
  not marked `"kind": "openai"`, a node with nothing to probe, a URL without a scheme, an unknown `"kind"`, and a
  top-level JSON list (which used to crash with `AttributeError` and exit `4`). A configuration that reported
  `worst: OK` and exit `0` while checking nothing now fails loudly. `"ollama": "http://..."` - a single string
  where a list belongs - is accepted as a one-element list with one note on stderr; it used to be iterated per
  character ("18 Ollama endpoint(s) do not answer: h, t, t, p, ...").
- **New: keys starting with `_` in a nodes file are ignored**, so a file can carry comments and candidate nodes,
  and `"_options": {"timeout_s": N}` sets that file's collection deadline.
- **The confidence grade is on the human `doctor` line**, not only in `--json`:
  `FAIL ollama.unreachable [gpu-box]: ... (LIKELY)`, with `worst: X` still the last line of stdout and one stderr
  hint when a WARN or FAIL is only `LIKELY`. `--json` is unchanged apart from a new `"schema": 1`.
- **`stack-guardian --version`** prints the version and exits `0`; it used to be a usage error. `--help` now lists
  `doctor`, `snapshot`, `init` and the `mcp` subcommand.
- **New: `stack-guardian init`** writes a starter nodes file for this machine (probes the local Ollama, lists
  tailnet peers as commented-out candidates, prints the next command). It never overwrites an existing file.
- **Fix: the process ends with the report.** A node that answered one byte at a time kept the collector thread -
  and the whole process - alive long after the 10 s deadline (measured: still running at 90 s). Collectors are
  daemon threads now. The deadline is settable with `--timeout SECONDS` or `"_options": {"timeout_s": N}`, and the
  `node.unreachable` advice names the flag that exists.
- **Fix: `--fix ID` as a dry run prints what it would do** - the argv it would run, the request it would send, or
  the variable it would set - instead of only a one-line description.
- **`gpu.unavailable` and `tailnet.unavailable` say why**: `nvidia-smi`/`tailscale` is not on PATH, or it ran and
  failed. The two need different fixes.
- **New rule `gpu.parse_degraded` (INFO):** a GPU line `nvidia-smi` filled with `[N/A]` used to vanish from the
  report, so a card the machine has went unmentioned. It is reported, with the line as evidence, and
  `stack_explain` covers it.
- **Fix: `stack-guardian-serve --host 0.0.0.0` without `--allow-remote`** printed a traceback; it prints one error
  line and exits `3`.
- **New: `stack-guardian-serve --cache SECONDS`** (default `0`, off) reuses one snapshot for a scrape interval
  faster than your nodes answer. The documented default - a fresh snapshot per request - is unchanged.
- **Private by default:** the state folder is created `0700`, and `env`, `fixes.jsonl` and `snapshots/*` `0600`.
  `set_env` used to write its values with whatever umask was in force.
- **MCP tool definitions cut from 1441 to 642 tokens** (budget 650, enforced by `tests/test_tools_list_budget.py`
  against the real server). Same three tools, same arguments, same annotations; `stack_health` and
  `stack_snapshot` now say that the text they return is data quoted from your nodes.
- **MCP: a JSON line that is not a request** (a list, a string, an object with no method) answers `-32600` instead
  of nothing, and the server keeps reading. An unknown method, `server/discover` included, still answers `-32601`.
- **Docs:** `SECURITY.md` (private reporting, and what the tool does by design) and `CONTRIBUTING.md`. The README
  gains the guardian family, a note that this is unrelated to the StackGuardian company, a Windows/pipx quick
  start, a troubleshooting table by finding code, and a "what leaves the machine, and what is written" section.
  The claim that you could "run `stack-guardian-serve` on each GPU box" to collect remote GPUs is gone: nothing in
  the package reads a remote `/snapshot`; it is a Prometheus scrape target.
- **Packaging:** a `test` extra (`pip install -e ".[test]"`), because `pip install -e . pytest` failed
  `tests/test_spdx_license.py` for want of `build`. Classifier `Development Status :: 3 - Alpha`.
- **CI:** Ubuntu also runs Python 3.12 and 3.13, and every third-party action is pinned to a commit SHA.

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
