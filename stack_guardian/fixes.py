"""stack_guardian.fixes -- typed, audited fixes (the finding "fix" field).

A finding may carry fix={"action": <id>, "args": {...}}. Only three typed actions exist; there is never a
raw shell string. Nothing changes without apply=True: a snapshot is taken first, the action runs through an
injected dependency, and one JSON audit line is written after."""

import argparse
import json
import re
import sys
import time

ACTIONS = ("unload_model", "restart_lane", "set_env")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_ENV_VALUE = 4096


def _str_arg(args, key):
    """Return the stripped string value of args[key], or "" when it is not a usable string."""
    val = args.get(key)
    return val.strip() if isinstance(val, str) else ""


def _declared_lane(config):
    """Lanes table from config, or an empty dict when absent/unusable."""
    if not isinstance(config, dict):
        return {}
    lanes = config.get("lanes") or {}
    return lanes if isinstance(lanes, dict) else {}


def validate_fix(fix, config):
    """Return "" when fix is a valid typed fix, else the refusal reason."""
    if not isinstance(fix, dict) or not isinstance(fix.get("action"), str) or not isinstance(fix.get("args"), dict):
        return "fix must be {'action': str, 'args': dict}"
    action = fix["action"]
    args = fix["args"]
    if action not in ACTIONS:
        return "unknown action: %s" % action

    if action == "unload_model":
        for key in ("node", "url", "model"):
            if not _str_arg(args, key):
                return "unload_model needs a non-empty %s" % key
        url = _str_arg(args, "url")
        if not (url.startswith("http://") or url.startswith("https://")):
            return "unload_model url must be http(s)"
        return ""

    if action == "restart_lane":
        lane = args.get("lane")
        if not isinstance(lane, str) or lane not in _declared_lane(config):
            return "restart_lane: lane is not declared in config lanes"
        cmd = _declared_lane(config)[lane]
        if (not isinstance(cmd, list) or not cmd
                or any(not isinstance(part, str) or not part.strip() for part in cmd)):
            return "restart_lane: the declared command must be a non-empty list of strings"
        return ""

    name = args.get("name")
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        return "set_env: name must match [A-Z_][A-Z0-9_]*"
    value = args.get("value")
    if (not isinstance(value, str) or len(value) > MAX_ENV_VALUE
            or "\n" in value or "\r" in value):
        return "set_env: value must be a single-line string of at most %d chars" % MAX_ENV_VALUE
    return ""


def plan_fix(fix, config):
    """Return the typed plan for a validated fix; raise ValueError with the refusal reason otherwise."""
    reason = validate_fix(fix, config)
    if reason:
        raise ValueError(reason)
    action = fix["action"]
    args = fix["args"]

    if action == "unload_model":
        url = _str_arg(args, "url").rstrip("/")
        return {"action": action,
                "describe": "unload %s on %s" % (_str_arg(args, "model"), _str_arg(args, "node")),
                "argv": None,
                "http": {"method": "POST", "url": url + "/api/generate",
                         "body": {"model": _str_arg(args, "model"), "keep_alive": 0}},
                "env": None}

    if action == "restart_lane":
        lane = args["lane"]
        return {"action": action,
                "describe": "restart lane %s" % lane,
                "argv": list(_declared_lane(config)[lane]),
                "http": None,
                "env": None}

    return {"action": action,
            "describe": "set %s" % args["name"],
            "argv": None,
            "http": None,
            "env": {args["name"]: args["value"]}}


def _audit_args(fix):
    """Args as they appear in the audit line: a set_env value is never written, only its length."""
    args = fix.get("args") if isinstance(fix, dict) else None
    if not isinstance(args, dict):
        return args
    if fix.get("action") == "set_env":
        value = args.get("value")
        return {"name": args.get("name"), "value_len": len(value) if isinstance(value, str) else 0}
    return args


def audit_line(ts, fix, ok, detail, snapshot_ref):
    """One single-line JSON audit record for an applied fix."""
    action = fix.get("action") if isinstance(fix, dict) else None
    return json.dumps({"ts": ts, "action": action, "args": _audit_args(fix),
                       "ok": bool(ok), "detail": detail, "snapshot": snapshot_ref}, sort_keys=True)


def apply_fix(fix, config, deps, apply=False):
    """Validate, then plan (default) or apply a fix through injected dependencies."""
    reason = validate_fix(fix, config)
    if reason:
        return {"ok": False, "applied": False, "error": reason}
    plan = plan_fix(fix, config)
    if not apply:
        return {"ok": True, "applied": False, "plan": plan}

    ref = deps["save_snapshot"](deps["snapshot"]())
    if plan["http"] is not None:
        req = plan["http"]
        status = deps["http"](req["method"], req["url"], req["body"])
        ok = status == 200
        detail = "http %s" % status
    elif plan["argv"] is not None:
        out = deps["run"](plan["argv"])
        rc, err = out if isinstance(out, tuple) else (out, "")
        ok = rc == 0
        detail = "rc %s" % rc + ("; stderr: %s" % err if err else "")
    else:
        name, value = fix["args"]["name"], fix["args"]["value"]
        ok = bool(deps["set_env"](name, value))
        detail = "set" if ok else "not set"

    deps["audit"](audit_line(deps["now"](), fix, ok, detail, ref))
    return {"ok": ok, "applied": True, "plan": plan, "detail": detail, "snapshot": ref}


def _selftest():
    cfg = {"lanes": {"cpu_worker": ["service", "restart", "cpu_worker"], "bad": [], "worse": "restart it"}}
    um = {"action": "unload_model", "args": {"node": "gpu-box", "url": "http://100.64.0.10:11434/", "model": "qwen3-coder:30b"}}
    rl = {"action": "restart_lane", "args": {"lane": "cpu_worker"}}
    se = {"action": "set_env", "args": {"name": "OLLAMA_KEEP_ALIVE", "value": "10m"}}

    def fake(status=200, rc=0, env_ok=True):
        log = {"calls": [], "audit": []}
        deps = {"snapshot": lambda: (log["calls"].append("snapshot"), {"s": 1})[1],
                "save_snapshot": lambda obj: (log["calls"].append("save"), "SNAP1")[1],
                "http": lambda m, u, b: (log["calls"].append(("http", m, u, b)), status)[1],
                "run": lambda argv: (log["calls"].append(("run", list(argv))), rc)[1],
                "set_env": lambda n, v: (log["calls"].append(("env", n, v)), env_ok)[1],
                "audit": lambda line: log["audit"].append(line), "now": lambda: "2026-10-04T16:40:00"}
        return deps, log

    def c_actions():
        return ACTIONS == ("unload_model", "restart_lane", "set_env")

    def c_validate_ok():
        return validate_fix(um, cfg) == "" and validate_fix(rl, cfg) == "" and validate_fix(se, cfg) == ""

    def c_refuses_shape_and_unknown():
        return (validate_fix("rm -rf", cfg) != "" and validate_fix({"action": "shell", "args": {"cmd": "x"}}, cfg) == "unknown action: shell"
                and validate_fix({"action": "set_env"}, cfg) != "")

    def c_refuses_undeclared_lane():
        return validate_fix({"action": "restart_lane", "args": {"lane": "nope"}}, cfg) == "restart_lane: lane is not declared in config lanes"

    def c_refuses_bad_lane_command():
        return (validate_fix({"action": "restart_lane", "args": {"lane": "bad"}}, cfg) != ""
                and validate_fix({"action": "restart_lane", "args": {"lane": "worse"}}, cfg) != "")

    def c_refuses_bad_env():
        bad = [{"name": "lower", "value": "x"}, {"name": "A", "value": "two\nlines"}, {"name": "A", "value": "x" * 4097}, {"name": "A", "value": 5}]
        return all(validate_fix({"action": "set_env", "args": a}, cfg) != "" for a in bad)

    def c_refuses_non_http_url():
        return validate_fix({"action": "unload_model", "args": dict(um["args"], url="file:///etc")}, cfg) != ""

    def c_plan_unload():
        p = plan_fix(um, cfg)
        return p["http"] == {"method": "POST", "url": "http://100.64.0.10:11434/api/generate", "body": {"model": "qwen3-coder:30b", "keep_alive": 0}} and p["argv"] is None

    def c_plan_lane_copies_argv():
        p = plan_fix(rl, cfg)
        p["argv"].append("x")
        return cfg["lanes"]["cpu_worker"] == ["service", "restart", "cpu_worker"]

    def c_dry_run_calls_nothing():
        deps, log = fake()
        r = apply_fix(um, cfg, deps)
        return r["ok"] and r["applied"] is False and log["calls"] == [] and log["audit"] == []

    def c_invalid_calls_nothing():
        deps, log = fake()
        r = apply_fix({"action": "shell", "args": {}}, cfg, deps, apply=True)
        return r["ok"] is False and r["applied"] is False and log["calls"] == [] and log["audit"] == []

    def c_apply_snapshot_first_then_one_audit():
        deps, log = fake()
        r = apply_fix(rl, cfg, deps, apply=True)
        line = json.loads(log["audit"][0])
        return (r["ok"] and r["applied"] and log["calls"][:2] == ["snapshot", "save"] and log["calls"][2] == ("run", ["service", "restart", "cpu_worker"])
                and len(log["audit"]) == 1 and line == {"ts": "2026-10-04T16:40:00", "action": "restart_lane", "args": {"lane": "cpu_worker"},
                                                        "ok": True, "detail": "rc 0", "snapshot": "SNAP1"})

    def c_apply_http_failure_is_not_ok():
        deps, log = fake(status=500)
        r = apply_fix(um, cfg, deps, apply=True)
        return r["ok"] is False and r["detail"] == "http 500" and json.loads(log["audit"][0])["ok"] is False

    def c_set_env_audit_never_has_value():
        deps, log = fake()
        r = apply_fix({"action": "set_env", "args": {"name": "TOKEN_X", "value": "s3cret-value-123"}}, cfg, deps, apply=True)
        return r["ok"] and "s3cret" not in log["audit"][0] and json.loads(log["audit"][0])["args"] == {"name": "TOKEN_X", "value_len": 16}

    def c_audit_line_single_line():
        return "\n" not in audit_line("t", se, True, "set", "R")

    checks = [
        ("actions", c_actions), ("validate_ok", c_validate_ok), ("refuses_shape_and_unknown", c_refuses_shape_and_unknown),
        ("refuses_undeclared_lane", c_refuses_undeclared_lane), ("refuses_bad_lane_command", c_refuses_bad_lane_command),
        ("refuses_bad_env", c_refuses_bad_env), ("refuses_non_http_url", c_refuses_non_http_url), ("plan_unload", c_plan_unload),
        ("plan_lane_copies_argv", c_plan_lane_copies_argv), ("dry_run_calls_nothing", c_dry_run_calls_nothing),
        ("invalid_calls_nothing", c_invalid_calls_nothing), ("apply_snapshot_first_then_one_audit", c_apply_snapshot_first_then_one_audit),
        ("apply_http_failure_is_not_ok", c_apply_http_failure_is_not_ok), ("set_env_audit_never_has_value", c_set_env_audit_never_has_value),
        ("audit_line_single_line", c_audit_line_single_line),
    ]
    passed = 0
    for name, fn in checks:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL %s -- raised %s: %s" % (name, type(exc).__name__, exc))
        else:
            print(("  ok   " if ok else "  FAIL ") + name)
        passed += 1 if ok else 0
    total = len(checks)
    print("fixes selftest: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if total and passed == total else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="stack-guardian-fix", description="Plan (default) or apply one typed fix")
    ap.add_argument("--fix-json", help="file holding {action, args}")
    ap.add_argument("--config", help="file holding {lanes: {name: [argv...]}}")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.fix_json:
        ap.print_usage(sys.stderr)
        return 3
    try:
        with open(a.fix_json, "r", encoding="utf-8") as f:
            fix = json.load(f)
        cfg = {}
        if a.config:
            with open(a.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
    except (OSError, ValueError) as exc:
        print("fixes: error: %s" % exc, file=sys.stderr)
        return 3
    why = validate_fix(fix, cfg)
    if why:
        print(json.dumps({"ok": False, "error": why}))
        return 1
    plan = plan_fix(fix, cfg)
    print(json.dumps({"ok": True, "applied": False, "describe": plan["describe"],
                      "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    return 0


if __name__ == "__main__" and "--selftest" in sys.argv:
    sys.exit(_selftest())
if __name__ == "__main__":
    sys.exit(main())