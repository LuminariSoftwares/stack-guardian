# Architecture decision records

Short records of the choices already in the code, written after the fact from the code and its history. Each has the
same four parts: context, decision, alternatives, consequences.

| ADR | decision |
|---|---|
| [0001](0001-standard-library-only.md) | Standard library only, no runtime dependencies |
| [0002](0002-rules-as-plain-python-files.md) | Health rules are plain Python files in a folder, isolated from each other's crashes |
| [0003](0003-confidence-from-run-history.md) | Confidence comes from run history: one sample is at most LIKELY |
| [0004](0004-typed-audited-fixes.md) | Fixes are three typed actions, dry run by default, snapshotted and audited |
| [0005](0005-read-only-agent-surfaces.md) | The MCP server and HTTP endpoint are read-only and never record history |
| [0006](0006-injectable-collectors-one-deadline.md) | Collectors are injectable, a dead node is data, and one deadline bounds a run |
