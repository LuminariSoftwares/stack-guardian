"""Run history for stack-guardian: the last runs per node, streaks, and confidence grading."""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

STATE_ENV = "STACK_GUARDIAN_STATE"
KEEP = 5
BAD = ("WARN", "FAIL")
DIR_MODE = 0o700   # the state folder holds a snapshot of your machines: owner only
FILE_MODE = 0o600  # env values, the fix audit and saved snapshots: owner only


def _chmod(path, mode):
    """Best effort: a filesystem without POSIX modes (Windows, FAT) keeps its own."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def private_dir(path):
    """Create path (with parents) and keep it to the owner. Returns path."""
    os.makedirs(path, exist_ok=True)
    _chmod(path, DIR_MODE)
    return path


def private_open(path, mode="w", **kwargs):
    """open() a file that only the owner can read, whether it is new or already there."""
    handle = open(path, mode, opener=lambda name, flags: os.open(name, flags, FILE_MODE), **kwargs)
    _chmod(path, FILE_MODE)
    return handle


def state_path() -> Path:
    """Path(os.environ[STATE_ENV]) if that variable is set and non-empty, else Path.home() / ".stack-guardian" / "state.json"."""
    path = os.environ.get(STATE_ENV, "")
    if path:
        return Path(path)
    return Path.home() / ".stack-guardian" / "state.json"


def fresh() -> dict:
    """a NEW dict every call: {"schema": 1, "nodes": {}}"""
    return {"schema": 1, "nodes": {}}


def load(path=None) -> dict:
    """path or state_path(); json.loads of the file (utf-8). Missing file, unreadable file, invalid JSON, a non-dict,
    "schema" != 1, or "nodes" not a dict -> fresh(). Never raises. Anything but a missing file prints one stderr line."""
    if path is None:
        path = state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return fresh()
    except (OSError, TypeError, ValueError) as exc:
        return _discard(path, "%s: %s" % (type(exc).__name__, exc))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("nodes"), dict):
        return _discard(path, "not a schema 1 state")
    return data


def _discard(path, why):
    print("stack-guardian: state %s discarded (%s); starting fresh" % (path, why), file=sys.stderr)
    return fresh()


def save(state, path=None) -> None:
    """path or state_path(); create parent folders; write json.dumps(state, sort_keys=True) to a unique temp file
    beside it, then os.replace(tmp, path) -- concurrent saves never share a temp file and none is left behind."""
    if path is None:
        path = state_path()
    path = Path(path)
    private_dir(path.parent)  # mkstemp below already creates the file 0600
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        for attempt in range(20):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:  # Windows: another save is replacing the same file this instant
                if attempt == 19:
                    raise
                time.sleep(0.01)
    except Exception:
        # Clean up the temporary file if something goes wrong
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def record_run(state, node, findings, at) -> dict:
    """runs = state["nodes"][node]["runs"] (create the levels that are missing); append
    {"at": at, "codes": {f["code"]: f["level"] for each finding f whose f.get("node", "") is node or ""}}
    then keep only the last KEEP runs (oldest dropped). Mutates and returns `state`."""
    if node not in state["nodes"]:
        state["nodes"][node] = {"runs": []}
    runs = state["nodes"][node]["runs"]
    
    # Filter findings for this node
    node_findings = [f for f in findings if f.get("node", "") == node or f.get("node", "") == ""]
    
    # Create the run entry
    run_entry = {
        "at": at,
        "codes": {f["code"]: f["level"] for f in node_findings}
    }
    
    runs.append(run_entry)
    
    # Keep only the last KEEP runs
    if len(runs) > KEEP:
        runs[:] = runs[-KEEP:]
    
    return state


def streak(state, node, code) -> int:
    """how many of the MOST RECENT runs of `node`, counting back from the newest, have codes[code] in BAD; stop at the first
    run that does not. Unknown node -> 0."""
    if node not in state["nodes"]:
        return 0
    
    runs = state["nodes"][node]["runs"]
    count = 0
    
    # Iterate from newest to oldest
    for run in reversed(runs):
        if code in run["codes"] and run["codes"][code] in BAD:
            count += 1
        else:
            break
    
    return count


def clear_streak(state, node, code) -> int:
    """how many of the most recent runs, counting back from the newest, do NOT have codes[code] in BAD; stop at the first that does."""
    if node not in state["nodes"]:
        return 0
    
    runs = state["nodes"][node]["runs"]
    count = 0
    
    # Iterate from newest to oldest
    for run in reversed(runs):
        if code in run["codes"] and run["codes"][code] in BAD:
            break
        else:
            count += 1
    
    return count


def grade(state, node, finding) -> dict:
    """Returns a COPY (dict(finding)); never mutates the input. Only for level in BAD:
    streak(state, node, finding["code"]) >= 2 -> confidence "VERIFIED";
    otherwise a "VERIFIED" confidence becomes "LIKELY" (LIKELY and INSUFFICIENT stay).
    Call it AFTER record_run for the current run (the current run counts as one sample). Other levels: copy unchanged."""
    result = dict(finding)
    
    if finding["level"] in BAD:
        st = streak(state, node, finding["code"])
        if st >= 2:
            result["confidence"] = "VERIFIED"
        elif result["confidence"] == "VERIFIED":
            result["confidence"] = "LIKELY"
    
    return result


def due(state, node, code, fail_after=1) -> bool:
    """streak(state, node, code) >= fail_after"""
    return streak(state, node, code) >= fail_after


def _selftest():
    tmp = tempfile.mkdtemp()
    old_env = os.environ.get(STATE_ENV)
    try:
        def f(code, level="FAIL", node="gpu-box", confidence="VERIFIED"):
            return {"code": code, "level": level, "node": node, "confidence": confidence}

        def t_missing_file_fresh():
            return load(os.path.join(tmp, "absent.json")) == {"schema": 1, "nodes": {}}

        def t_corrupt_file_fresh():
            p = os.path.join(tmp, "bad.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            return load(p) == {"schema": 1, "nodes": {}}

        def t_save_load_roundtrip():
            p = os.path.join(tmp, "sub", "s.json")
            s = record_run(fresh(), "gpu-box", [f("x.y")], "2026-10-03T18:00:00")
            save(s, p)
            return load(p) == s and not os.path.exists(p + ".tmp")

        def t_keeps_last_5():
            s = fresh()
            for i in range(7):
                record_run(s, "gpu-box", [], "t%d" % i)
            runs = s["nodes"]["gpu-box"]["runs"]
            return len(runs) == KEEP and runs[0]["at"] == "t2" and runs[-1]["at"] == "t6"

        def t_other_node_ignored():
            s = record_run(fresh(), "gpu-box", [f("a.b", node="gpu-box-2"), f("c.d")], "t0")
            return s["nodes"]["gpu-box"]["runs"][0]["codes"] == {"c.d": "FAIL"}

        def t_streak_counts_recent_bad_runs():
            s = fresh()
            record_run(s, "gpu-box", [f("x.y")], "t0")
            record_run(s, "gpu-box", [], "t1")
            record_run(s, "gpu-box", [f("x.y", "WARN")], "t2")
            record_run(s, "gpu-box", [f("x.y")], "t3")
            return streak(s, "gpu-box", "x.y") == 2 and clear_streak(s, "gpu-box", "x.y") == 0

        def t_one_sample_is_likely_at_most():
            s = record_run(fresh(), "gpu-box", [f("x.y")], "t0")
            return grade(s, "gpu-box", f("x.y"))["confidence"] == "LIKELY"

        def t_two_samples_verified():
            s = fresh()
            record_run(s, "gpu-box", [f("x.y", confidence="LIKELY")], "t0")
            record_run(s, "gpu-box", [f("x.y", confidence="LIKELY")], "t1")
            return grade(s, "gpu-box", f("x.y", confidence="LIKELY"))["confidence"] == "VERIFIED"

        def t_ok_finding_untouched():
            s = record_run(fresh(), "gpu-box", [f("x.ok", "OK")], "t0")
            return grade(s, "gpu-box", f("x.ok", "OK"))["confidence"] == "VERIFIED"

        def t_due_respects_fail_after():
            s = fresh()
            record_run(s, "gpu-box", [f("x.y")], "t0")
            first = due(s, "gpu-box", "x.y", fail_after=2)
            record_run(s, "gpu-box", [f("x.y")], "t1")
            return first is False and due(s, "gpu-box", "x.y", fail_after=2) is True

        def t_state_dir_and_file_are_private():
            if sys.platform == "win32":  # no POSIX modes to assert
                return True
            p = os.path.join(tmp, "private", "s.json")
            save(record_run(fresh(), "gpu-box", [], "t0"), p)
            with private_open(os.path.join(tmp, "private", "env")) as fh:
                fh.write("X=1\n")
            return (os.stat(os.path.dirname(p)).st_mode & 0o777 == DIR_MODE
                    and os.stat(p).st_mode & 0o777 == FILE_MODE
                    and os.stat(os.path.join(tmp, "private", "env")).st_mode & 0o777 == FILE_MODE)

        def t_env_path():
            os.environ[STATE_ENV] = os.path.join(tmp, "env.json")
            return state_path() == Path(tmp) / "env.json"

        checks = [
            ("missing_file_fresh", t_missing_file_fresh),
            ("corrupt_file_fresh", t_corrupt_file_fresh),
            ("save_load_roundtrip", t_save_load_roundtrip),
            ("keeps_last_5", t_keeps_last_5),
            ("other_node_ignored", t_other_node_ignored),
            ("streak_counts_recent_bad_runs", t_streak_counts_recent_bad_runs),
            ("one_sample_is_likely_at_most", t_one_sample_is_likely_at_most),
            ("two_samples_verified", t_two_samples_verified),
            ("ok_finding_untouched", t_ok_finding_untouched),
            ("due_respects_fail_after", t_due_respects_fail_after),
            ("state_dir_and_file_are_private", t_state_dir_and_file_are_private),
            ("env_path", t_env_path),
        ]
        passed = 0
        for name, fn in checks:
            try:
                ok = bool(fn())
            except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
                ok = False
                name = "%s (raised %s: %s)" % (name, type(exc).__name__, exc)
            passed += ok
            print(("ok   " if ok else "FAIL ") + name)
        failed = len(checks) - passed
        print("sg_state selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
        return 0 if failed == 0 and checks else 1
    finally:
        if old_env is None:
            os.environ.pop(STATE_ENV, None)
        else:
            os.environ[STATE_ENV] = old_env

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(load(), indent=1))
