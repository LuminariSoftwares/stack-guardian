# stack-guardian

A health doctor for a home AI stack. It collects a snapshot of your nodes (Ollama endpoints, loaded models, NVIDIA
GPUs, tailnet peers), runs small Python rules against it, remembers the last runs, and reports findings with a
confidence grade: one sample is at most `LIKELY`, the same finding twice in a row is `VERIFIED`. A finding can carry a
typed fix that you run on purpose, never automatically.

Status: **0.1.0.dev0, pre-release.** Standard library only, Python 3.11+.

## Install

```
git clone https://github.com/LuminariSoftwares/stack-guardian.git
cd stack-guardian
python -m venv .venv
.venv\Scripts\activate          # Windows;  macOS/Linux: source .venv/bin/activate
pip install -e .
```

This gives you three commands: `stack-guardian` (the CLI), `stack-guardian-serve` and `stack-guardian-mcp`.

## What it does

### 1. `doctor` - run the checks

Describe your nodes in a JSON file (see [examples/nodes.json](examples/nodes.json)):

```
stack-guardian doctor --nodes examples/nodes.json --ctx examples/ctx.json
```

Or run the shipped rules against a snapshot you already have (works offline, try it now):

```
stack-guardian doctor --snapshot examples/snapshot.json
```

Options: `--checks-dir DIR` (your own rules, default is the shipped `checks.d`), `--ctx FILE` (rule settings such as
the model each node is expected to hold), `--state FILE` (default `$STACK_GUARDIAN_STATE` or
`~/.stack-guardian/state.json`), `--json`, `--quiet` (print nothing when all is OK).

Exit codes: `0` all OK/INFO, `1` a WARN, `2` a FAIL, `3` usage, `4` the doctor itself crashed.

Shipped rules: `ollama.unreachable`, `model.not_loaded`, `vram.unattributed`, `tailnet.peer_offline`,
`tailnet.relayed`. A rule that raises becomes a `check.crashed` WARN - a buggy rule never reports a broken stack.

### 2. `doctor --fix ID` - run a finding's typed fix

A rule may attach `fix={"action": ..., "args": {...}}` to a finding. There are exactly three actions and never a raw
shell string:

| action | what it does | needs |
|---|---|---|
| `unload_model` | POST `keep_alive: 0` to an Ollama `/api/generate` | `node`, `url`, `model` |
| `restart_lane` | runs the argv list you declared for that lane | `lane` declared in `--config` |
| `set_env` | writes `NAME=value` to `<state folder>/env` and sets it for the process | `name`, `value` |

```
stack-guardian doctor --checks-dir examples/checks.d --snapshot examples/snapshot.json --fix atlas.stuck --config examples/lanes.json
```

That is a **dry run**: it prints the plan and changes nothing. Add `--apply` to perform it. An applied fix first saves
the snapshot under `<state folder>/snapshots/`, then runs, then appends one JSON line to `<state folder>/fixes.jsonl`
(`--audit FILE` to move it). A `set_env` value is never written to the audit, only its length. `ID` is the finding
code, or `code@node` when several nodes raise the same code. With `--fix`, the exit code is the fix's: `0` plan or
success, `1` refused or failed, `3` usage.

### 3. Other commands

- `stack-guardian snapshot --nodes FILE` prints the raw snapshot the rules read.
- `stack-guardian-serve --nodes FILE [--port 8788]` serves a fresh snapshot at `GET /snapshot` (read-only, loopback
  unless you pass `--host H --allow-remote`), so one machine can ask another for its health.
- `stack-guardian-mcp --nodes FILE [--ctx FILE] [--state FILE]` is a read-only MCP stdio server with the tools
  `stack_health`, `stack_snapshot` and `explain`.
- `dsh/` is a plugin for the DeepSeek Harness that adds one line at session start when the doctor reports WARN or
  FAIL (Node 20+; see `dsh/index.js`).

## Writing a rule

```python
from stack_guardian import finding as F

def ollama_up(snapshot, ctx):
    if not snapshot["nodes"]["atlas"].get("reachable"):
        return F.make_finding("ollama.unreachable", "missing", "FAIL", "atlas", "Ollama does not answer")
    return None

CHECKS = [("ollama.up", ollama_up)]
```

Drop the file in a folder and point `--checks-dir` at it. A full example with a fix is in
[examples/checks.d/20_atlas_fix.py](examples/checks.d/20_atlas_fix.py).

## Examples

| file | what it is |
|---|---|
| `examples/nodes.json` | two nodes: a local one with GPUs, a remote one over the tailnet |
| `examples/ctx.json` | rule settings: which model atlas should hold |
| `examples/snapshot.json` | an offline snapshot with one dead node, for trying `doctor` and `--fix` |
| `examples/lanes.json` | the lanes `restart_lane` is allowed to run |
| `examples/checks.d/20_atlas_fix.py` | a custom rule whose finding carries a fix |

## Test

```
pip install pytest
python -m pytest -q
```

CI runs this on Ubuntu, Windows and macOS with Python 3.11. To publish a release see [RELEASE_STEPS.md](RELEASE_STEPS.md).

## License

MIT
