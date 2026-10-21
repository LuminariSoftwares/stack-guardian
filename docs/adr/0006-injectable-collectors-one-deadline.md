# 0006 - Injectable collectors, a dead node is data, one deadline per run

Status: accepted (since the initial import, 2026-10-04)

## Context

The doctor is most needed when parts of the stack are down: a node is off, an endpoint hangs, `nvidia-smi` is missing.
A collector that raises or waits forever on one node would hide the state of all the others. The collectors also have
to be testable on a CI runner with no GPU, no Ollama and no tailnet.

## Decision

- Every collector in `collect.py` takes its command runner, HTTP getter and clock as parameters (`run`, `http_get`,
  `clock`, `post_first`); the defaults use `subprocess` and `urllib`, the selftests pass fakes.
- A failure is recorded in the snapshot (`error`, `gpu_error`, `ps_error`, `reachable: false`), never raised.
- `multinode.pull` starts every node in its own thread at once and applies one overall deadline; a node that has not
  answered, raised, or returned garbage is reported down.
- Parsing lives in `parsers.py` as pure functions over text, separate from the probing.
- Timings use `perf_counter`, after Windows' ~16 ms monotonic tick under-reported short latencies (0.2.0).

## Alternatives

- **Sequential collection with per-node timeouts**: simpler, but the run time grows with the number of dead nodes.
- **asyncio**: one thread, but every collector (and every user rule touching I/O) would have to be async.
- **Mocking at the module level in tests**: works, but hides which calls a collector actually makes.

## Consequences

- A run returns within the deadline plus a little slack however many nodes are down, and the rules see the failures as
  data they can grade.
- Every collector and parser is covered by selftests that run without network, GPU or tailnet.
- A thread stuck in a hung call is abandoned, not killed, until its own socket timeout fires.
