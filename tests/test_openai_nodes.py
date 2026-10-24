"""OpenAI-compatible nodes as first-class nodes: {"openai": ["http://HOST:PORT/v1"], "api_key_env": "NAME"}.

Every server here is a stdlib stub on 127.0.0.1 that answers GET /v1/models the way LM Studio, llama.cpp
llama-server, vLLM and LiteLLM document it. These tests prove the code against that shape only; they say nothing
about a real server (the README says which real servers were run).

Before this change, on main aa6671c, a node {"openai": [...]} was a usage error (exit 3, "nothing to probe"),
and the older {"kind": "openai", "endpoints": [...]} form reported a dead server as ollama.unreachable
("1 Ollama endpoint(s) do not answer"), an empty model list as worst: OK, and could not send a bearer token.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stack_guardian import cli, collect, core, mcp_server, nodes as sg_nodes

ROOT = Path(__file__).resolve().parent.parent
MODELS = {"object": "list", "data": [{"id": "qwen2.5-7b-instruct", "object": "model", "owned_by": "x"},
                                      {"id": "nomic-embed-text", "object": "model", "owned_by": "x"}]}
KEY = "stub-bearer-0123456789abcdef"  # a stub's secret: the tests prove it never appears in any output
DEAD = "http://127.0.0.1:9/v1"


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("STACK_GUARDIAN_")}
    env.update(extra)
    return env


def _run(tmp_path, doc, *args, env=None, command="doctor"):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    state = ["--state", str(tmp_path / "state.json")] if command == "doctor" else []
    return subprocess.run([sys.executable, "-m", "stack_guardian", command, "--nodes", str(path), *state, *args],
                          capture_output=True, text=True, timeout=120, env=env or _env(), cwd=str(ROOT))


def _snap(doc, **kw):
    return collect.snapshot(doc, now="T", **kw)


def _codes(snapshot, ctx=None):
    return [(f["code"], f["node"], f["level"]) for f in
            core.run_all(core.load_checks(cli.DEFAULT_CHECKS_DIR), snapshot, ctx or {})]


# --- the nodes file ---------------------------------------------------------------------------------------------

def test_an_openai_node_is_accepted_beside_or_instead_of_ollama():
    nodes, _ = sg_nodes.validate({"only": {"openai": ["http://127.0.0.1:1234/v1"]},
                                  "both": {"ollama": ["http://127.0.0.1:11434"], "openai": ["http://192.0.2.7:8000/v1"],
                                           "api_key_env": "LITELLM_KEY"}})
    assert nodes["only"]["openai"] == ["http://127.0.0.1:1234/v1"] and nodes["only"]["ollama"] == []
    assert nodes["both"]["openai"] == ["http://192.0.2.7:8000/v1"] and nodes["both"]["api_key_env"] == "LITELLM_KEY"


def test_openai_urls_follow_the_same_shape_rules_and_error_style():
    said = []
    nodes, _ = sg_nodes.validate({"a": {"openai": "http://127.0.0.1:1234/v1"}}, warn=said.append)
    assert nodes["a"]["openai"] == ["http://127.0.0.1:1234/v1"] and len(said) == 1 and "one URL string" in said[0]
    for bad, needle in (({"openai": {"url": "http://x/v1"}}, "must be a list of base URLs"),
                        ({"openai": ["127.0.0.1:1234/v1"]}, "needs a scheme"),
                        ({"openai": [1]}, "must be a base URL string")):
        with pytest.raises(sg_nodes.NodesError) as err:
            sg_nodes.validate({"a": bad})
        assert 'node "a"' in str(err.value) and '"openai"' in str(err.value) and needle in str(err.value), str(err.value)


def test_a_node_with_neither_endpoints_nor_local_nor_tailnet_host_is_still_refused():
    for doc in ({"a": {}}, {"a": {"openai": []}}, {"a": {"ollama": [], "openai": []}}, {"a": {"api_key_env": "K"}}):
        with pytest.raises(sg_nodes.NodesError) as err:
            sg_nodes.validate({"a": doc["a"]})
        assert "nothing to probe" in str(err.value) and '"openai"' in str(err.value), str(err.value)
    nodes, _ = sg_nodes.validate({"a": {"openai": [], "local": True}})
    assert nodes["a"]["openai"] == []


def test_api_key_env_is_a_variable_name_and_a_pasted_key_is_refused_without_echoing_it():
    for doc in ({"openai": ["http://x:1/v1"], "api_key_env": KEY},
                {"openai": ["http://x:1/v1"], "api_key_env": ""},
                {"openai": ["http://x:1/v1"], "api_key_env": ["K"]},
                {"ollama": ["http://x:1"], "api_key_env": "OK_NAME"}):  # no "openai" list for the key to go with
        with pytest.raises(sg_nodes.NodesError) as err:
            sg_nodes.validate({"a": doc})
        assert 'node "a"' in str(err.value) and "api_key_env" in str(err.value), str(err.value)
        assert KEY not in str(err.value), "the message must not echo the value"


def test_the_older_kind_openai_form_still_validates_and_still_means_what_it_meant():
    nodes, _ = sg_nodes.validate({"studio": {"kind": "openai", "endpoints": ["http://127.0.0.1:1234"]}})
    assert nodes["studio"]["endpoints"] == ["http://127.0.0.1:1234"] and "openai" not in nodes["studio"]


# --- the collector ----------------------------------------------------------------------------------------------

def test_collector_probes_models_under_the_base_url_and_records_ids_and_latency(stub):
    s = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    snap = _snap({"studio": {"openai": [s.url + "/v1/"]}})  # a trailing slash is fine
    node = snap["nodes"]["studio"]
    [ep] = node["openai"]
    assert s.paths("GET") == ["/v1/models"]
    assert node["reachable"] is True and node["ollama"] == []
    assert ep["url"] == s.url + "/v1" and ep["kind"] == "openai" and ep["error"] is None
    assert ep["models"] == ["qwen2.5-7b-instruct", "nomic-embed-text"] and isinstance(ep["latency_ms"], int)
    assert "running" not in ep, "loaded-model state is Ollama-only"


def test_collector_records_a_dead_endpoint_and_an_unlisted_server_as_data(stub):
    wrong = stub({("GET", "/v1/models"): (200, {"hello": "not a model list"}, 0)})
    html = stub({("GET", "/v1/models"): (200, "<html>nope</html>", 0)})
    gone = stub({})  # answers 404 to everything
    snap = _snap({"a": {"openai": [DEAD]}, "b": {"openai": [wrong.url + "/v1"]}, "c": {"openai": [html.url + "/v1"]},
                  "d": {"openai": [gone.url]}, "e": {"openai": [gone.url + "/v1"]}})
    errs = {n: snap["nodes"][n]["openai"][0]["error"] for n in "abcde"}
    assert errs["a"].startswith(("URLError", "ConnectionRefusedError", "OSError"))
    assert errs["b"].startswith("ValueError") and "model list" in errs["b"]
    assert errs["c"].startswith("JSONDecodeError")
    assert errs["d"].startswith("ValueError") and "404" in errs["d"] and "usually ends in /v1" in errs["d"]
    assert errs["e"] == "HTTPError: HTTP Error 404: Not Found"  # already /v1: nothing to hint at
    assert all(snap["nodes"][n]["reachable"] is False for n in "abcde")


def test_an_empty_model_list_is_an_answer_not_an_error(stub):
    s = stub({("GET", "/v1/models"): (200, {"object": "list", "data": []}, 0)})
    [ep] = _snap({"a": {"openai": [s.url + "/v1"]}})["nodes"]["a"]["openai"]
    assert ep["error"] is None and ep["models"] == []


def test_a_huge_model_list_is_capped_and_says_so(stub):
    s = stub({("GET", "/v1/models"): (200, {"data": [{"id": "m%d" % i} for i in range(450)]}, 0)})
    [ep] = _snap({"a": {"openai": [s.url + "/v1"]}})["nodes"]["a"]["openai"]
    assert len(ep["models"]) == 200 and ep["models_total"] == 450


def test_bearer_token_is_read_from_the_named_variable_and_never_recorded(stub, monkeypatch):
    s = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    monkeypatch.setenv("SG_TEST_KEY", KEY)
    snap = _snap({"a": {"openai": [s.url + "/v1"], "api_key_env": "SG_TEST_KEY"}})
    assert s.headers[0]["authorization"] == "Bearer " + KEY
    assert snap["nodes"]["a"]["reachable"] is True
    assert KEY not in json.dumps(snap)
    plain = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    _snap({"a": {"openai": [plain.url + "/v1"]}})
    assert "authorization" not in plain.headers[0], "no api_key_env, no credential"


def test_a_missing_variable_is_reported_by_name_and_nothing_is_sent(stub, monkeypatch):
    monkeypatch.delenv("SG_TEST_KEY", raising=False)
    s = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    [ep] = _snap({"a": {"openai": [s.url + "/v1"], "api_key_env": "SG_TEST_KEY"}})["nodes"]["a"]["openai"]
    assert "SG_TEST_KEY" in ep["error"] and "not set" in ep["error"]
    assert s.requests == []


def test_a_rejected_key_is_an_error_that_does_not_contain_the_key(stub, monkeypatch):
    s = stub({("GET", "/v1/models"): (401, {"error": {"message": "bad key"}}, 0)})
    monkeypatch.setenv("SG_TEST_KEY", KEY)
    [ep] = _snap({"a": {"openai": [s.url + "/v1"], "api_key_env": "SG_TEST_KEY"}})["nodes"]["a"]["openai"]
    assert ep["error"].startswith("HTTPError") and "401" in ep["error"] and KEY not in ep["error"]


def test_the_bearer_token_is_not_forwarded_to_another_host_by_a_redirect(stub, monkeypatch):
    target = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    origin = stub({("GET", "/v1/models"): (302, "", 0, {"Location": target.url + "/v1/models"})})
    monkeypatch.setenv("SG_TEST_KEY", KEY)
    [ep] = _snap({"a": {"openai": [origin.url + "/v1"], "api_key_env": "SG_TEST_KEY"}})["nodes"]["a"]["openai"]
    assert origin.headers[0]["authorization"] == "Bearer " + KEY
    assert ep["error"] is None and ep["models"] == ["qwen2.5-7b-instruct", "nomic-embed-text"]  # redirect was followed
    assert "authorization" not in target.headers[0], "the other host must never see the token"


def test_openai_endpoint_respects_the_single_deadline(stub):
    slow = stub({("GET", "/v1/models"): (200, MODELS, 2.0)})
    snap = _snap({"a": {"openai": [slow.url + "/v1"]}}, timeout_s=0.3)
    assert snap["nodes"]["a"]["error"] == "timeout after 0.3s"
    assert _codes(snap) == [("node.unreachable", "a", "FAIL")]


def test_a_node_with_both_kinds_is_reachable_when_either_answers(stub):
    s = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    node = _snap({"a": {"ollama": ["http://127.0.0.1:9"], "openai": [s.url + "/v1"]}})["nodes"]["a"]
    assert node["reachable"] is True and node["latency_ms"] == node["openai"][0]["latency_ms"]
    assert len(node["ollama"]) == 1 and len(node["openai"]) == 1


# --- the rules --------------------------------------------------------------------------------------------------

def _ep(**over):
    return dict({"url": "http://127.0.0.1:8000/v1", "kind": "openai", "models": ["m"], "latency_ms": 20, "error": None}, **over)


def _node(*eps, **over):
    return dict({"reachable": True, "latency_ms": 20, "ollama": [], "openai": list(eps)}, **over)


def test_openai_unreachable_is_a_fail_naming_each_endpoint():
    snap = {"taken_at": "T", "nodes": {"a": _node(_ep(), _ep(url="http://192.0.2.7:8000/v1", error="URLError: refused", models=[]))}}
    [(code, node, level)] = _codes(snap)
    assert (code, node, level) == ("openai.unreachable", "a", "FAIL")
    [f] = [f for f in core.run_all(core.load_checks(cli.DEFAULT_CHECKS_DIR), snap, {})]
    assert "1 OpenAI-compatible endpoint(s)" in f["summary"] and "192.0.2.7" in f["summary"]
    assert f["severity"] == "missing" and "refused" in f["evidence"][0] and f["try"]


def test_openai_no_models_is_a_warn_only_when_the_server_answered():
    assert _codes({"nodes": {"a": _node(_ep(models=[]))}}) == [("openai.no_models", "a", "WARN")]
    down = _node(_ep(models=[], error="URLError: refused"), reachable=False)
    assert _codes({"nodes": {"a": down}}) == [("openai.unreachable", "a", "FAIL")]  # not also "no models"


def test_openai_slow_is_info_above_the_threshold_and_the_threshold_is_settable():
    snap = {"nodes": {"a": _node(_ep(latency_ms=2600))}}
    assert _codes(snap) == [("openai.slow", "a", "INFO")]
    assert _codes(snap, {"openai_slow_ms": 3000}) == []
    assert _codes({"nodes": {"a": _node(_ep(latency_ms=1999))}}) == []


def test_a_healthy_openai_node_has_no_findings_and_ollama_rules_ignore_it():
    assert _codes({"taken_at": "T", "nodes": {"a": _node(_ep())}}) == []
    # no ollama.unreachable, model.expiring or model.slow_ttft from a node that only has OpenAI endpoints
    snap = {"nodes": {"a": _node(_ep(), reachable=True)}}
    assert _codes(snap, {"expect": {"a": {"model": "m"}}}) == []


def test_model_not_loaded_counts_the_ids_an_openai_endpoint_lists():
    snap = {"nodes": {"a": _node(_ep(models=["qwen2.5-7b-instruct"]))}}
    assert _codes(snap, {"expect": {"a": {"model": "qwen2.5-7b-instruct"}}}) == []
    assert _codes(snap, {"expect": {"a": {"model": "llama3"}}}) == [("model.not_loaded", "a", "WARN")]


def test_new_codes_are_explained_for_the_mcp_stack_explain_tool():
    for code in ("openai.unreachable", "openai.no_models", "openai.slow"):
        info = mcp_server.explain(code)
        assert info["try"] and info["evidence_needed"] and "unknown code" not in info["summary"], code


# --- end to end: CLI, confidence, MCP ---------------------------------------------------------------------------

def test_doctor_cli_reports_a_dead_openai_node_and_grades_it_like_ollama(tmp_path):
    doc = {"studio": {"openai": [DEAD]}}
    first = _run(tmp_path, doc, "--json")
    second = _run(tmp_path, doc, "--json")
    [f1] = json.loads(first.stdout)["findings"]
    [f2] = json.loads(second.stdout)["findings"]
    assert first.returncode == second.returncode == 2, first.stdout + first.stderr
    assert (f1["code"], f1["level"], f1["confidence"]) == ("openai.unreachable", "FAIL", "LIKELY")
    assert f2["confidence"] == "VERIFIED"


def test_doctor_cli_healthy_and_empty_servers(tmp_path, stub):
    ok = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    empty = stub({("GET", "/v1/models"): (200, {"object": "list", "data": []}, 0)})
    good = _run(tmp_path, {"studio": {"openai": [ok.url + "/v1"]}})
    assert good.returncode == 0 and good.stdout.strip() == "worst: OK", good.stdout + good.stderr
    bad = _run(tmp_path, {"studio": {"openai": [empty.url + "/v1"]}})
    assert bad.returncode == 1 and "WARN openai.no_models [studio]" in bad.stdout, bad.stdout + bad.stderr


def test_the_key_appears_in_no_output_and_no_file(tmp_path, stub):
    s = stub({("GET", "/v1/models"): (200, MODELS, 0)})
    doc = {"studio": {"openai": [s.url + "/v1"], "api_key_env": "SG_TEST_KEY"}}
    env = _env(SG_TEST_KEY=KEY)
    results = [_run(tmp_path, doc, "--json", env=env), _run(tmp_path, doc, env=env),
               _run(tmp_path, doc, env=env, command="snapshot")]
    assert all(KEY in (h.get("authorization", "")) for h in s.headers) and len(s.headers) == 3
    for r in results:
        assert r.returncode == 0, r.stdout + r.stderr
        assert KEY not in r.stdout and KEY not in r.stderr
    assert "qwen2.5-7b-instruct" in results[2].stdout
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(encoding="utf-8", errors="replace"), p


def test_mcp_health_and_snapshot_include_openai_nodes(tmp_path, stub):
    s = stub({("GET", "/v1/models"): (200, {"data": []}, 0)})
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"studio": {"openai": [s.url + "/v1"]}, "gone": {"openai": [DEAD]}}), encoding="utf-8")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "stack_health", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "stack_snapshot", "arguments": {"node": "studio"}}}]
    r = subprocess.run([sys.executable, "-m", "stack_guardian", "mcp", "--nodes", str(nodes), "--state", str(tmp_path / "st.json")],
                       input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, timeout=120, env=_env())
    out = {o["id"]: o for o in (json.loads(x) for x in r.stdout.splitlines() if x.strip())}
    health = json.loads(out[2]["result"]["content"][0]["text"])
    assert {(f["code"], f["node"]) for f in health["findings"]} == {("openai.unreachable", "gone"), ("openai.no_models", "studio")}
    snap = json.loads(out[3]["result"]["content"][0]["text"])
    assert snap["nodes"]["studio"]["openai"][0]["models"] == [] and "gone" not in snap["nodes"]
