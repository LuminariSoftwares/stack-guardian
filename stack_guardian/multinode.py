# Multi-node snapshot pull: every node at once, one overall deadline.
"""Collect health data from several machines at once, without letting one hung
node stall the whole check.

Every node is handed to ``collect_one(name, cfg)`` in its own thread at the same
instant, and a single overall deadline is applied to all of them. Whatever has
not answered by the deadline is reported as down instead of being waited on, so
a run always returns within ``timeout_s`` plus a little slack.

The collector threads are daemon threads on purpose: a node that answers one
byte at a time keeps its thread alive long after the deadline, and the report is
only honest if the process ends with it rather than waiting for that node.
"""

import json
import sys
import threading
import time
from datetime import datetime, timezone


def _down(error):
    return {"reachable": False, "error": error, "ollama": []}


def pull(nodes, collect_one, timeout_s=5.0, now=None):
    """Pull ``nodes`` (name -> cfg, cfg may be None) concurrently.

    Returns ``{"schema": 1, "taken_at": ..., "nodes": {...}}`` with the node
    results in the same order as the input dict. A node that raised, returned a
    non-dict or did not answer before the deadline is reported as down.
    """
    items = list((nodes or {}).items())
    result = {}
    answers = {}  # name -> ("ok", value) | ("raised", exception); one writer per key, read after the deadline

    def work(name, cfg):
        try:
            answers[name] = ("ok", collect_one(name, cfg))
        except BaseException as exc:  # noqa: BLE001 - whatever a collector throws is reported as a down node
            answers[name] = ("raised", exc)

    threads = []
    for name, cfg in items:
        thread = threading.Thread(target=work, args=(name, cfg if cfg is not None else {}),
                                  name="stack-guardian-collect", daemon=True)
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + timeout_s
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    for name, _cfg in items:
        answered = answers.get(name)
        if answered is None:
            result[name] = _down("timeout after %ss" % timeout_s)
        elif answered[0] == "raised":
            exc = answered[1]
            result[name] = _down("%s: %s" % (type(exc).__name__, exc))
        elif isinstance(answered[1], dict):
            result[name] = answered[1]
        else:
            result[name] = _down("collector returned %s" % type(answered[1]).__name__)
    return {
        "schema": 1,
        "taken_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": result,
    }


def elapsed_ok(started, finished, timeout_s, slack_s=1.0):
    """True when a run finished within ``timeout_s`` plus ``slack_s``."""
    return (finished - started) <= timeout_s + slack_s


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return _selftest()
    print(json.dumps({"usage": "multinode.py --selftest (library: pull(nodes, collect_one, timeout_s))"}))
    return 2


def _selftest():
    import threading
    release = threading.Event()

    def fake(name, cfg):
        kind = cfg.get("kind")
        if kind == "hang":
            release.wait(30)
            return {"reachable": True}
        if kind == "slow":
            time.sleep(0.3)
            return {"reachable": True, "who": name}
        if kind == "boom":
            raise ConnectionError("refused")
        if kind == "odd":
            return ["not", "a", "dict"]
        return {"reachable": True, "who": name, "cfg": cfg}

    def c_order_and_shape():
        r = pull({"b": {}, "a": {}}, fake, timeout_s=2, now="T")
        return r["schema"] == 1 and r["taken_at"] == "T" and list(r["nodes"]) == ["b", "a"] and r["nodes"]["a"]["who"] == "a"

    def c_cfg_passed_and_none_cfg():
        r = pull({"a": {"x": 1}, "b": None}, fake, timeout_s=2, now="T")
        return r["nodes"]["a"]["cfg"] == {"x": 1} and r["nodes"]["b"]["cfg"] == {}

    def c_raise_becomes_down():
        r = pull({"d": {"kind": "boom"}}, fake, timeout_s=2, now="T")
        return r["nodes"]["d"] == {"reachable": False, "error": "ConnectionError: refused", "ollama": []}

    def c_non_dict_becomes_down():
        return pull({"o": {"kind": "odd"}}, fake, timeout_s=2, now="T")["nodes"]["o"]["error"] == "collector returned list"

    def c_hang_times_out_fast():
        t0 = time.monotonic()
        r = pull({"h": {"kind": "hang"}, "ok": {}}, fake, timeout_s=0.5, now="T")
        took = time.monotonic() - t0
        release.set()
        return (r["nodes"]["h"] == {"reachable": False, "error": "timeout after 0.5s", "ollama": []}
                and r["nodes"]["ok"]["reachable"] is True and took < 1.5)

    def c_concurrent_not_serial():
        t0 = time.monotonic()
        r = pull({"s1": {"kind": "slow"}, "s2": {"kind": "slow"}, "s3": {"kind": "slow"}}, fake, timeout_s=3, now="T")
        took = time.monotonic() - t0
        return all(n["reachable"] for n in r["nodes"].values()) and took < 0.8

    def c_collector_threads_are_daemon():
        """A node that never answers must not keep the process alive after the report."""
        stay = threading.Event()
        seen = {}

        def peek(name, cfg):
            seen["daemon"] = threading.current_thread().daemon
            stay.wait(30)
            return {"reachable": True}
        try:
            pull({"h": {}}, peek, timeout_s=0.2, now="T")
            return seen.get("daemon") is True
        finally:
            stay.set()

    def c_empty():
        r = pull({}, fake, now="T")
        return r == {"schema": 1, "taken_at": "T", "nodes": {}} and pull(None, fake, now="T")["nodes"] == {}

    def c_default_now_is_iso():
        t = pull({}, fake)["taken_at"]
        return isinstance(t, str) and t[4] == "-" and "T" in t

    def c_elapsed_ok():
        return elapsed_ok(0.0, 5.9, 5) is True and elapsed_ok(0.0, 6.1, 5) is False and elapsed_ok(0.0, 5.4, 5, slack_s=0.2) is False

    checks = [("order_and_shape", c_order_and_shape), ("cfg_passed_and_none_cfg", c_cfg_passed_and_none_cfg),
              ("raise_becomes_down", c_raise_becomes_down), ("non_dict_becomes_down", c_non_dict_becomes_down),
              ("hang_times_out_fast", c_hang_times_out_fast), ("concurrent_not_serial", c_concurrent_not_serial),
              ("collector_threads_are_daemon", c_collector_threads_are_daemon),
              ("empty", c_empty), ("default_now_is_iso", c_default_now_is_iso), ("elapsed_ok", c_elapsed_ok)]
    passed = failed = 0
    for name, fn in checks:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("FAIL %s -- raised %s: %s" % (name, type(exc).__name__, exc))
        else:
            print(("ok %s" if ok else "FAIL %s") % name)
        passed += ok
        failed += not ok
    print("multinode selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
    return 0 if failed == 0 and checks else 1


if __name__ == "__main__":
    sys.exit(main())
