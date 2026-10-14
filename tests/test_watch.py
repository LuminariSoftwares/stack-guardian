"""Item 3: doctor --watch N prints only findings that changed state, with a timestamp; Ctrl+C exits 0 with a
one-line summary; with --json, one JSON object per change."""
import json

from stack_guardian import cli

SNAP_1 = {"schema": 1, "taken_at": "T1", "nodes": {
    "gpu-box": {"reachable": False, "ollama": [{"url": "http://127.0.0.1:9", "error": "OSError: refused", "running": []}]},
    "gpu-box-2": {"reachable": True, "ollama": [{"url": "http://100.64.0.2:11434", "error": None, "running": [{"name": "qwen3:8b"}]}]}}}
SNAP_2 = {"schema": 1, "taken_at": "T2", "nodes": {
    "gpu-box": {"reachable": True, "ollama": [{"url": "http://127.0.0.1:11434", "error": None, "running": []}]},
    "gpu-box-2": {"reachable": False, "error": "timeout after 10.0s", "ollama": []}}}
CTX = {"expect": {"gpu-box-2": {"model": "qwen3:8b"}, "gpu-box": {"model": "qwen3:30b"}}}


def _drive(tmp_path, as_json):
    snaps = iter([SNAP_1, SNAP_1, SNAP_2])
    slept, out, err = [], [], []
    clock = iter(["2026-10-14T12:00:00+00:00", "2026-10-14T12:00:05+00:00", "2026-10-14T12:00:10+00:00"])

    def run_once():
        return cli.doctor(cli.DEFAULT_CHECKS_DIR, next(snaps), str(tmp_path / "s.json"), ctx=CTX)[0]

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            raise KeyboardInterrupt

    rc = cli.watch(run_once, 5, sleep=sleep, out=out.append, err=err.append, now=lambda: next(clock), as_json=as_json)
    return rc, slept, out, err


def test_watch_prints_only_changes_and_exits_0_on_ctrl_c(tmp_path):
    rc, slept, out, err = _drive(tmp_path, as_json=False)
    assert rc == 0 and slept == [5, 5, 5]
    lines, summary = out[:-1], out[-1]
    # run 1: one new finding; run 2: identical, prints nothing; run 3: resolved first, then new in rule order
    assert lines == [
        "2026-10-14T12:00:00+00:00 new FAIL ollama.unreachable@gpu-box: 1 Ollama endpoint(s) do not answer: http://127.0.0.1:9",
        "2026-10-14T12:00:10+00:00 resolved ollama.unreachable@gpu-box (was FAIL)",
        "2026-10-14T12:00:10+00:00 new FAIL node.unreachable@gpu-box-2: node did not answer the collector: timeout after 10.0s",
        "2026-10-14T12:00:10+00:00 new WARN model.not_loaded@gpu-box: expected model not loaded: gpu-box wants qwen3:30b",
    ], lines
    assert summary == "watch stopped: 3 runs, 4 changes, worst now FAIL" and err == []


def test_watch_severity_change_is_reported():
    runs = iter([[{"code": "x.y", "node": "gpu-box", "level": "INFO", "summary": "a"}],
                 [{"code": "x.y", "node": "gpu-box", "level": "WARN", "summary": "b"}]])
    out = []

    def sleep(_):
        if len(out) >= 2:
            raise KeyboardInterrupt

    assert cli.watch(lambda: next(runs), 1, sleep=sleep, out=out.append, err=out.append, now=lambda: "T") == 0
    assert out == ["T new INFO x.y@gpu-box: a", "T changed INFO->WARN x.y@gpu-box: b",
                   "watch stopped: 2 runs, 2 changes, worst now WARN"]


def test_watch_json_one_object_per_change(tmp_path):
    rc, _, out, err = _drive(tmp_path, as_json=True)
    objs = [json.loads(line) for line in out]
    assert rc == 0 and len(objs) == 4
    assert [(o["change"], o["code"], o["node"], o["level"], o["was"]) for o in objs] == [
        ("new", "ollama.unreachable", "gpu-box", "FAIL", None),
        ("resolved", "ollama.unreachable", "gpu-box", None, "FAIL"),
        ("new", "node.unreachable", "gpu-box-2", "FAIL", None),
        ("new", "model.not_loaded", "gpu-box", "WARN", None)]
    assert objs[0]["at"] == "2026-10-14T12:00:00+00:00"
    assert err == ["watch stopped: 3 runs, 4 changes, worst now FAIL"]  # stdout stays pure JSON lines


def test_watch_usage_errors():
    assert cli.main(["doctor", "--snapshot", "x.json", "--watch", "0"]) == cli.USAGE
    assert cli.main(["doctor", "--snapshot", "x.json", "--watch", "5", "--fix", "a.b"]) == cli.USAGE
