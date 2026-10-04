# stack-guardian

A health doctor for a home AI stack. It runs small Python checks against a snapshot of your nodes (Ollama, GPUs,
tailnet, harness logs), remembers the last runs, and reports findings with a confidence grade: one sample is at most
`LIKELY`, the same finding twice in a row is `VERIFIED`.

Status: **0.1.0.dev0 — pre-release.** The core, state and `doctor` command exist; the snapshot collectors, the MCP
server and the DSH plugin are still being built.

## Use

```
pip install -e .
stack-guardian doctor --checks-dir checks.d --snapshot snapshot.json [--state state.json] [--json] [--quiet]
```

Exit codes: `0` all OK/INFO · `1` a WARN · `2` a FAIL · `3` usage · `4` the doctor itself crashed.

A check file:

```python
from stack_guardian import finding as F

def ollama_up(snapshot, ctx):
    if not snapshot["nodes"]["atlas"].get("reachable"):
        return F.make_finding("ollama.unreachable", "missing", "FAIL", "atlas", "Ollama does not answer")
    return None

CHECKS = [("ollama.up", ollama_up)]
```

A check that raises becomes a `check.crashed` WARN — a buggy check never reports a broken stack.

## Test

```
python -m pytest -q
```

## License

MIT
