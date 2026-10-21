# 0002 - Rules are plain Python files in a folder

Status: accepted (since the initial import, 2026-10-04)

## Context

Every home stack is different: which models must stay loaded, which ports, which machines. The shipped rules can only
cover the common cases, so users need to add their own without forking the package. A rule written in a hurry will
sometimes be wrong, and a wrong rule must not make the doctor itself unusable or report a healthy stack as broken.

## Decision

A rule file is any `*.py` in a checks folder (`--checks-dir`, default the shipped `checks.d/`) that exports
`CHECKS = [(check_id, fn(snapshot, ctx) -> finding | [findings] | None)]`. `core.load_checks` imports the files in
name order, skipping names that start with `_`; `core.run_all` runs every check concurrently in a thread pool.
`finding.run_check` wraps each call: an exception becomes one `check.crashed` finding with severity `crash`, whose
level is forced to `WARN` and whose evidence carries the exception. A file that fails to import becomes a check that
raises, so it is reported the same way. Findings are built with `finding.make_finding`, which fixes their shape.

## Alternatives

- **A declarative rule format** (YAML or JSON conditions): safe to load from anywhere, but every new kind of
  comparison needs engine work, and real rules need loops over nodes and models.
- **Entry-point plugins**: discoverable, but a rule then needs a package and an install step.
- **Let a crash propagate**: simpler, but one bad rule hides every other finding.

## Consequences

- A rule is ten lines of Python with no registration; `examples/checks.d/20_ollama_fix.py` is a complete one.
- A broken rule shows up as a `check.crashed` WARN, never as a FAIL and never as a dead run.
- Rules run in-process with the user's privileges, so a rule folder is code and must be trusted; the README says so.
- The shipped rules are not a Python package, so they need `package-data` to reach the wheel. 0.1.0 shipped that fix,
  and CI installs the built wheel in a clean virtualenv and checks that the rules fire.
