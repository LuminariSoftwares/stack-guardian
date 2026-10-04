"""Core types for "stack-guardian", a doctor that health-checks an AI stack snapshot.

Pure and standalone: standard library only, no network, no persistent files.

Public contract:
    SEVERITIES / LEVELS / CONFIDENCES
    make_finding(code, severity, level, node, summary, evidence=None, try_=None,
                 confidence="LIKELY", fix=None) -> dict
    run_check(check_id, fn, snapshot, ctx, node="") -> dict or None
    worst(findings) -> str
    exit_code(findings) -> int

CLI:
    python sg_finding.py --worst PATH   # last stdout line is worst(...); rc is exit_code(...)
    python sg_finding.py --selftest      # counted checks; rc 0 only if all pass
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

SEVERITIES = ("ok", "partial", "missing", "crash", "n/a")
LEVELS = ("OK", "INFO", "WARN", "FAIL")  # ascending badness
CONFIDENCES = ("VERIFIED", "LIKELY", "INSUFFICIENT")

# Severities whose level is forced, regardless of the caller-supplied level.
_FORCED_LEVELS = {"ok": "OK", "n/a": "INFO", "crash": "WARN"}

SUMMARY_MAX = 120
CRASH_EVIDENCE_MAX = 300

FINDING_KEYS = (
    "code",
    "severity",
    "level",
    "node",
    "summary",
    "evidence",
    "try",
    "confidence",
    "fix",
)


def _valid_fix(fix):
    """True iff fix is a dict shaped {"action": str, "args": dict}."""
    if not isinstance(fix, dict):
        return False
    if "action" not in fix or "args" not in fix:
        return False
    return isinstance(fix["action"], str) and isinstance(fix["args"], dict)


def make_finding(
    code,
    severity,
    level,
    node,
    summary,
    evidence=None,
    try_=None,
    confidence="LIKELY",
    fix=None,
):
    """Build a finding dict with exactly the FINDING_KEYS keys.

    Validation of severity/level/confidence happens before any level override,
    so a bad enum always raises rather than being masked by the override.
    """
    if severity not in SEVERITIES:
        raise ValueError("unknown severity: %r" % (severity,))
    if level not in LEVELS:
        raise ValueError("unknown level: %r" % (level,))
    if confidence not in CONFIDENCES:
        raise ValueError("unknown confidence: %r" % (confidence,))
    if fix is not None and not _valid_fix(fix):
        raise ValueError("fix must be None or {'action': str, 'args': dict}")

    effective = _FORCED_LEVELS.get(severity, level)
    return {
        "code": str(code),
        "severity": severity,
        "level": effective,
        "node": str(node),
        "summary": str(summary)[:SUMMARY_MAX],
        "evidence": [str(x) for x in (evidence or [])],
        "try": [str(x) for x in (try_ or [])],
        "confidence": confidence,
        "fix": None if fix is None else dict(fix),
    }


def run_check(check_id, fn, snapshot, ctx, node=""):
    """Run fn(snapshot, ctx); convert any Exception into a crash finding.

    A buggy check never takes down the run and never reports FAIL: the crash
    finding is severity "crash", which forces level "WARN".
    """
    try:
        return fn(snapshot, ctx)
    except Exception as exc:  # noqa: BLE001 - a broken check must not abort the run
        # Coerce eagerly: reporting the crash must not raise a second time, or a
        # non-str check_id would take down the run the handler exists to protect.
        label = "%s" % (check_id,)
        return make_finding(
            "check.crashed",
            "crash",
            "WARN",
            node,
            "check %s raised %s" % (label, type(exc).__name__),
            evidence=[("%s: %s" % (type(exc).__name__, exc))[:CRASH_EVIDENCE_MAX]],
            try_=["fix or disable the check " + label],
            confidence="VERIFIED",
        )


def worst(findings):
    """Highest LEVELS entry among the findings' "level" values; "OK" if none."""
    best = "OK"
    rank = LEVELS.index(best)
    for finding in findings or ():
        level = finding.get("level") if isinstance(finding, dict) else None
        if level in LEVELS and LEVELS.index(level) > rank:
            best = level
            rank = LEVELS.index(level)
    return best


def exit_code(findings):
    """2 for FAIL, 1 for WARN, else 0."""
    top = worst(findings)
    if top == "FAIL":
        return 2
    if top == "WARN":
        return 1
    return 0


def load_findings(path):
    """Read a JSON file holding a list of findings (None entries allowed)."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("%s must hold a JSON list of findings" % (path,))
    return data


def main(argv=None):
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="sg_finding", description="Finding helpers for stack-guardian."
    )
    parser.add_argument("--worst", metavar="PATH", help="JSON file holding findings")
    parser.add_argument(
        "--selftest", action="store_true", help="run the in-file selftest"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.worst:
        try:
            findings = load_findings(args.worst)
        except (OSError, ValueError) as exc:
            sys.stderr.write("sg_finding: cannot read findings: %s\n" % (exc,))
            return 2
        top = worst(findings)
        sys.stdout.write(top + "\n")
        return exit_code(findings)
    parser.error("one of --worst PATH or --selftest is required")
    return 1  # unreachable; parser.error raises SystemExit


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def _expect_value_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError:
        return True
    except Exception:  # a different exception type is still a contract failure
        return False
    return False


def _raising_check(_snapshot, _ctx):
    raise ValueError("boom")


def _raising_long_check(_snapshot, _ctx):
    raise RuntimeError("x" * 500)


def check_constants():
    return (
        SEVERITIES == ("ok", "partial", "missing", "crash", "n/a")
        and LEVELS == ("OK", "INFO", "WARN", "FAIL")
        and CONFIDENCES == ("VERIFIED", "LIKELY", "INSUFFICIENT")
        and LEVELS.index("OK") < LEVELS.index("INFO") < LEVELS.index("WARN") < LEVELS.index("FAIL")
    )


def check_finding_keys():
    finding = make_finding("c", "partial", "WARN", "n1", "s")
    keys = set(finding)
    return (
        len(keys) == len(FINDING_KEYS)
        and keys == set(FINDING_KEYS)
        and tuple(finding) == FINDING_KEYS
        and isinstance(finding["evidence"], list)
        and isinstance(finding["try"], list)
        and finding["fix"] is None
        and finding["confidence"] == "LIKELY"
    )


def check_level_override():
    pairs = (
        ("ok", "FAIL", "OK"),
        ("n/a", "FAIL", "INFO"),
        ("crash", "FAIL", "WARN"),
        ("partial", "WARN", "WARN"),
        ("partial", "FAIL", "FAIL"),
        ("missing", "FAIL", "FAIL"),
        ("missing", "INFO", "INFO"),
    )
    for severity, given, expected in pairs:
        got = make_finding("c", severity, given, "n", "s")["level"]
        if got != expected:
            return False
    return True


def check_text_coercion():
    long_summary = "y" * 400
    finding = make_finding(
        "c",
        "missing",
        "WARN",
        "node-1",
        long_summary,
        evidence=[1, None, "e"],
        try_=[2.5, "z"],
    )
    coerced = make_finding("c", "ok", "OK", 7, 12345)
    return (
        len(finding["summary"]) == SUMMARY_MAX
        and finding["summary"] == "y" * SUMMARY_MAX
        and finding["summary"] == str(long_summary)[:SUMMARY_MAX]
        and finding["evidence"] == ["1", "None", "e"]
        and finding["try"] == ["2.5", "z"]
        and coerced["summary"] == "12345"
        and coerced["node"] == "7"
        and coerced["code"] == "c"
        and make_finding(99, "ok", "OK", "n", None)["code"] == "99"
        and make_finding("c", "ok", "OK", "n", None)["summary"] == "None"
    )


def check_bad_enum_raises():
    return (
        _expect_value_error(make_finding, "c", "nope", "WARN", "n", "s")
        and _expect_value_error(make_finding, "c", "ok", "nope", "n", "s")
        and _expect_value_error(
            make_finding, "c", "ok", "OK", "n", "s", confidence="MAYBE"
        )
        and _expect_value_error(make_finding, "c", "warn", "OK", "n", "s")
        and _expect_value_error(make_finding, "c", "OK", "OK", "n", "s")
    )


def check_enum_checked_before_override():
    # A bad level must raise even when severity would have overridden it.
    ok = _expect_value_error(make_finding, "c", "ok", "NOPE", "n", "s")
    bad_sev = _expect_value_error(make_finding, "c", "NOPE", "OK", "n", "s")
    return ok and bad_sev and SEVERITIES[0] == "ok"


def check_fix_validation():
    supplied = {"action": "install", "args": {"pkg": "x"}}
    good = make_finding("c", "partial", "WARN", "n", "s", fix=supplied)
    bad_values = (
        "install",
        ["install", {}],
        {"action": "install"},
        {"args": {}},
        {"action": 1, "args": {}},
        {"action": "install", "args": []},
        {},
    )
    return (
        good["fix"] == {"action": "install", "args": {"pkg": "x"}}
        and good["fix"] is not supplied
        and all(_expect_value_error(make_finding, "c", "ok", "OK", "n", "s", fix=b)
                for b in bad_values)
        and make_finding("c", "ok", "OK", "n", "s", fix=None)["fix"] is None
    )


def check_run_check_passthrough():
    snapshot = {"nodes": 1}
    ctx = {"deep": True}
    seen = []

    def ok_check(snap, c):
        seen.append((snap, c))
        return make_finding("ok", "ok", "OK", "", "fine")

    def none_check(snap, c):
        seen.append((snap, c))
        return None

    finding = run_check("ok", ok_check, snapshot, ctx)
    passed_none = run_check("none", none_check, snapshot, ctx)
    return (
        isinstance(finding, dict)
        and finding["code"] == "ok"
        and finding["node"] == ""
        and passed_none is None
        and len(seen) == 2
        and all(s is snapshot and c is ctx for s, c in seen)
    )


def check_run_check_crash():
    finding = run_check("c1", _raising_check, {}, {}, node="nodeA")
    evidence = "%s: %s" % ("ValueError", "boom")
    return (
        finding["code"] == "check.crashed"
        and finding["severity"] == "crash"
        and finding["level"] == "WARN"
        and finding["node"] == "nodeA"
        and finding["summary"] == "check c1 raised ValueError"
        and finding["evidence"] == [evidence]
        and finding["try"] == ["fix or disable the check c1"]
        and finding["confidence"] == "VERIFIED"
        and finding["fix"] is None
    )


def check_crash_never_fail_and_truncates():
    finding = run_check("c2", _raising_long_check, {}, {})
    msg = "%s: %s" % ("RuntimeError", "x" * 500)
    return (
        finding["level"] == "WARN"
        and finding["level"] != "FAIL"
        and exit_code([finding]) == 1
        and len(finding["evidence"]) == 1
        and len(finding["evidence"][0]) == CRASH_EVIDENCE_MAX
        and finding["evidence"][0] == msg[:CRASH_EVIDENCE_MAX]
        and finding["node"] == ""
    )


def check_run_check_survives_odd_ids():
    # Reporting the crash must never raise, whatever the id/node look like.
    findings = [
        run_check(7, _raising_check, {}, {}),
        run_check(None, _raising_check, {}, {}, node=None),
        run_check(("a", 1), _raising_check, {}, {}, node=("n", 2)),
        run_check("x", _raising_check, {}, {}, node=""),
    ]
    return (
        len(findings) == 4
        and all(isinstance(f, dict) for f in findings)
        and all(f["code"] == "check.crashed" for f in findings)
        and all(f["level"] == "WARN" for f in findings)
        and findings[0]["try"] == ["fix or disable the check 7"]
        and findings[0]["summary"] == "check 7 raised ValueError"
        and findings[1]["node"] == "None"
        and findings[2]["node"] == "('n', 2)"
        and all(exit_code([f]) == 1 for f in findings)
    )


def check_worst_basics():
    ok = make_finding("a", "ok", "OK", "", "s")
    warn = make_finding("b", "partial", "WARN", "", "s")
    fail = make_finding("c", "missing", "FAIL", "", "s")
    return (
        worst([ok]) == "OK"
        and worst([ok, warn]) == "WARN"
        and worst([warn, ok, fail]) == "FAIL"
        and worst([fail, warn]) == "FAIL"
        and worst([ok, ok]) == "OK"
    )


def check_worst_ignores_none():
    no_level = make_finding("a", "partial", "WARN", "", "s")
    no_level["level"] = None
    return (
        worst([]) == "OK"
        and worst(None) == "OK"
        and worst([None, None]) == "OK"
        and worst([None, no_level]) == "OK"
        and worst([None, no_level, {"level": "nonsense"}, "junk", make_finding("d", "missing", "FAIL", "", "s")]) == "FAIL"
    )


def check_exit_codes():
    ok = make_finding("a", "ok", "OK", "", "s")
    info = make_finding("b", "n/a", "FAIL", "", "s")
    warn = make_finding("c", "partial", "WARN", "", "s")
    fail = make_finding("d", "missing", "FAIL", "", "s")
    return (
        exit_code([]) == 0
        and exit_code([ok, info]) == 0
        and exit_code([ok, warn]) == 1
        and exit_code([warn, fail]) == 2
        and exit_code([None, ok]) == 0
        and exit_code([None, warn]) == 1
    )


def check_cli_roundtrip():
    findings = [
        make_finding("a", "ok", "OK", "", "healthy"),
        None,
        make_finding("b", "partial", "WARN", "n", "degraded"),
    ]
    workdir = tempfile.mkdtemp()
    try:
        path = os.path.join(workdir, "findings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(findings, handle)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--worst", path])
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        return (
            os.path.exists(path)
            and code == 1
            and len(lines) >= 1
            and lines[-1] == worst(findings)
            and lines[-1] == "WARN"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


SELFTEST_CHECKS = [
    ("constants", check_constants),
    ("finding_keys", check_finding_keys),
    ("level_override", check_level_override),
    ("text_coercion", check_text_coercion),
    ("bad_enum_raises", check_bad_enum_raises),
    ("enum_checked_before_override", check_enum_checked_before_override),
    ("fix_validation", check_fix_validation),
    ("run_check_passthrough", check_run_check_passthrough),
    ("run_check_crash", check_run_check_crash),
    ("crash_never_fail_and_truncates", check_crash_never_fail_and_truncates),
    ("run_check_survives_odd_ids", check_run_check_survives_odd_ids),
    ("worst_basics", check_worst_basics),
    ("worst_ignores_none", check_worst_ignores_none),
    ("exit_codes", check_exit_codes),
    ("cli_roundtrip", check_cli_roundtrip),
]


def selftest():
    """Run every named check; print the counted summary; return an exit code."""
    outcomes = []
    for _name, check in SELFTEST_CHECKS:
        try:
            outcomes.append(bool(check()))
        except Exception as exc:  # a broken check is a failure, not a crash
            sys.stderr.write("check %s errored: %s\n" % (_name, exc))
            outcomes.append(False)
    total = len(SELFTEST_CHECKS)
    passed = 0
    for outcome in outcomes:
        if outcome:
            passed += 1
    failed = total - passed
    sys.stdout.write(
        "sg_finding selftest: %d checks, %d passed, %d failed\n"
        % (total, passed, failed)
    )
    return 0 if failed == 0 and total > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))