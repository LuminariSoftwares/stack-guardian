# 0004 - Fixes are typed, dry-run by default, and audited

Status: accepted (added 2026-10-04, wired into the CLI as `doctor --fix ID [--apply]`)

## Context

Many findings have an obvious remedy (unload a stuck model, restart a service, set an environment variable). Letting a
rule hand back a shell command would make every rule file a remote-code path and make it impossible to say afterwards
what was changed.

## Decision

A finding may carry `fix = {"action": ..., "args": {...}}` with one of exactly three actions (`fixes.ACTIONS`):
`unload_model` (POST `keep_alive: 0` to an Ollama endpoint), `restart_lane` (run an argv list the user declared for
that lane in `--config`, never one from the rule) and `set_env` (name checked against `^[A-Z_][A-Z0-9_]{0,63}$`, value
at most 4096 characters). `finding.make_finding` rejects a fix that is not `{action: str, args: dict}`, and `fixes.plan_fix` refuses an unknown
action or bad arguments. `fixes.apply_fix` only prints the plan unless
`apply=True`; when applying it saves the current snapshot first, runs the action through an injected dependency, and
appends one JSON audit line. A `set_env` value is never written to the audit, only its length.

## Alternatives

- **Shell strings from rules**: maximally flexible, but turns every rule into arbitrary command execution.
- **Auto-apply on FAIL**: faster recovery, but a wrong rule would act on a healthy stack with no one watching.
- **No fixes at all**: safest, but leaves the most common remedies as copy-paste from a README.

## Consequences

- Anything beyond the three actions has to be done by hand or added as a new typed action with its own tests.
- Every applied fix can be traced to a snapshot and an audit line; a dry run changes nothing.
- No agent-facing surface can apply a fix (ADR 0005).
