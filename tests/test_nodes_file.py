"""The nodes file is validated on load, because every shape below used to produce a confident wrong answer.

Each test failed on main f73bebf:
  * "ollama": "http://..." (a string) was iterated per character -> "18 Ollama endpoint(s) do not answer: h, t, t, p"
  * "endpoints" without "kind": "openai", and a node with nothing to probe, reported worst: OK and exit 0
  * a top-level JSON list crashed with AttributeError and exit 4
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stack_guardian import nodes as sg_nodes

ROOT = Path(__file__).resolve().parent.parent
DEAD = "http://127.0.0.1:9"  # nothing listens on the discard port: refused at once


def _env():
    return {k: v for k, v in os.environ.items() if not k.startswith("STACK_GUARDIAN_")}


def _doctor(tmp_path, doc, *extra):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return subprocess.run([sys.executable, "-m", "stack_guardian", "doctor", "--nodes", str(path),
                           "--state", str(tmp_path / "s.json"), *extra],
                          capture_output=True, text=True, timeout=120, env=_env(), cwd=str(ROOT))


def test_a_single_url_string_is_read_as_one_endpoint(tmp_path):
    """A string where a list belongs is tolerated, said out loud once, and probed once -- not once per character."""
    r = _doctor(tmp_path, {"gpu-box": {"ollama": DEAD}}, "--json")
    assert r.returncode == 2, r.stdout + r.stderr
    findings = json.loads(r.stdout)["findings"]
    assert [f["code"] for f in findings] == ["ollama.unreachable"], findings
    assert "1 Ollama endpoint(s)" in findings[0]["summary"], findings[0]["summary"]
    assert r.stderr.count("one URL string") == 1, r.stderr


def test_endpoints_without_the_openai_kind_is_a_usage_error(tmp_path):
    r = _doctor(tmp_path, {"gpu-box": {"endpoints": [DEAD]}})
    assert r.returncode == 3, r.stdout + r.stderr
    assert "gpu-box" in r.stderr and '"kind": "openai"' in r.stderr


def test_a_node_with_nothing_to_probe_is_a_usage_error(tmp_path):
    r = _doctor(tmp_path, {"gpu-box": {"ollama": []}, "other": {"ollama": [DEAD]}})
    assert r.returncode == 3, r.stdout + r.stderr
    assert "gpu-box" in r.stderr and "nothing to probe" in r.stderr


def test_a_top_level_list_is_a_usage_error_not_a_crash(tmp_path):
    r = _doctor(tmp_path, [{"gpu-box": {"ollama": [DEAD]}}])
    assert r.returncode == 3, r.stdout + r.stderr
    assert "crash" not in r.stderr and "JSON object" in r.stderr


def test_an_openai_node_still_works(tmp_path):
    r = _doctor(tmp_path, {"studio": {"kind": "openai", "endpoints": [DEAD]}}, "--json")
    assert r.returncode == 2, r.stdout + r.stderr
    assert [f["code"] for f in json.loads(r.stdout)["findings"]] == ["ollama.unreachable"]


def test_underscore_keys_are_comments_and_options_sets_the_deadline(tmp_path):
    doc = {"_README": "this line is ignored", "_options": {"timeout_s": 4}, "gpu-box": {"ollama": [DEAD]}}
    nodes, options = sg_nodes.validate(doc, warn=lambda _line: None)
    assert list(nodes) == ["gpu-box"] and options == {"timeout_s": 4.0}
    assert sg_nodes.timeout_s(options) == 4.0 and sg_nodes.timeout_s(options, override=2) == 2.0
    r = _doctor(tmp_path, doc, "--json")
    assert r.returncode == 2, r.stdout + r.stderr


@pytest.mark.parametrize("doc", [
    {"gpu-box": {"ollama": ["127.0.0.1:11434"]}},        # no scheme
    {"gpu-box": {"ollama": [None]}},                      # not a string
    {"gpu-box": "http://127.0.0.1:11434"},                # the node itself is a string
    {"gpu-box": {"kind": "vllm", "ollama": [DEAD]}},      # unknown kind
    {"_options": {"timeout_s": -1}, "gpu-box": {"ollama": [DEAD]}},
    {},
])
def test_other_unusable_files_name_the_problem(doc, tmp_path):
    r = _doctor(tmp_path, doc)
    assert r.returncode == 3, (doc, r.stdout, r.stderr)
    assert r.stderr.startswith("stack-guardian: usage:"), r.stderr
