import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    from . import collect as sg_collect
    from . import core as sg_core
    from . import finding as sg_finding
    from . import fixes as sg_fixes
    from . import nodes as sg_nodes
    from . import state as sg_state
    from . import __version__
except ImportError:  # run as a plain file next to the sg_* modules
    import sg_collect
    import sg_core
    import sg_finding
    import sg_fixes
    import sg_nodes
    import sg_state
    __version__ = "unknown"  # only when the modules are run as plain files, outside the installed package

# The rules shipped with the package; --checks-dir points elsewhere.
DEFAULT_CHECKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checks.d")


USAGE = 3
CRASH = 4
SCHEMA = 1  # the shape of `doctor --json`; bumped only if a field changes meaning
NODES_ENV = "STACK_GUARDIAN_NODES"
EPILOG = """one more command:
  mcp       start the MCP server over stdio, the same as stack-guardian-mcp:
            stack-guardian mcp --nodes FILE [--ctx FILE] [--state FILE] [--ttl SECONDS]
            It is handled before the options above, so it takes only its own flags.

stack-guardian COMMAND --help for a command's options.
Exit codes: 0 all OK/INFO, 1 a WARN, 2 a FAIL, 3 usage, 4 the doctor itself crashed."""


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


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def watch_changes(before, after):
    """Changes from one run's findings to the next: resolved first, then new and severity changes in rule order."""
    def by_key(findings):
        return {(f.get("code"), f.get("node", "")): f for f in findings}
    old, new = by_key(before), by_key(after)
    changes = [("resolved", f, None) for key, f in old.items() if key not in new]
    for key, f in new.items():
        if key not in old:
            changes.append(("new", f, None))
        elif old[key].get("level") != f.get("level"):
            changes.append(("changed", f, old[key]))
    return changes


def watch(run_once, interval, sleep=time.sleep, out=None, err=None, now=_utcnow, as_json=False):
    """Run run_once() every interval seconds, printing only the findings that changed state. Ctrl+C exits 0
    with a one-line summary (on err in JSON mode, so stdout stays one JSON object per change)."""
    out = out or (lambda line: print(line, flush=True))  # flushed: a watch is often piped into a log
    err = err or (lambda line: print(line, file=sys.stderr, flush=True))
    findings, runs, changes = [], 0, 0
    try:
        while True:
            try:
                current = run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad run is reported, the watch goes on
                err("%s crash: %s: %s" % (now(), type(exc).__name__, exc))
                current = findings
            else:
                runs += 1
                at = now()
                for change, f, was in watch_changes(findings, current):
                    changes += 1
                    ident = "%s@%s" % (f.get("code"), f.get("node", ""))
                    if as_json:
                        level = None if change == "resolved" else f.get("level")
                        before = f.get("level") if change == "resolved" else (was or {}).get("level")
                        out(json.dumps({"at": at, "change": "severity" if change == "changed" else change,
                                        "code": f.get("code"), "node": f.get("node", ""), "level": level,
                                        "was": before, "summary": f.get("summary")}))
                    elif change == "resolved":
                        out("%s resolved %s (was %s)" % (at, ident, f.get("level")))
                    elif change == "changed":
                        out("%s changed %s->%s %s: %s" % (at, was.get("level"), f.get("level"), ident, f.get("summary")))
                    else:
                        out("%s new %s %s: %s" % (at, f.get("level"), ident, f.get("summary")))
            findings = current
            sleep(interval)
    except KeyboardInterrupt:
        summary = "watch stopped: %d runs, %d changes, worst now %s" % (runs, changes, sg_finding.worst(findings))
        (err if as_json else out)(summary)
    return 0


def _live(nodes, ttft=False, timeout_s=None):
    """A live snapshot under one wall-clock deadline; with ttft it grows to cover the 1-token generation."""
    return sg_collect.snapshot(nodes, ttft=ttft, timeout_s=sg_nodes.timeout_s({}, timeout_s, ttft=ttft))


def _state_dir(state_path):
    """Folder holding snapshots/, fixes.jsonl and env next to the state file."""
    return os.path.dirname(os.path.abspath(state_path)) if state_path else os.path.dirname(os.path.abspath(sg_state.state_path()))


def real_deps(snapshot, state_dir, audit_path=None, http_timeout=10, run_timeout=120):
    """The real dependencies fixes.apply_fix needs. Everything that touches the world lives here, in the CLI layer."""
    state_dir = os.fspath(state_dir)
    audit_path = audit_path or os.path.join(state_dir, "fixes.jsonl")

    def save_snapshot(obj):
        folder = sg_state.private_dir(os.path.join(state_dir, "snapshots"))
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        path = os.path.join(folder, name)
        with sg_state.private_open(path, encoding="utf-8") as fh:  # a snapshot describes your machines: owner only
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
            sg_state.private_dir(state_dir)
            kept = []
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    kept = [ln for ln in fh.read().splitlines() if not ln.startswith(name + "=")]
            kept.append("%s=%s" % (name, value))
            with sg_state.private_open(path, encoding="utf-8", newline="\n") as fh:  # it holds secrets: owner only
                fh.write("\n".join(kept) + "\n")
        except OSError:
            return False
        os.environ[name] = value
        return True

    def audit(line):
        sg_state.private_dir(os.path.dirname(os.path.abspath(audit_path)))
        with sg_state.private_open(audit_path, "a", encoding="utf-8", newline="\n") as fh:
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


def _plan_detail(plan):
    """What the fix would actually do: the argv it would run, the request it would send, the variable it would set."""
    if plan.get("argv"):
        return "would run: %s" % " ".join(plan["argv"])
    if plan.get("http"):
        http = plan["http"]
        return "would send: %s %s %s" % (http.get("method"), http.get("url"), json.dumps(http.get("body")))
    if plan.get("env"):
        return "would set: %s (value not shown)" % ", ".join(sorted(plan["env"]))
    return ""


def _render_fix(ident, result):
    if not result["ok"] and not result.get("applied"):
        return "fix %s refused: %s" % (ident, result.get("error", "unknown"))
    if not result.get("applied"):
        detail = _plan_detail(result["plan"])
        return "fix %s (dry run, add --apply to perform): %s%s" % (ident, result["plan"]["describe"],
                                                                   "\n  " + detail if detail else "")
    return "fix %s %s: %s (snapshot %s)" % (ident, "applied" if result["ok"] else "FAILED", result["detail"], result["snapshot"])


LOCAL_OLLAMA = "http://127.0.0.1:11434"


def _peers(run=None, which=None):
    """Tailnet peer host names this machine can see, or [] when tailscale is not here to ask."""
    run = run or sg_collect.default_run
    which = which or shutil.which
    if which("tailscale") is None:
        return []
    out = run(sg_collect.TAILSCALE_STATUS)
    try:
        peers = (json.loads(out or "") or {}).get("Peer") or {}
    except ValueError:
        return []
    names = [p.get("HostName") for p in peers.values() if isinstance(p, dict) and p.get("HostName")]
    return sorted(set(names))


def starter_nodes(ollama_url=LOCAL_OLLAMA, answered=False, peers=()):
    """The starter nodes file: this machine, plus any tailnet peers as candidates under an ignored "_" key."""
    note = ("%s answered, so this node is ready to check." % ollama_url if answered else
            "Nothing answered at %s. Start Ollama, or change the URL to the machine that runs it." % ollama_url)
    doc = {"_README": ["Written by stack-guardian init. Every key starting with _ is ignored, so these lines and the",
                       "candidates below are comments. A node takes \"ollama\": [URLs], \"local\": true to read this",
                       "machine's GPUs with nvidia-smi, and \"tailnet_host\": \"NAME\" to look the peer up in tailscale.",
                       "An LM Studio / vLLM / llama.cpp node is {\"kind\": \"openai\", \"endpoints\": [URLs]}.",
                       note],
           "_options": {"timeout_s": 10},
           "this-machine": {"ollama": [ollama_url], "local": True}}
    if peers:
        doc["_tailnet_candidates"] = {
            "Move a peer up into this file (without the leading _) to check it": True,
            "peers": {name: {"ollama": ["http://%s:11434" % name], "tailnet_host": name} for name in peers},
        }
    return doc


def init_nodes(path, ollama_url=LOCAL_OLLAMA, http_get=None, run=None, which=None, out=print, err=None):
    """Write a starter nodes file. Never overwrites: an existing file is a usage error, so nothing is ever lost."""
    err = err or (lambda line: print(line, file=sys.stderr))
    http_get = http_get or sg_collect.default_http_get
    if os.path.exists(path):
        err("stack-guardian: usage: %s already exists; delete it or pass --out OTHER.json" % path)
        return USAGE
    try:
        status, _body = http_get(ollama_url.rstrip("/") + "/api/tags", timeout=2)
        answered = status == 200
    except Exception:  # noqa: BLE001 - nothing listening is the common case, not an error
        answered = False
    peers = _peers(run=run, which=which)
    doc = starter_nodes(ollama_url, answered, peers)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        err("stack-guardian: usage: cannot write %s: %s" % (path, exc))
        return USAGE
    out("wrote %s" % path)
    out("  %s at %s" % ("Ollama answered" if answered else "nothing answered yet", ollama_url))
    out("  %d tailnet peer(s) listed as candidates" % len(peers) if peers
        else "  no tailnet peers listed (tailscale is not on PATH, or it sees none)")
    out("next: stack-guardian doctor --nodes %s" % path)
    return 0


def main(argv=None):
    """stack-guardian doctor | snapshot | init | mcp. Exit: 0 OK/INFO, 1 WARN, 2 FAIL, 3 usage, 4 crash in the doctor."""
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["mcp"]:  # `stack-guardian mcp` = stack-guardian-mcp, so `uvx stack-guardian mcp` works (MCP Registry)
        from stack_guardian import mcp_server
        return mcp_server.main(argv[1:])
    if "--version" in argv:
        print("stack-guardian %s" % __version__)
        return 0

    class _Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError(message)

    parser = _Parser(prog="stack-guardian", description="A health doctor for a home AI stack: Ollama and "
                                                        "OpenAI-compatible nodes, NVIDIA GPUs and tailnet peers.",
                     epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    d = sub.add_parser("doctor", help="run the health rules and grade the findings")
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
    d.add_argument("--ttft", action="store_true", help="with --nodes: also time a 1-token generation on each loaded "
                                                       "Ollama model (costs a generation; never on by default)")
    d.add_argument("--timeout", type=float, metavar="SECONDS", help="with --nodes: wall-clock deadline for the whole "
                                                                    "collection (default 10, 30 with --ttft, or "
                                                                    "\"_options\": {\"timeout_s\": N} in the nodes file)")
    d.add_argument("--watch", type=float, metavar="N", help="re-run every N seconds, printing only findings that changed state; Ctrl+C stops")
    s = sub.add_parser("snapshot", help="print the raw snapshot the rules read")
    s.add_argument("--nodes", required=True)
    s.add_argument("--ttft", action="store_true", help="also time a 1-token generation on each loaded Ollama model")
    s.add_argument("--timeout", type=float, metavar="SECONDS", help="wall-clock deadline for the whole collection")
    i = sub.add_parser("init", help="write a starter nodes file for this machine")
    i.add_argument("--out", default="nodes.json", help="where to write it (default nodes.json; never overwritten)")
    i.add_argument("--ollama", default=LOCAL_OLLAMA, help="the Ollama base URL to probe (default %s)" % LOCAL_OLLAMA)
    try:
        args = parser.parse_args(argv)
        if args.command not in ("doctor", "snapshot", "init"):
            raise ValueError("a command is required: doctor | snapshot | init | mcp (--help lists them)")
        if args.command == "doctor":
            if not (args.snapshot or args.nodes):
                args.nodes = os.environ.get(NODES_ENV) or None
            if bool(args.snapshot) == bool(args.nodes):
                raise ValueError("doctor needs exactly one of --snapshot FILE or --nodes FILE (or %s)" % NODES_ENV)
            if not args.fix and (args.apply or args.config or args.audit):
                raise ValueError("--apply, --config and --audit need --fix ID")
            if args.ttft and not args.nodes:
                raise ValueError("--ttft probes live nodes: it needs --nodes FILE (or %s), not --snapshot" % NODES_ENV)
            if args.timeout is not None and (args.timeout <= 0 or not args.nodes):
                raise ValueError("--timeout needs SECONDS > 0 and --nodes FILE (or %s): a saved snapshot is not collected" % NODES_ENV)
            if args.watch is not None and (args.watch <= 0 or args.fix):
                raise ValueError("--watch needs N > 0 seconds and cannot be combined with --fix")
        if args.command == "snapshot" and args.timeout is not None and args.timeout <= 0:
            raise ValueError("--timeout needs SECONDS > 0")
    except ValueError as exc:
        print("stack-guardian: usage: %s" % exc, file=sys.stderr)
        return USAGE
    except SystemExit as exc:  # --help / -h on a subparser
        return exc.code or 0
    if args.command == "init":
        return init_nodes(args.out, args.ollama)
    try:
        def read(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)

        def read_nodes(path):
            """(nodes, deadline): a nodes file that cannot be used is a usage error, not a wrong answer."""
            parsed, options = sg_nodes.load(path)
            return parsed, sg_nodes.timeout_s(options, args.timeout, ttft=args.ttft)

        if args.command == "snapshot":
            nodes, deadline = read_nodes(args.nodes)
            print(json.dumps(_live(nodes, args.ttft, deadline), indent=1))
            return 0
        ctx = read(args.ctx) if args.ctx else {}
        if args.watch:
            def run_once():
                if args.snapshot:
                    snap = read(args.snapshot)
                else:
                    nodes, deadline = read_nodes(args.nodes)
                    snap = _live(nodes, args.ttft, deadline)
                return doctor(args.checks_dir, snap, args.state, ctx=ctx)[0]
            return watch(run_once, args.watch, as_json=args.json)
        if args.snapshot:
            snapshot = read(args.snapshot)
        else:
            nodes, deadline = read_nodes(args.nodes)
            snapshot = _live(nodes, args.ttft, deadline)
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
    except sg_nodes.NodesError as exc:  # a nodes file this tool cannot read is the user's to fix, not a crash
        print("stack-guardian: usage: %s: %s" % (args.nodes, exc), file=sys.stderr)
        return USAGE
    except Exception as exc:  # noqa: BLE001 - one line, no traceback: the doctor never crashes the shell
        print("stack-guardian: crash: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return CRASH
    if args.json:
        print(json.dumps({"schema": SCHEMA, "findings": findings, "worst": sg_finding.worst(findings)}))
    elif not (args.quiet and code == 0):
        print(sg_core.render(findings))
        unconfirmed = sg_core.unconfirmed(findings)
        if unconfirmed:
            print("stack-guardian: %d finding(s) are LIKELY on one run: run again to confirm them as VERIFIED"
                  % len(unconfirmed), file=sys.stderr)
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

        def t_version_is_0_and_help_is_0():
            return main(["--version"]) == 0 and main(["--help"]) == 0

        def t_bad_nodes_file_is_usage_3():
            bad = os.path.join(tmp, "bad-nodes.json")
            with open(bad, "w", encoding="utf-8") as fh:
                json.dump([{"gpu-box": {"ollama": ["http://127.0.0.1:1"]}}], fh)
            return main(["doctor", "--checks-dir", checks, "--nodes", bad, "--state", state]) == USAGE

        def t_init_writes_once():
            path = os.path.join(tmp, "init-nodes.json")
            first = init_nodes(path, ollama_url="http://127.0.0.1:1", out=lambda _line: None,
                               err=lambda _line: None, which=lambda _p: None)
            second = init_nodes(path, ollama_url="http://127.0.0.1:1", out=lambda _line: None,
                                err=lambda _line: None, which=lambda _p: None)
            with open(path, encoding="utf-8") as fh:
                written = json.load(fh)
            nodes, _options = sg_nodes.validate(written, warn=lambda _line: None)
            return first == 0 and second == USAGE and list(nodes) == ["this-machine"]

        tests = [
            ("fail_exit_2_and_likely", t_fail_exit_2_and_likely),
            ("second_run_verified", t_second_run_verified),
            ("ok_run_records_clean", t_ok_run_records_clean),
            ("main_json", t_main_json),
            ("main_usage_3", t_main_usage_3),
            ("main_crash_4", t_main_crash_4),
            ("version_is_0_and_help_is_0", t_version_is_0_and_help_is_0),
            ("bad_nodes_file_is_usage_3", t_bad_nodes_file_is_usage_3),
            ("init_writes_once", t_init_writes_once),
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
