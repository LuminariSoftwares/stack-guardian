import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    from . import collect as sg_collect
    from . import core as sg_core
    from . import finding as sg_finding
    from . import fixes as sg_fixes
    from . import state as sg_state
except ImportError:  # run as a plain file next to the sg_* modules
    import sg_collect
    import sg_core
    import sg_finding
    import sg_fixes
    import sg_state

# The rules shipped with the package; --checks-dir points elsewhere.
DEFAULT_CHECKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checks.d")


USAGE = 3
CRASH = 4
NODES_ENV = "STACK_GUARDIAN_NODES"


def doctor(checks_dir, snapshot, state_path=None, at=None, ctx=None, record=True):
    """
    Run health checks against a snapshot and return graded findings and exit code.
    
    Args:
        checks_dir: Directory containing check modules
        snapshot: Dictionary with snapshot data
        state_path: Path to state file (optional)
        at: Timestamp for the run (optional)
        record: False grades this run against the saved history without saving it
        
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
    if record:
        sg_state.save(state, state_path)
    
    return graded, sg_finding.exit_code(graded)


def _state_dir(state_path):
    """Folder holding snapshots/, fixes.jsonl and env next to the state file."""
    return os.path.dirname(os.path.abspath(state_path)) if state_path else os.path.dirname(os.path.abspath(sg_state.state_path()))


def real_deps(snapshot, state_dir, audit_path=None, http_timeout=10, run_timeout=120):
    """The real dependencies fixes.apply_fix needs. Everything that touches the world lives here, in the CLI layer."""
    state_dir = os.fspath(state_dir)
    audit_path = audit_path or os.path.join(state_dir, "fixes.jsonl")

    def save_snapshot(obj):
        folder = os.path.join(state_dir, "snapshots")
        os.makedirs(folder, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1)
        return path

    def http(method, url, body):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:  # noqa: S310 - fixes.validate_fix only allows http(s)
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, OSError):
            return 0

    def run(argv):
        """(returncode, last 200 chars of stderr): 124 on a timeout, 127 when the program cannot be started."""
        try:  # argv is a list from a declared lane: never a shell string
            proc = subprocess.run(list(argv), capture_output=True, timeout=run_timeout, shell=False)  # noqa: S603
        except subprocess.TimeoutExpired as exc:
            return 124, _tail(exc.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, _tail(str(exc))
        return proc.returncode, _tail(proc.stderr)

    def set_env(name, value):
        """Persist NAME=value in <state dir>/env (replacing any earlier line) and set it for this process."""
        path = os.path.join(state_dir, "env")
        try:
            os.makedirs(state_dir, exist_ok=True)
            kept = []
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    kept = [ln for ln in fh.read().splitlines() if not ln.startswith(name + "=")]
            kept.append("%s=%s" % (name, value))
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(kept) + "\n")
        except OSError:
            return False
        os.environ[name] = value
        return True

    def audit(line):
        os.makedirs(os.path.dirname(os.path.abspath(audit_path)), exist_ok=True)
        with open(audit_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    return {"snapshot": lambda: snapshot, "save_snapshot": save_snapshot, "http": http, "run": run, "set_env": set_env,
            "audit": audit, "now": lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _tail(err, keep=200):
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    return (err or "").strip()[-keep:]


def pick_fix_finding(findings, ident):
    """(finding, error): ident is a finding code, or code@node when several nodes raise the same code."""
    code, _, node = ident.partition("@")
    hits = [f for f in findings if f.get("code") == code and (not node or f.get("node") == node)]
    if not hits:
        return None, "no finding %s in this run" % ident
    with_fix = [f for f in hits if f.get("fix")]
    if not with_fix:
        return None, "finding %s carries no fix" % ident
    if len(with_fix) > 1:
        return None, "finding %s is raised on several nodes (%s); use code@node" % (code, ", ".join(sorted(f.get("node", "") for f in with_fix)))
    return with_fix[0], ""


def _render_fix(ident, result):
    if not result["ok"] and not result.get("applied"):
        return "fix %s refused: %s" % (ident, result.get("error", "unknown"))
    if not result.get("applied"):
        return "fix %s (dry run, add --apply to perform): %s" % (ident, result["plan"]["describe"])
    return "fix %s %s: %s (snapshot %s)" % (ident, "applied" if result["ok"] else "FAILED", result["detail"], result["snapshot"])


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
    d.add_argument("--nodes", help="a nodes JSON file: collect a live snapshot instead of reading one "
                                   "(default $STACK_GUARDIAN_NODES when neither --snapshot nor --nodes is given)")
    d.add_argument("--ctx", help="a JSON file of rule settings (expect, thresholds)")
    d.add_argument("--state", help="state file (default $STACK_GUARDIAN_STATE or ~/.stack-guardian/state.json)")
    d.add_argument("--json", action="store_true")
    d.add_argument("--quiet", action="store_true")
    d.add_argument("--fix", metavar="ID", help="run the typed fix of finding ID (code or code@node); a dry run unless --apply")
    d.add_argument("--apply", action="store_true", help="with --fix: really perform it (snapshot first, one audit line after)")
    d.add_argument("--config", help="with --fix: JSON file {\"lanes\": {name: [argv...]}} declaring the lanes restart_lane may run")
    d.add_argument("--audit", help="with --fix --apply: audit file (default fixes.jsonl next to the state file)")
    s = sub.add_parser("snapshot")
    s.add_argument("--nodes", required=True)
    try:
        args = parser.parse_args(argv)
        if args.command not in ("doctor", "snapshot"):
            raise ValueError("a command is required: doctor | snapshot")
        if args.command == "doctor" and not (args.snapshot or args.nodes):
            args.nodes = os.environ.get(NODES_ENV) or None
        if args.command == "doctor" and bool(args.snapshot) == bool(args.nodes):
            raise ValueError("doctor needs exactly one of --snapshot FILE or --nodes FILE (or %s)" % NODES_ENV)
        if args.command == "doctor" and not args.fix and (args.apply or args.config or args.audit):
            raise ValueError("--apply, --config and --audit need --fix ID")
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
        if args.fix:
            target, why = pick_fix_finding(findings, args.fix)
            if target is None:
                print("stack-guardian: usage: %s" % why, file=sys.stderr)
                return USAGE
            deps = real_deps(snapshot, _state_dir(args.state), args.audit)
            result = sg_fixes.apply_fix(target["fix"], read(args.config) if args.config else {}, deps, apply=args.apply)
            print(json.dumps(result) if args.json else _render_fix(args.fix, result))
            return 0 if result["ok"] else 1
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
                     "        return F.make_finding('service.down', 'missing', 'FAIL', 'gpu-box-2', 'service down')\n"
                     "    return None\n"
                     "CHECKS = [('service.down', down)]\n")
        snap_down = os.path.join(tmp, "down.json")
        snap_up = os.path.join(tmp, "up.json")
        with open(snap_down, "w", encoding="utf-8") as fh:
            json.dump({"up": False, "nodes": {"gpu-box-2": {}}}, fh)
        with open(snap_up, "w", encoding="utf-8") as fh:
            json.dump({"up": True, "nodes": {"gpu-box-2": {}}}, fh)
        state = os.path.join(tmp, "state.json")

        def t_fail_exit_2_and_likely():
            f, code = doctor(checks, {"up": False, "nodes": {"gpu-box-2": {}}}, state, at="t1")
            return code == 2 and f[0]["confidence"] == "LIKELY"

        def t_second_run_verified():
            f, code = doctor(checks, {"up": False, "nodes": {"gpu-box-2": {}}}, state, at="t2")
            return code == 2 and f[0]["confidence"] == "VERIFIED"

        def t_ok_run_records_clean():
            f, code = doctor(checks, {"up": True, "nodes": {"gpu-box-2": {}}}, state, at="t3")
            return code == 0 and f == [] and sg_state.streak(sg_state.load(state), "gpu-box-2", "service.down") == 0

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
