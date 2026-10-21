"""Every MCP tool and parameter is fully documented, and the README's tool reference is generated from these schemas.

Glama's Tool Definition Quality Score reads exactly these fields, and so does every agent choosing between the tools,
so a missing description, example or constraint fails here rather than in a listing."""
import importlib.util
import re
from pathlib import Path

import pytest

from stack_guardian.mcp_server import EXPLAIN, TOOL_SPECS

ROOT = Path(__file__).resolve().parent.parent
NAMES = [t["name"] for t in TOOL_SPECS]
PARAMS = [(t["name"], p, spec) for t in TOOL_SPECS for p, spec in t["inputSchema"]["properties"].items()]


def _tool_reference():
    spec = importlib.util.spec_from_file_location("tool_reference", ROOT / "scripts" / "tool_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("tool", TOOL_SPECS, ids=NAMES)
def test_tool_has_every_documented_part(tool):
    assert re.fullmatch(r"[a-z]+(_[a-z]+)*", tool["name"]), "tool names are lower snake_case"
    assert tool["title"].strip() and tool["annotations"]["title"] == tool["title"]
    desc = tool["description"]
    for part in ("When to use:", "Behaviour:", "Returns JSON:"):
        assert part in desc, (tool["name"], part)
    assert "read-only" in desc or "no side effects" in desc, tool["name"]
    for other in NAMES:
        if other != tool["name"]:
            assert other in desc, (tool["name"], "should say when to use", other)
    assert len(desc) <= 1200, "keep descriptions concise: %d chars" % len(desc)
    schema = tool["inputSchema"]
    assert schema["type"] == "object" and schema.get("additionalProperties") is False
    assert set(schema.get("required", [])) <= set(schema["properties"])


@pytest.mark.parametrize("tool,name,spec", PARAMS, ids=["%s.%s" % (t, p) for t, p, _ in PARAMS])
def test_parameter_has_every_documented_part(tool, name, spec):
    required = name in next(t for t in TOOL_SPECS if t["name"] == tool)["inputSchema"].get("required", [])
    assert spec.get("type") == "string"
    desc = spec.get("description", "")
    assert len(desc) >= 60, "parameter needs a real description"
    assert desc.startswith("Required." if required else "Optional."), desc[:20]
    assert "for example" in desc
    if not required:
        assert "default" in spec and "Default:" in desc
    assert "maxLength" in spec, "every string parameter is bounded"
    examples = spec.get("examples")
    assert examples, "parameter needs examples"
    for ex in examples:
        assert isinstance(ex, str) and len(ex) <= spec["maxLength"] and len(ex) >= spec.get("minLength", 0)
        if "pattern" in spec:
            assert re.fullmatch(spec["pattern"], ex), ex


def test_every_shipped_finding_code_can_be_explained():
    rules = (ROOT / "stack_guardian" / "checks.d" / "10_core.py").read_text(encoding="utf-8")
    shipped = set(re.findall(r'"([a-z_]+\.[a-z_]+)"', rules)) | {"check.crashed"}
    assert shipped, "no codes found in the shipped rules"
    assert shipped <= set(EXPLAIN), sorted(shipped - set(EXPLAIN))
    pattern = TOOL_SPECS[NAMES.index("explain")]["inputSchema"]["properties"]["code"]["pattern"]
    assert all(re.fullmatch(pattern, code) for code in EXPLAIN)


def test_readme_tool_reference_is_generated_from_the_schemas():
    ref = _tool_reference()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert ref.splice(readme, ref.render()) == readme, "run: python scripts/tool_reference.py --write"
    for name in NAMES:
        assert "#### `%s`" % name in readme
