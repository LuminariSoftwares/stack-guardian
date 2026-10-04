"""sg_parsers -- pure parsers for the "stack-guardian" snapshot (TJ plan P43 S2, C-Snapshot).

Standard library only.  No shelling out, no network: the caller passes the raw
command / HTTP output text in and gets plain data structures back.

Public API
----------
    parse_nvsmi(gpu_csv: str, apps_csv: str = "") -> list
    parse_tailscale(status_json: str, peer_host: str) -> dict
    parse_ollama_ps(ps_json: str) -> list

CLI
---
    python sg_parsers.py --nvsmi GPU_CSV_FILE [--apps APPS_CSV_FILE]
    python sg_parsers.py --selftest
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

__all__ = ["parse_nvsmi", "parse_tailscale", "parse_ollama_ps", "main"]

_UNKNOWN = {"peer": False, "direct": None, "derp": None, "ips": []}


def _basename(path: str) -> str:
    """Basename of a process path that may use either Windows or POSIX separators."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _to_int(field: str):
    """int(field) or None when the field is not an int (e.g. "[N/A]")."""
    try:
        return int(field)
    except (TypeError, ValueError):
        return None


def _parse_gpu_lines(gpu_csv: str) -> list:
    """One dict per parsable GPU line, in input order.  Never raises."""
    gpus = []
    for line in (gpu_csv or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            index, uuid, name, total_mib, used_mib = [f.strip() for f in line.split(",", 4)]
            idx = int(index)
            total = int(total_mib)
            used = int(used_mib)
        except (AttributeError, ValueError):
            continue
        # attributed_mib / unattributed_mib are recomputed per-GPU in
        # parse_nvsmi once the compute-app lines are attached.
        gpus.append({
            "index": idx,
            "uuid": uuid,
            "name": name,
            "total_mib": total,
            "used_mib": used,
            "attributed_mib": 0,
            "unattributed_mib": 0,
            "processes": [],
        })
    return gpus


def _parse_app_lines(apps_csv: str) -> list:
    """One dict per parsable compute-app line, in input order.  Never raises."""
    apps = []
    for line in (apps_csv or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            # A process path may itself contain commas, so only the memory
            # column is split off from the right.
            uuid, pid, rest = line.split(",", 2)
            pid_int = int(pid.strip())
            name, mem = rest.rsplit(",", 1)
        except (AttributeError, ValueError):
            continue
        apps.append({
            "uuid": uuid.strip(),
            "pid": pid_int,
            "name": _basename(name.strip()),
            "used_mib": _to_int(mem.strip()),
        })
    return apps


def parse_nvsmi(gpu_csv: str, apps_csv: str = "") -> list:
    """Parse `nvidia-smi` GPU and compute-app CSV into snapshot GPU records.

    Lines whose int fields do not parse are skipped; nothing here raises.
    """
    gpus = _parse_gpu_lines(gpu_csv)
    apps = _parse_app_lines(apps_csv)

    for gpu in gpus:
        attributed = 0
        processes = []
        for app in apps:
            if app["uuid"] != gpu["uuid"]:
                continue  # a process whose uuid matches no GPU is dropped
            processes.append({
                "pid": app["pid"],
                "name": app["name"],
                "used_mib": app["used_mib"],
            })
            # A "[N/A]" memory counts 0 toward attributed_mib.
            if app["used_mib"] is not None:
                attributed += app["used_mib"]
        gpu["attributed_mib"] = attributed
        gpu["unattributed_mib"] = max(0, gpu["used_mib"] - attributed)
        gpu["processes"] = processes

    return gpus


def parse_tailscale(status_json: str, peer_host: str) -> dict:
    """Parse `tailscale status --json` for one peer, matched on HostName (case-insensitive)."""
    try:
        data = json.loads(status_json)
    except (TypeError, ValueError):
        return dict(_UNKNOWN)

    if not isinstance(data, dict):
        return dict(_UNKNOWN)
    peers = data.get("Peer")
    if not isinstance(peers, dict):
        return dict(_UNKNOWN)

    wanted = peer_host.lower() if isinstance(peer_host, str) else None
    peer = None
    for candidate in peers.values():
        if not isinstance(candidate, dict):
            continue
        host = candidate.get("HostName")
        if isinstance(host, str) and wanted is not None and host.lower() == wanted:
            peer = candidate
            break

    if peer is None:
        return dict(_UNKNOWN)

    return {
        "peer": bool(peer.get("Online")),
        "direct": bool(peer.get("CurAddr")),
        "derp": peer.get("Relay") or None,
        "ips": list(peer.get("TailscaleIPs") or []),
    }


def parse_ollama_ps(ps_json: str) -> list:
    """Parse the body of Ollama GET /api/ps into model records, in order."""
    try:
        data = json.loads(ps_json)
    except (TypeError, ValueError):
        return []

    if not isinstance(data, dict):
        return []
    models = data.get("models")
    if not isinstance(models, list):
        return []

    result = []
    for model in models:
        if not isinstance(model, dict):
            continue
        size = _to_int(model.get("size_vram", 0))
        expires = model.get("expires_at", "")
        name = model.get("name", "")
        result.append({
            "name": "" if name is None else str(name),
            "size_vram": 0 if size is None else size,
            "expires_at": "" if expires is None else str(expires),
        })
    return result


def _cli_error(message: str) -> int:
    sys.stderr.write("sg_parsers: " + message + "\n")
    return 1


def _split_cli_argv(argv: list):
    """Accept `--flag VALUE`, `--flag=VALUE` and flags in any order."""
    nvsmi_file = None
    apps_file = None
    i = 0
    while i < len(argv):
        token = argv[i]
        key, sep, inline = token.partition("=")
        if key == "--nvsmi":
            nvsmi_file = inline if sep else _cli_value(argv, i)
            i += 2
        elif key == "--apps":
            apps_file = inline if sep else _cli_value(argv, i)
            i += 2
        elif key == "--selftest":
            return "selftest", None
        else:
            return None, "unrecognized argument: " + token
    if nvsmi_file is None:
        return None, "--nvsmi GPU_CSV_FILE is required"
    return (nvsmi_file, apps_file), None


def _cli_value(argv: list, i: int):
    return argv[i + 1] if i + 1 < len(argv) else ""


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: python sg_parsers.py --nvsmi GPU_CSV_FILE [--apps APPS_CSV_FILE]\n")
        sys.stderr.write("       python sg_parsers.py --selftest\n")
        return 1

    parsed, err = _split_cli_argv(argv)
    if err:
        return _cli_error(err)
    if parsed == "selftest":
        return _run_selftest()

    nvsmi_file, apps_file = parsed
    try:
        gpu_csv = _read_text(nvsmi_file)
        apps_csv = _read_text(apps_file) if apps_file else ""
    except OSError as exc:
        return _cli_error("cannot read input: " + str(exc))

    sys.stdout.write(json.dumps(parse_nvsmi(gpu_csv, apps_csv)) + "\n")
    return 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def _check_nvsmi_basic() -> bool:
    """No apps output still yields correct unattributed_mib for every GPU."""
    res = parse_nvsmi("0, GPU-9658, NVIDIA GeForce RTX 3060, 12288, 0\n"
                       "1, GPU-e138, NVIDIA GeForce RTX 3070, 8192, 4096")
    ok = len(res) == 2
    ok = ok and res[0]["index"] == 0 and res[0]["uuid"] == "GPU-9658"
    ok = ok and res[0]["name"] == "NVIDIA GeForce RTX 3060"
    ok = ok and res[0]["total_mib"] == 12288 and res[0]["used_mib"] == 0
    ok = ok and res[0]["attributed_mib"] == 0 and res[0]["unattributed_mib"] == 0
    ok = ok and res[0]["processes"] == []
    ok = ok and res[1]["index"] == 1 and res[1]["total_mib"] == 8192
    ok = ok and res[1]["used_mib"] == 4096 and res[1]["attributed_mib"] == 0
    # The contract wants max(0, used_mib - attributed_mib) even with no apps.
    ok = ok and res[1]["unattributed_mib"] == 4096 and res[1]["processes"] == []
    return ok


def _check_nvsmi_with_apps() -> bool:
    """Apps memory is attributed per-GPU and split off the remainder."""
    res = parse_nvsmi("0, GPU-9658, NVIDIA GeForce RTX 3060, 12288, 1024",
                      "GPU-9658, 15808, C:\\Windows\\explorer.exe, 512\n"
                      "GPU-9658, 15809, C:\\Program Files\\Chrome\\chrome.exe, 256")
    ok = len(res) == 1
    ok = ok and res[0]["attributed_mib"] == 768 and res[0]["unattributed_mib"] == 256
    procs = res[0]["processes"]
    ok = ok and len(procs) == 2
    ok = ok and procs[0] == {"pid": 15808, "name": "explorer.exe", "used_mib": 512}
    ok = ok and procs[1] == {"pid": 15809, "name": "chrome.exe", "used_mib": 256}
    return ok


def _check_nvsmi_app_na_memory() -> bool:
    """'[N/A]' used_memory becomes None and contributes 0 to attributed_mib."""
    res = parse_nvsmi("0, GPU-9658, NVIDIA GeForce RTX 3060, 12288, 1400",
                      "GPU-9658, 15808, C:\\Windows\\explorer.exe, [N/A]\n"
                      "GPU-9658, 15809, C:\\Program Files\\tool, 700\n"
                      "GPU-9658, 15810, /x/junk, abc")
    ok = len(res) == 1
    ok = ok and res[0]["attributed_mib"] == 700 and res[0]["unattributed_mib"] == 700
    procs = res[0]["processes"]
    ok = ok and len(procs) == 3
    ok = ok and procs[0] == {"pid": 15808, "name": "explorer.exe", "used_mib": None}
    ok = ok and procs[1] == {"pid": 15809, "name": "tool", "used_mib": 700}
    # Anything non-int, not just the literal "[N/A]", becomes None.
    ok = ok and procs[2] == {"pid": 15810, "name": "junk", "used_mib": None}
    return ok


def _check_nvsmi_path_with_commas() -> bool:
    """A path containing commas survives the split(",", 2) / rsplit(",", 1) rules."""
    res = parse_nvsmi("0, GPU-9658, NVIDIA GeForce RTX 3060, 12288, 100",
                      "GPU-9658, 4242, /opt/models/a,b/run, 100")
    ok = len(res) == 1 and res[0]["attributed_mib"] == 100
    procs = res[0]["processes"]
    ok = ok and len(procs) == 1 and procs[0]["pid"] == 4242
    ok = ok and procs[0]["name"] == "run" and procs[0]["used_mib"] == 100
    return ok


def _check_nvsmi_posix_basename() -> bool:
    """Basename splits on '/' as well as on the Windows separator."""
    res = parse_nvsmi("0, GPU-9658, RTX 3060, 12288, 10",
                      "GPU-9658, 7, /opt/vllm/bin/python3, 10")
    ok = len(res) == 1 and len(res[0]["processes"]) == 1
    ok = ok and res[0]["processes"][0]["name"] == "python3"
    return ok


def _check_nvsmi_unknown_uuid_dropped() -> bool:
    """A process whose uuid matches no GPU is dropped."""
    res = parse_nvsmi("0, GPU-9658, RTX 3060, 12288, 300",
                      "GPU-zzzz, 99, /bin/ghost, 300")
    ok = len(res) == 1
    ok = ok and res[0]["processes"] == []
    ok = ok and res[0]["attributed_mib"] == 0 and res[0]["unattributed_mib"] == 300
    return ok


def _check_nvsmi_skips_bad_lines() -> bool:
    """Blank lines and lines whose int fields do not parse are skipped, never raised on."""
    res = parse_nvsmi("0, GPU-9658, RTX 3060, 12288, 5\n"
                      "\n"
                      "   \n"
                      "invalid_line\n"
                      "1, GPU-e138, RTX 3070, invalid, 4096\n"
                      "1, GPU-e138, RTX 3070, 8192, 4096")
    ok = len(res) == 2
    ok = ok and [g["uuid"] for g in res] == ["GPU-9658", "GPU-e138"]
    ok = ok and res[0]["unattributed_mib"] == 5
    return ok


def _check_nvsmi_multi_gpu_order() -> bool:
    """GPU order is input order and attribution is per uuid."""
    res = parse_nvsmi("2, GPU-c, RTX A, 100, 10\n"
                      "0, GPU-a, RTX B, 200, 20\n"
                      "1, GPU-b, RTX C, 300, 30",
                      "GPU-b, 1, /x/b.exe, 5\n"
                      "GPU-a, 2, /x/a.exe, 7\n"
                      "GPU-b, 3, /x/c.exe, [N/A]")
    ok = len(res) == 3
    ok = ok and [g["index"] for g in res] == [2, 0, 1]
    ok = ok and [g["uuid"] for g in res] == ["GPU-c", "GPU-a", "GPU-b"]
    ok = ok and res[0]["processes"] == [] and res[0]["unattributed_mib"] == 10
    ok = ok and res[1]["attributed_mib"] == 7 and res[1]["unattributed_mib"] == 13
    ok = ok and res[2]["attributed_mib"] == 5 and res[2]["unattributed_mib"] == 25
    ok = ok and [p["pid"] for p in res[2]["processes"]] == [1, 3]
    return ok


def _check_nvsmi_attribution_exceeds_used() -> bool:
    """unattributed_mib is clamped at 0 when processes report more than used."""
    res = parse_nvsmi("0, GPU-9658, RTX 3060, 12288, 10",
                      "GPU-9658, 1, /x/a.exe, 60\n"
                      "GPU-9658, 2, /x/b.exe, 40")
    ok = len(res) == 1
    ok = ok and res[0]["attributed_mib"] == 100 and res[0]["unattributed_mib"] == 0
    return ok


def _check_nvsmi_never_raises() -> bool:
    """No malformed input shape may raise, however the int columns are broken."""
    ok = True
    for junk in [
        "not a csv line at all",
        "x, GPU-a, Name, 100, 5",           # non-int index
        "0, GPU-a, Name, 100",              # too few columns
        "0, GPU-a, Name, 100, 5, extra",    # extra columns -> int() must fail, not split oddly
        ",,,,",                             # all empty
        "0, GPU-a, a, b, c, 100, 5",        # commas inside the name column
        "0, GPU-a, Name, 1.5, 5",           # float memory
        "0, GPU-a, Name, 100, [N/A]",
    ]:
        try:
            res = parse_nvsmi(junk)
        except Exception:  # noqa: BLE001  -- the contract says never raise
            ok = False
            continue
        if not isinstance(res, list):
            ok = False
        for gpu in res:
            if gpu["unattributed_mib"] != max(0, gpu["used_mib"] - gpu["attributed_mib"]):
                ok = False
    # The same guarantee applies to the apps blob, including a bad pid.
    for junk_apps in ["nope", "GPU-a, notapid, /x/a.exe, 5", "GPU-a, 1", ",,,"]:
        try:
            parse_nvsmi("0, GPU-a, Name, 100, 5", junk_apps)
        except Exception:  # noqa: BLE001
            ok = False
    # Exactly one line survives the mixed bag, with its unattributed value intact.
    try:
        mixed = parse_nvsmi("x, GPU-a, Name, 100, 5\n0, GPU-b, Name, 100, 5")
    except Exception:  # noqa: BLE001
        mixed = None
    return ok and mixed == [{"index": 0, "uuid": "GPU-b", "name": "Name", "total_mib": 100,
                             "used_mib": 5, "attributed_mib": 0, "unattributed_mib": 5,
                             "processes": []}]


def _check_nvsmi_empty() -> bool:
    """Empty and whitespace-only input give an empty list, with an optional apps blob."""
    ok = parse_nvsmi("") == []
    ok = ok and parse_nvsmi("   \n\n") == []
    ok = ok and parse_nvsmi("", "GPU-9658, 1, /x/a.exe, 60") == []
    return ok


def _check_tailscale_online_direct() -> bool:
    """An online peer with a CurAddr is direct and reports its derp and IPs."""
    status = json.dumps({"Peer": {"p1": {
        "HostName": "myhost", "Online": True, "CurAddr": "10.0.0.1",
        "Relay": "us-central1",
        "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0:ab12:3456:7890:abcd:ef12"]}}})
    res = parse_tailscale(status, "myhost")
    ok = res["peer"] is True and res["direct"] is True
    ok = ok and res["derp"] == "us-central1"
    ok = ok and res["ips"] == ["100.64.0.1", "fd7a:115c:a1e0:ab12:3456:7890:abcd:ef12"]
    ok = ok and len(res) == 4
    return ok


def _check_tailscale_offline_derp() -> bool:
    """An offline peer behind a relay: peer/direct False, derp kept."""
    status = json.dumps({"Peer": {"p1": {
        "HostName": "box", "Online": False, "CurAddr": "",
        "Relay": "sea", "TailscaleIPs": []}}})
    res = parse_tailscale(status, "box")
    ok = res["peer"] is False and res["direct"] is False
    ok = ok and res["derp"] == "sea" and res["ips"] == []
    return ok


def _check_tailscale_empty_relay_is_none() -> bool:
    """A falsy Relay (empty string, as tailscale reports a direct connection) is None."""
    status = json.dumps({"Peer": {"p1": {
        "HostName": "direct-box", "Online": True, "CurAddr": "10.1.1.9",
        "Relay": "", "TailscaleIPs": ["100.64.0.9"]}}})
    res = parse_tailscale(status, "direct-box")
    ok = res == {"peer": True, "direct": True, "derp": None, "ips": ["100.64.0.9"]}
    ok = ok and res["derp"] is None
    return ok


def _check_tailscale_case_insensitive() -> bool:
    """HostName matching is case-insensitive on both sides."""
    status = json.dumps({"Peer": {
        "p1": {"HostName": "Other", "Online": True},
        "p2": {"HostName": "MyBox", "Online": True, "CurAddr": "192.168.1.5"}}})
    res = parse_tailscale(status, "mybox")
    ok = res["peer"] is True and res["direct"] is True
    ok = ok and res["derp"] is None and res["ips"] == []
    return ok


def _check_tailscale_missing_relay_keys() -> bool:
    """Absent optional keys do not raise; Online/CurAddr fall back to False."""
    status = json.dumps({"Peer": {"p1": {"HostName": "bare"}}})
    res = parse_tailscale(status, "bare")
    ok = res == {"peer": False, "direct": False, "derp": None, "ips": []}
    return ok


def _check_tailscale_not_found() -> bool:
    """A peer that is absent gives the not-found record with direct None."""
    status = json.dumps({"Peer": {"p1": {"HostName": "otherhost", "Online": True}}})
    return parse_tailscale(status, "myhost") == dict(_UNKNOWN)


def _check_tailscale_invalid_json() -> bool:
    """Unparsable JSON gives the not-found record rather than raising."""
    ok = parse_tailscale('{"invalid": json}', "myhost") == dict(_UNKNOWN)
    ok = ok and parse_tailscale("", "myhost") == dict(_UNKNOWN)
    ok = ok and parse_tailscale("not json at all", "myhost") == dict(_UNKNOWN)
    return ok


def _check_tailscale_bad_shape() -> bool:
    """A non-dict Peer, a non-dict peer entry, or a JSON list gives not-found."""
    ok = parse_tailscale(json.dumps({"Peer": ["myhost"]}), "myhost") == dict(_UNKNOWN)
    ok = ok and parse_tailscale(json.dumps({"Peer": {"p1": "myhost"}}), "myhost") == dict(_UNKNOWN)
    ok = ok and parse_tailscale(json.dumps([{"HostName": "myhost"}]), "myhost") == dict(_UNKNOWN)
    ok = ok and parse_tailscale(json.dumps({"Peer": {"p1": {"HostName": 7}}}), "myhost") == dict(_UNKNOWN)
    return ok


def _check_ollama_ps_basic() -> bool:
    """Models come back in order with int size_vram and str expires_at."""
    body = json.dumps({"models": [
        {"name": "llama3:latest", "size_vram": 1024, "expires_at": "2026-01-01T00:00:00Z"},
        {"name": "mistral", "size_vram": 2048, "expires_at": "2026-01-02T00:00:00Z"}]})
    res = parse_ollama_ps(body)
    ok = len(res) == 2
    ok = ok and res[0] == {"name": "llama3:latest", "size_vram": 1024,
                           "expires_at": "2026-01-01T00:00:00Z"}
    ok = ok and res[1] == {"name": "mistral", "size_vram": 2048,
                           "expires_at": "2026-01-02T00:00:00Z"}
    return ok


def _check_ollama_ps_defaults() -> bool:
    """Missing size_vram is 0 and missing expires_at is the empty string."""
    res = parse_ollama_ps(json.dumps({"models": [{"name": "phi3"}]}))
    ok = res == [{"name": "phi3", "size_vram": 0, "expires_at": ""}]
    ok = ok and parse_ollama_ps(json.dumps({"models": [{}]})) == [
        {"name": "", "size_vram": 0, "expires_at": ""}]
    return ok


def _check_ollama_ps_bad_values() -> bool:
    """Non-int size_vram becomes 0 and non-dict entries are skipped."""
    res = parse_ollama_ps(json.dumps({"models": [
        {"name": "a", "size_vram": None, "expires_at": None},
        "not a dict",
        {"name": "b", "size_vram": "128", "expires_at": "soon"}]}))
    ok = len(res) == 2
    ok = ok and res[0] == {"name": "a", "size_vram": 0, "expires_at": ""}
    ok = ok and res[1] == {"name": "b", "size_vram": 128, "expires_at": "soon"}
    return ok


def _check_ollama_ps_invalid_json() -> bool:
    """Unparsable JSON gives an empty list."""
    ok = parse_ollama_ps('{"invalid": json}') == []
    ok = ok and parse_ollama_ps("") == []
    ok = ok and parse_ollama_ps("[]") == []
    return ok


def _check_ollama_ps_missing_models() -> bool:
    """No 'models' key, or one that is not a list, gives an empty list."""
    ok = parse_ollama_ps(json.dumps({"other": "data"})) == []
    ok = ok and parse_ollama_ps(json.dumps({"models": "none"})) == []
    ok = ok and parse_ollama_ps(json.dumps({"models": []})) == []
    return ok


def _safe_json_loads(text: str):
    """json.loads(text) or None, so a check can never die on malformed output."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _check_cli_nvsmi_one_line_json() -> bool:
    """--nvsmi prints json.dumps(result) as exactly one line of valid JSON."""
    gpu_csv = "0, GPU-9658, NVIDIA GeForce RTX 3060, 12288, 1024"
    apps_csv = "GPU-9658, 15808, C:\\Windows\\explorer.exe, 512"
    workdir = tempfile.mkdtemp()
    ok = False
    try:
        gpu_path = os.path.join(workdir, "gpu.csv")
        apps_path = os.path.join(workdir, "apps.csv")
        with open(gpu_path, "w", encoding="utf-8") as handle:
            handle.write(gpu_csv)
        with open(apps_path, "w", encoding="utf-8") as handle:
            handle.write(apps_csv)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--nvsmi", gpu_path, "--apps", apps_path])
        printed = buf.getvalue()
        lines = printed.splitlines()
        payload = _safe_json_loads(lines[0]) if len(lines) == 1 else None

        ok = (code == 0 and printed.count("\n") == 1 and printed.endswith("\n")
              and len(lines) == 1 and payload is not None)
        ok = ok and payload == parse_nvsmi(gpu_csv, apps_csv)
        ok = ok and payload[0]["attributed_mib"] == 512
        ok = ok and payload[0]["unattributed_mib"] == 512
        ok = ok and payload[0]["processes"][0]["name"] == "explorer.exe"

        # The same run without --apps must still print exactly one JSON line.
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            code2 = main(["--nvsmi", gpu_path])
        printed2 = buf2.getvalue()
        payload2 = _safe_json_loads(printed2)
        ok = ok and code2 == 0 and printed2.count("\n") == 1
        ok = ok and payload2 is not None and payload2[0]["unattributed_mib"] == 1024
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return ok


SELFTESTS = [
    ("nvsmi_basic", _check_nvsmi_basic),
    ("nvsmi_with_apps", _check_nvsmi_with_apps),
    ("nvsmi_app_na_memory", _check_nvsmi_app_na_memory),
    ("nvsmi_path_with_commas", _check_nvsmi_path_with_commas),
    ("nvsmi_posix_basename", _check_nvsmi_posix_basename),
    ("nvsmi_unknown_uuid_dropped", _check_nvsmi_unknown_uuid_dropped),
    ("nvsmi_skips_bad_lines", _check_nvsmi_skips_bad_lines),
    ("nvsmi_multi_gpu_order", _check_nvsmi_multi_gpu_order),
    ("nvsmi_attribution_exceeds_used", _check_nvsmi_attribution_exceeds_used),
    ("nvsmi_never_raises", _check_nvsmi_never_raises),
    ("nvsmi_empty", _check_nvsmi_empty),
    ("tailscale_online_direct", _check_tailscale_online_direct),
    ("tailscale_offline_derp", _check_tailscale_offline_derp),
    ("tailscale_empty_relay_is_none", _check_tailscale_empty_relay_is_none),
    ("tailscale_case_insensitive", _check_tailscale_case_insensitive),
    ("tailscale_missing_relay_keys", _check_tailscale_missing_relay_keys),
    ("tailscale_not_found", _check_tailscale_not_found),
    ("tailscale_invalid_json", _check_tailscale_invalid_json),
    ("tailscale_bad_shape", _check_tailscale_bad_shape),
    ("ollama_ps_basic", _check_ollama_ps_basic),
    ("ollama_ps_defaults", _check_ollama_ps_defaults),
    ("ollama_ps_bad_values", _check_ollama_ps_bad_values),
    ("ollama_ps_invalid_json", _check_ollama_ps_invalid_json),
    ("ollama_ps_missing_models", _check_ollama_ps_missing_models),
    ("cli_nvsmi_one_line_json", _check_cli_nvsmi_one_line_json),
]


def _run_selftest() -> int:
    """Run every named check; the pass counter is incremented from each return value."""
    passed = 0
    for name, check in SELFTESTS:
        try:
            result = check()
        except Exception as exc:  # a raising check is a failing check, never a crash
            result = False
            sys.stderr.write("sg_parsers selftest: " + name + " raised "
                             + type(exc).__name__ + ": " + str(exc) + "\n")
        if result:
            passed += 1
        else:
            sys.stderr.write("sg_parsers selftest: FAIL " + name + "\n")

    total = len(SELFTESTS)
    failed = total - passed
    sys.stdout.write("sg_parsers selftest: " + str(total) + " checks, "
                     + str(passed) + " passed, " + str(failed) + " failed\n")
    sys.stdout.flush()
    return 0 if failed == 0 and total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())