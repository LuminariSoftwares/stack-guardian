"""Validation for the nodes file, the one place a user describes their machines.

A nodes file is a JSON object of node name -> settings:

    {"gpu-box": {"ollama": ["http://127.0.0.1:11434"], "local": true},
     "studio":  {"kind": "openai", "endpoints": ["http://127.0.0.1:1234"]}}

Keys whose name starts with "_" are ignored, so a file can carry comments and
candidate nodes it is not using yet; "_options" is read for run settings
(currently ``timeout_s``).  Everything here is pure except the one-line stderr
note a tolerated shape prints: a file that cannot be used raises NodesError, and
the callers turn that into a usage error rather than a crash or a silent OK.
"""

import json
import sys

OPTIONS_KEY = "_options"
DEFAULT_TIMEOUT_S = 10.0
TTFT_TIMEOUT_S = 30.0
KINDS = {"ollama": "ollama", "openai": "endpoints"}  # kind -> the key holding its base URLs
EXAMPLE = '{"gpu-box": {"ollama": ["http://127.0.0.1:11434"], "local": true}}'


class NodesError(ValueError):
    """The nodes file cannot be used as written. The message names the node and the shape it needs."""


def _warn(line):
    print("stack-guardian: " + line, file=sys.stderr)


def _kind_of(value):
    return {dict: "object", list: "list", str: "string", int: "number", float: "number", bool: "boolean"}.get(type(value), "null")


def _urls(name, key, value, warn):
    """The base URLs of one node: a single string is accepted as a one-element list, with a note on stderr."""
    if isinstance(value, str):
        warn('node "%s": "%s" is one URL string, not a list; reading it as ["%s"]' % (name, key, value))
        value = [value]
    if value is None:
        return []
    if not isinstance(value, list):
        raise NodesError('node "%s": "%s" must be a list of base URLs, for example ["http://127.0.0.1:11434"], not a %s'
                         % (name, key, _kind_of(value)))
    for url in value:
        if not isinstance(url, str) or not url.strip():
            raise NodesError('node "%s": every entry of "%s" must be a base URL string, for example "http://127.0.0.1:11434"' % (name, key))
        if not (url.startswith("http://") or url.startswith("https://")):
            raise NodesError('node "%s": "%s" entry %r needs a scheme, for example "http://%s"' % (name, key, url, url))
    return list(value)


def _node(name, cfg, warn):
    """One validated node config, or NodesError naming the node and the shape it needs."""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise NodesError('node "%s" must be an object, for example %s, not a %s' % (name, EXAMPLE, _kind_of(cfg)))
    kind = cfg.get("kind", "ollama")
    if kind not in KINDS:
        raise NodesError('node "%s": "kind" is %r; it is "ollama" (the default) or "openai"' % (name, kind))
    key, other = KINDS[kind], KINDS["ollama" if kind == "openai" else "openai"]
    if other in cfg:
        raise NodesError('node "%s" has "%s" but kind is "%s": an Ollama node is {"ollama": [...]}, an '
                         'OpenAI-compatible node (LM Studio, vLLM, llama.cpp) is {"kind": "openai", "endpoints": [...]}'
                         % (name, other, kind))
    urls = _urls(name, key, cfg.get(key), warn)
    if not urls and not cfg.get("local") and not cfg.get("tailnet_host"):
        raise NodesError('node "%s" has nothing to probe: give it "%s": ["http://HOST:PORT"], "local": true for this '
                         'machine\'s GPUs, or "tailnet_host": "NAME" for a tailnet peer' % (name, key))
    return dict(cfg, **{key: urls})


def _options(value):
    """The validated "_options" object: {"timeout_s": float} today."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NodesError('"%s" must be an object, for example {"timeout_s": 20}, not a %s' % (OPTIONS_KEY, _kind_of(value)))
    out = {}
    if "timeout_s" in value:
        timeout = value["timeout_s"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise NodesError('"%s": "timeout_s" must be a number of seconds greater than 0, for example 20' % OPTIONS_KEY)
        out["timeout_s"] = float(timeout)
    return out


def validate(obj, warn=None):
    """(nodes, options) from a parsed nodes file; NodesError when it cannot be used as written."""
    warn = warn or _warn
    if not isinstance(obj, dict):
        raise NodesError("a nodes file is a JSON object of node name -> settings, for example %s; this one is a %s"
                         % (EXAMPLE, _kind_of(obj)))
    nodes = {name: _node(name, cfg, warn) for name, cfg in obj.items() if not name.startswith("_")}
    if not nodes:
        raise NodesError("the nodes file names no nodes (a key starting with \"_\" is a comment): add one, for example %s" % EXAMPLE)
    return nodes, _options(obj.get(OPTIONS_KEY))


def load(path, warn=None):
    """(nodes, options) read from a nodes file. A JSON syntax error raises ValueError; an unusable file, NodesError."""
    with open(path, encoding="utf-8") as fh:
        return validate(json.load(fh), warn=warn)


def timeout_s(options, override=None, ttft=False):
    """The wall-clock deadline for one collection: --timeout, else the nodes file, else the default."""
    if override is not None:
        return float(override)
    if options and options.get("timeout_s"):
        return float(options["timeout_s"])
    return TTFT_TIMEOUT_S if ttft else DEFAULT_TIMEOUT_S


def _selftest():
    said = []

    def warn(line):
        said.append(line)

    def refuses(obj):
        try:
            validate(obj, warn=warn)
        except NodesError:
            return True
        except Exception:  # noqa: BLE001 - any other exception is the crash this module exists to prevent
            return False
        return False

    def c_plain_node():
        nodes, options = validate({"gpu-box": {"ollama": ["http://127.0.0.1:11434"], "local": True}}, warn=warn)
        return nodes["gpu-box"]["ollama"] == ["http://127.0.0.1:11434"] and options == {}

    def c_string_url_becomes_a_list():
        said.clear()
        nodes, _ = validate({"gpu-box": {"ollama": "http://127.0.0.1:11434"}}, warn=warn)
        return nodes["gpu-box"]["ollama"] == ["http://127.0.0.1:11434"] and len(said) == 1 and "one URL string" in said[0]

    def c_endpoints_without_kind_refused():
        return refuses({"gpu-box": {"endpoints": ["http://127.0.0.1:1234"]}})

    def c_openai_node_ok_and_ollama_key_refused():
        nodes, _ = validate({"studio": {"kind": "openai", "endpoints": ["http://127.0.0.1:1234"]}}, warn=warn)
        return nodes["studio"]["endpoints"] == ["http://127.0.0.1:1234"] and refuses({"studio": {"kind": "openai", "ollama": ["http://x:1"]}})

    def c_nothing_to_probe_refused():
        return refuses({"gpu-box": {}}) and refuses({"gpu-box": {"ollama": []}}) and refuses({"gpu-box": None})

    def c_gpu_or_tailnet_only_node_is_fine():
        a, _ = validate({"gpu-box": {"local": True}}, warn=warn)
        b, _ = validate({"peer": {"tailnet_host": "peer"}}, warn=warn)
        return a["gpu-box"]["ollama"] == [] and b["peer"]["ollama"] == []

    def c_top_level_list_refused():
        return refuses([{"gpu-box": {"ollama": ["http://x:1"]}}]) and refuses("gpu-box") and refuses(None)

    def c_bad_urls_refused():
        return (refuses({"a": {"ollama": ["127.0.0.1:11434"]}}) and refuses({"a": {"ollama": [1]}})
                and refuses({"a": {"ollama": {"url": "http://x"}}}) and refuses({"a": {"kind": "vllm", "ollama": ["http://x:1"]}}))

    def c_underscore_keys_are_comments():
        nodes, _ = validate({"_note": "candidates below", "_candidates": {"peer": {}}, "gpu-box": {"local": True}}, warn=warn)
        return list(nodes) == ["gpu-box"]

    def c_options_and_timeout():
        _, options = validate({"_options": {"timeout_s": 20}, "gpu-box": {"local": True}}, warn=warn)
        return (options == {"timeout_s": 20.0} and timeout_s(options) == 20.0 and timeout_s(options, override=3) == 3.0
                and timeout_s({}) == DEFAULT_TIMEOUT_S and timeout_s({}, ttft=True) == TTFT_TIMEOUT_S
                and refuses({"_options": {"timeout_s": 0}, "a": {"local": True}})
                and refuses({"_options": [], "a": {"local": True}}))

    def c_empty_file_refused():
        return refuses({}) and refuses({"_note": "nothing here"})

    checks = [("plain_node", c_plain_node), ("string_url_becomes_a_list", c_string_url_becomes_a_list),
              ("endpoints_without_kind_refused", c_endpoints_without_kind_refused),
              ("openai_node_ok_and_ollama_key_refused", c_openai_node_ok_and_ollama_key_refused),
              ("nothing_to_probe_refused", c_nothing_to_probe_refused),
              ("gpu_or_tailnet_only_node_is_fine", c_gpu_or_tailnet_only_node_is_fine),
              ("top_level_list_refused", c_top_level_list_refused), ("bad_urls_refused", c_bad_urls_refused),
              ("underscore_keys_are_comments", c_underscore_keys_are_comments),
              ("options_and_timeout", c_options_and_timeout), ("empty_file_refused", c_empty_file_refused)]
    passed = failed = 0
    for name, fn in checks:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            ok = False
            print("FAIL %s -- raised %s: %s" % (name, type(exc).__name__, exc))
        else:
            print(("ok %s" if ok else "FAIL %s") % name)
        passed += ok
        failed += not ok
    print("nodes selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
    return 0 if failed == 0 and checks else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 2)
