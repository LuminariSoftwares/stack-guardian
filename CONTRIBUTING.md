# Contributing

Issues and pull requests are welcome. It is a small project; the rules below are what keeps it small.

## Getting set up

```
git clone https://github.com/LuminariSoftwares/stack-guardian.git
cd stack-guardian
python -m venv .venv
.venv\Scripts\activate          # Windows;  macOS/Linux: source .venv/bin/activate
pip install -e ".[test]"
python -m pytest -q
```

`pip install -e .` alone is enough to *run* stack-guardian; the `[test]` extra adds `pytest` and `build`
(`tests/test_spdx_license.py` builds a real wheel).

## What a change needs

- **A test you watched fail first.** Add it, run it, see it red, then write the fix. Say "red -> green" in the pull
  request and name the test.
- **No new runtime dependency.** The package declares `dependencies = []` and installs on any machine with
  Python 3.11. Test-only extras are fine.
- **The public surface keeps working.** Tool names, CLI flags, nodes-file keys, environment variables, JSON fields
  and exit codes are a contract. If one genuinely has to change, keep a deprecated alias and add a CHANGELOG line
  marked **Breaking** with the one-line migration.
- **The MCP tool definitions have a token budget.** They are sent on every request, so
  `tests/test_tools_list_budget.py` caps the whole `tools/list` payload. Never lengthen a description to score
  better in a directory; if you change a tool definition, say in the pull request what the budget was before and
  after.
- **Keep the module selftests passing.** Every module carries a counted `--selftest`
  (`python -m stack_guardian.collect --selftest`) that the suite runs; new behaviour in a module belongs there too.
- **No personal data in a commit.** This repository and its pull requests are public. The `privacy` workflow scans
  every tracked file for addresses, host names, personal names and secrets; examples use `127.0.0.1`,
  `100.64.0.1`-`100.64.0.20`, `gpu-box` and `C:\path\to\project`.
- **One change per pull request**, with a CHANGELOG line under `## Unreleased` for anything a user would notice.

## House style

Standard library only. Plain functions over classes. Collectors take an injected runner or HTTP getter so tests
never touch the network. A dead endpoint is data, never an exception. Comments explain *why*, not *what*.
