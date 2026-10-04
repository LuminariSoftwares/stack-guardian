import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

try:
    from . import collect as sg_collect
    from . import core as sg_core
    from . import finding as sg_finding
    from . import state as sg_state
except ImportError:  # run as a plain file next to the sg_* modules
    import sg_collect
    import sg_core
    import sg_finding
    import sg_state

# The rules shipped with the package (P43 S3); --checks-dir points elsewhere.
DEFAULT_CHECKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checks.d")


USAGE = 3
CRASH = 4


def doctor(checks_dir, snapshot, state_path=None, at=None, ctx=None):
    """
    Run health checks against a snapshot and return graded findings and exit code.
    
    Args:
        checks_dir: Directory containing check modules
        snapshot: Dictionary with snapshot data
        state_path: Path to state file (optional)
        at: Timestamp for the run (optional)
        
    Returns:
        tuple: (graded_findings, exit_code)
    """
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    checks = sg_core.load_checks(checks_dir)
    findings = sg_core.run_all(checks, snapshot, ctx or {})
    
    state = sg_state.load(state_path)
    nodes = set((snapshot.get("nodes") or {}).keys()) | {f.get("node", "") for f in findings}
    
    for node in sorted(nodes):
        sg_state.record_run(state, node, findings, at)
        
    graded = [sg_state.grade(state, f.get("node", ""), f) for f in findings]
    sg_state.save(state, state_path)
    
    return graded, sg_finding.exit_code(graded)


def main(argv=None):
    """stack-guardian doctor | snapshot. Exit: 0 OK/INFO, 1 WARN, 2 FAIL, 3 usage, 4 crash in the doctor itself."""
    if argv is None:
        argv = sys.argv[1:]

    class _Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError(message)

    parser = _Parser(prog="stack-guardian")
    sub = parser.add_subparsers(dest="command")
    d = sub.add_parser("doctor")
    d.add_argument("--checks-dir", default=DEFAULT_CHECKS_DIR, help="folder of check files (default: the shipped rules)")
    d.add_argument("--snapshot", help="a snapshot JSON file")
    d.add_argument("--nodes", help="a nodes JSON file: collect a live snapshot instead of reading one")
    d.add_argument("--ctx", help="a JSON file of rule settings (expect, thresholds)")
    d.add_argument("--state", help="state file (default $STACK_GUARDIAN_STATE or ~/.stack-guardian/state.json)")
    d.add_argument("--json", action="store_true")
    d.add_argument("--quiet", action="store_true")
    s = sub.add_parser("snapshot")
    s.add_argument("--nodes", required=True)
    try:
        args = parser.parse_args(argv)
        if args.command not in ("doctor", "snapshot"):
            raise ValueError("a command is required: doctor | snapshot")
        if args.command == "doctor" and bool(args.snapshot) == bool(args.nodes):
            raise ValueError("doctor needs exactly one of --snapshot FILE or --nodes FILE")
    except ValueError as exc:
        print("stack-guardian: usage: %s" % exc, file=sys.stderr)
        return USAGE
    try:
        def read(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        if args.command == "snapshot":
            print(json.dumps(sg_collect.snapshot(read(args.nodes)), indent=1))
            return 0
        snapshot = read(args.snapshot) if args.snapshot else sg_collect.snapshot(read(args.nodes))
        ctx = read(args.ctx) if args.ctx else {}
        findings, code = doctor(args.checks_dir, snapshot, args.state, ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - one line, no traceback: the doctor never crashes the shell
        print("stack-guardian: crash: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return CRASH
    if args.json:
        print(json.dumps({"findings": findings, "worst": sg_finding.worst(findings)}))
    elif not (args.quiet and code == 0):
        print(sg_core.render(findings))
    return code


def _selftest():
    tmp = tempfile.mkdtemp()
    try:
        checks = os.path.join(tmp, "checks")
        os.mkdir(checks)
        with open(os.path.join(checks, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("import sg_finding as F\n"
                     "def down(s, c):\n"
                     "    if not s['up']:\n"
                     "        return F.make_finding('svc.down', 'missing', 'FAIL', 'node1', 'service down')\n"
                     "    return None\n"
                     "CHECKS = [('svc.down', down)]\n")
        snap_down = os.path.join(tmp, "down.json")
        snap_up = os.path.join(tmp, "up.json")
        with open(snap_down, "w", encoding="utf-8") as fh:
            json.dump({"up": False, "nodes": {"node1": {}}}, fh)
        with open(snap_up, "w", encoding="utf-8") as fh:
            json.dump({"up": True, "nodes": {"node1": {}}}, fh)
        state = os.path.join(tmp, "state.json")

        def t_fail_exit_2_and_likely():
            f, code = doctor(checks, {"up": False, "nodes": {"node1": {}}}, state, at="t1")
            return code == 2 and f[0]["confidence"] == "LIKELY"

        def t_second_run_verified():
            f, code = doctor(checks, {"up": False, "nodes": {"node1": {}}}, state, at="t2")
            return code == 2 and f[0]["confidence"] == "VERIFIED"

        def t_ok_run_records_clean():
            f, code = doctor(checks, {"up": True, "nodes": {"node1": {}}}, state, at="t3")
            return code == 0 and f == [] and sg_state.streak(sg_state.load(state), "node1", "svc.down") == 0

        def t_main_json():
            return main(["doctor", "--checks-dir", checks, "--snapshot", snap_up, "--state", state, "--json"]) == 0

        def t_main_usage_3():
            return main(["doctor", "--checks-dir", checks]) == USAGE and main([]) == USAGE

        def t_main_crash_4():
            return main(["doctor", "--checks-dir", checks, "--snapshot", os.path.join(tmp, "absent.json"), "--state", state]) == CRASH

        tests = [
            ("fail_exit_2_and_likely", t_fail_exit_2_and_likely),
            ("second_run_verified", t_second_run_verified),
            ("ok_run_records_clean", t_ok_run_records_clean),
            ("main_json", t_main_json),
            ("main_usage_3", t_main_usage_3),
            ("main_crash_4", t_main_crash_4),
        ]
        passed = 0
        for name, fn in tests:
            try:
                ok = bool(fn())
            except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
                ok = False
                name = "%s (raised %s: %s)" % (name, type(exc).__name__, exc)
            passed += ok
            print(("ok   " if ok else "FAIL ") + name)
        failed = len(tests) - passed
        print("sg_cli selftest: %d checks, %d passed, %d failed" % (len(tests), passed, failed))
        return 0 if failed == 0 and tests else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
