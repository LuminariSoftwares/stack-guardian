# 0003 - Confidence comes from run history

Status: accepted (since the initial import, 2026-10-04)

## Context

Home stacks are noisy: a model is mid-load, a laptop wakes up, a tailnet path flips to a relay for a few seconds. A
doctor that reports every blip with full certainty trains people (and agents) to ignore it, while one that hides blips
misses real outages.

## Decision

Every finding carries a `confidence` of `VERIFIED`, `LIKELY` or `INSUFFICIENT`. `state.py` keeps the last 5 runs per
node in a JSON file (`--state`, `$STACK_GUARDIAN_STATE` or `~/.stack-guardian/state.json`). `state.grade` raises a
WARN or FAIL finding to `VERIFIED` only when its code was bad in at least two consecutive runs for that node, and
lowers a single-sample `VERIFIED` to `LIKELY`. Only WARN and FAIL findings are graded; OK and INFO keep the confidence
their rule gave them. That includes `check.crashed`: the runner marks it `VERIFIED`, and grading lowers it to `LIKELY`
until the same rule crashes again. A missing or corrupt state file is replaced by a fresh one, never fatal.

## Alternatives

- **Report every finding as certain**: simplest, but noisy.
- **Retry inside one run before reporting**: catches some blips, but makes every run slower and still cannot tell a
  ten-minute outage from a ten-second one.
- **A time-series store**: more precise, but a database dependency for a doctor (see ADR 0001).

## Consequences

- One run is never more than `LIKELY`; a real outage needs a second run (or `--watch`) to become `VERIFIED`.
- History is local to one state file. The MCP server reads it but never writes it (ADR 0005), so on its own it
  reports at most `LIKELY` unless the CLI shares the same state file.
