# 0005 - Agent-facing surfaces are read-only

Status: accepted (MCP server since the initial import; `record=False` and the snapshot TTL since the 2026-10-11 review
fixes)

## Context

Coding agents call the MCP tools freely and repeatedly, often several times in one turn. Other machines on a tailnet
poll the HTTP endpoint. Neither caller should be able to change the stack, and repeated calls should not change the
answer: if every `stack_health` call recorded a run, an agent asking twice in a second would turn a one-sample blip into
a `VERIFIED` finding (ADR 0003).

## Decision

- The MCP server exposes only `stack_health`, `stack_snapshot` and `explain`, all annotated `readOnlyHint: true`,
  `destructiveHint: false`, `idempotentHint: true`; only `explain` is `openWorldHint: false`, because it is a local
  table lookup. There is no fix tool.
- `stack_health` calls the doctor with `record=False`: it grades against the saved history without adding to it.
- One snapshot is reused for `--ttl` seconds (default 10), so a burst of calls probes the nodes once.
- `stack-guardian-serve` answers `GET /snapshot`, `/metrics` and `/health`, refuses every write verb, and refuses to
  bind a non-loopback address unless `--allow-remote` is passed.

## Alternatives

- **Expose fixes as MCP tools behind a confirmation**: convenient, but the confirmation lives in the client, which
  this server cannot see or enforce.
- **Record every MCP call**: more history, but grading would depend on how chatty the agent is.
- **Collect on every call**: always fresh, but a burst of agent calls becomes a burst of probes against every node.

## Consequences

- An agent can diagnose and explain but has to hand the fix to a person (`stack-guardian doctor --fix ID --apply`).
- Data is up to `--ttl` seconds old.
- The HTTP endpoint has no auth; it relies on loopback or a private network.
