"""stack-guardian core runner.

Loads the Python checks of a stack-guardian folder, runs them concurrently
against a snapshot dict and reports the findings it collects.
"""

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from . import finding as sg_finding
except ImportError:  # run as a plain file next to sg_finding.py
    import sg_finding

_LEVEL_RANK = {"FAIL": 0, "WARN": 1, "INFO": 2, "OK": 3}


def _broken_check(message):
    """Build a check function that raises RuntimeError(message) when run."""

    def _check(*_args, **_kwargs):
        raise RuntimeError(message)

    _check.__name__ = "broken_check"
    return _check


def load_checks(checks_dir):
    """Return the [(check_id, fn), ...] list published by checks_dir.

    Files are visited in file-name order; names starting with "_" are
    skipped. A file that cannot be imported, or that publishes no CHECKS
    list, contributes a single ("load.<stem>", fn) entry whose fn raises,
    so the doctor reports it as a crashed check instead of dying itself.
    """
    if not checks_dir or not os.path.isdir(checks_dir):
        return []
    try:
        names = sorted(os.listdir(checks_dir))
    except OSError:
        return []

    checks = []
    for position, name in enumerate(names):
        if name.startswith("_") or not name.endswith(".py"):
            continue
        path = os.path.join(checks_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            mod_name = "sg_check_%d_%s" % (position, name[:-3])
            spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            entries = getattr(module, "CHECKS", None)
            if not isinstance(entries, list):
                raise RuntimeError("no CHECKS list")
            for check_id, fn in entries:
                checks.append((check_id, fn))
        except Exception as exc:
            checks.append(("load." + name[:-3],
                           _broken_check("%s: %s" % (name, exc))))
    return checks


def run_all(checks, snapshot, ctx=None, max_workers=8):
    """Run every check through sg_finding.run_check, concurrently.

    Findings come back in the order of `checks`; None results are dropped.
    """
    checks = list(checks)
    if not checks:
        return []
    ctx = ctx or {}
    results = [None] * len(checks)

    def _work(index, check_id, fn):
        results[index] = sg_finding.run_check(check_id, fn, snapshot, ctx)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_work, index, check_id, fn)
                   for index, (check_id, fn) in enumerate(checks)]
        for future in futures:
            future.result()
    return [finding for finding in results if finding is not None]


def render(findings):
    """Render findings one per line, worst level first, plus a verdict."""
    findings = list(findings)
    if not findings:
        return "worst: OK"
    ordered = sorted(findings,
                     key=lambda f: _LEVEL_RANK.get(f.get("level", "OK"), 3))
    lines = []
    for finding in ordered:
        node = finding.get("node") or ""
        lines.append("%-4s %s%s: %s" % (finding.get("level", "OK"),
                                       finding.get("code", ""),
                                       " [" + node + "]" if node else "",
                                       finding.get("summary", "")))
    lines.append("worst: " + sg_finding.worst(findings))
    return "\n".join(lines)


@contextlib.contextmanager
def _scratch_dir():
    """A temporary directory that is always removed again."""
    path = tempfile.mkdtemp()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_check_file(directory, name, body):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        handle.write(body)


def _check_file(check_id):
    """Source of a check file that publishes a single no-op check."""
    return ('def _noop(*_args, **_kwargs):\n    return None\n\n'
            'CHECKS = [("%s", _noop)]\n' % check_id)


def _check_loads_in_file_order():
    with _scratch_dir() as scratch:
        _write_check_file(scratch, "b.py", _check_file("b1"))
        _write_check_file(scratch, "a.py", _check_file("a1"))
        return [check_id for check_id, _fn in load_checks(scratch)] == ["a1", "b1"]


def _check_underscore_skipped():
    with _scratch_dir() as scratch:
        _write_check_file(scratch, "_helper.py", _check_file("helper"))
        return load_checks(scratch) == []


def _check_broken_file_is_a_check():
    with _scratch_dir() as scratch:
        _write_check_file(scratch, "bad.py", "this is not python\n")
        checks = load_checks(scratch)
        if [check_id for check_id, _fn in checks] != ["load.bad"]:
            return False
        findings = run_all(checks, {})
        if len(findings) != 1:
            return False
        return (findings[0].get("code") == "check.crashed"
                and findings[0].get("level") == "WARN")


def _check_none_dropped():
    with _scratch_dir() as scratch:
        _write_check_file(scratch, "quiet.py", _check_file("quiet"))
        return run_all(load_checks(scratch), {}) == []


def _check_order_kept():
    def _ok(code):
        def _check(*_args, **_kwargs):
            return sg_finding.make_finding(code, "ok", "OK", "", code)
        return _check

    def _slow(*_args, **_kwargs):
        time.sleep(0.2)
        return sg_finding.make_finding("x", "ok", "OK", "", "x")

    checks = [("x", _slow), ("y", _ok("y")), ("z", _ok("z"))]
    findings = run_all(checks, {})
    return [finding.get("code") for finding in findings] == ["x", "y", "z"]


def _check_render_sorted():
    findings = [sg_finding.make_finding("ok_code", "ok", "OK", "", "ok summary"),
                sg_finding.make_finding("bad_code", "missing", "FAIL", "web",
                                        "fail summary")]
    lines = render(findings).split("\n")
    return lines[0].startswith("FAIL")


def _check_render_empty():
    return render([]) == "worst: OK"


def _check_missing_dir():
    with _scratch_dir() as scratch:
        return load_checks(os.path.join(scratch, "absent")) == []


_SELFTESTS = [
    ("loads_in_file_order", _check_loads_in_file_order),
    ("underscore_skipped", _check_underscore_skipped),
    ("broken_file_is_a_check", _check_broken_file_is_a_check),
    ("none_dropped", _check_none_dropped),
    ("order_kept", _check_order_kept),
    ("render_sorted", _check_render_sorted),
    ("render_empty", _check_render_empty),
    ("missing_dir", _check_missing_dir),
]


def run_selftest():
    """Run the built-in checks, print the tally and return an exit code."""
    total = len(_SELFTESTS)
    passed = 0
    failed = 0
    for name, check in _SELFTESTS:
        detail = ""
        try:
            ok = bool(check())
        except Exception as exc:
            ok = False
            detail = "%s: %s" % (type(exc).__name__, exc)
        if ok:
            passed += 1
        else:
            failed += 1
            print("FAIL %s%s" % (name, ": " + detail if detail else ""))
    print("sg_core selftest: %d checks, %d passed, %d failed"
          % (total, passed, failed))
    return 0 if failed == 0 and total > 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sg_core",
                                     description="stack-guardian health doctor")
    parser.add_argument("--checks-dir", help="folder holding the check modules")
    parser.add_argument("--snapshot", help="JSON file holding the snapshot")
    parser.add_argument("--json", action="store_true",
                        help="print the findings as one JSON line")
    parser.add_argument("--selftest", action="store_true",
                        help="run the built-in checks and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.checks_dir or not args.snapshot:
        parser.error("--checks-dir and --snapshot are required")

    with open(args.snapshot, "r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    findings = run_all(load_checks(args.checks_dir), snapshot)
    if args.json:
        print(json.dumps(findings))
    else:
        print(render(findings))
    return sg_finding.exit_code(findings)


if __name__ == "__main__":
    sys.exit(main())
