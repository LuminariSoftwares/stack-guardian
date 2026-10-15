"""Glama scores each MCP tool on how its description and parameters read. Keep them informative: say when to use
the tool and when to use a sibling instead, document every parameter, and keep the README's MCP section complete."""
import json
import re
from pathlib import Path

from stack_guardian import __version__
from stack_guardian.mcp_server import TOOL_SPECS

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
NAMES = [t["name"] for t in TOOL_SPECS]


def test_each_description_routes_to_its_siblings():
    for tool in TOOL_SPECS:
        desc = tool["description"]
        assert len(desc) >= 200, tool["name"]
        assert "Use " in desc or "Call " in desc, tool["name"]
        for other in NAMES:
            if other != tool["name"]:
                assert other in desc, (tool["name"], other)


def test_every_parameter_is_documented_with_an_example():
    for tool in TOOL_SPECS:
        for pname, spec in tool["inputSchema"]["properties"].items():
            assert len(spec.get("description", "")) >= 60, (tool["name"], pname)
            assert "e.g." in spec["description"] or "for example" in spec["description"], (tool["name"], pname)


def test_readme_status_matches_the_package_version():
    assert "Status: **%s" % __version__ in README
    assert "0.1.0" not in README


def test_readme_mcp_section_is_complete():
    section = README[README.index("## MCP server"):]
    section = section[:section.index("\n## ", 1)]
    assert "uvx stack-guardian mcp" in section
    for name in NAMES:
        assert "`%s`" % name in section, name
    block = re.search(r"```json\n(.*?)```", section, re.S).group(1)
    server = json.loads(block)["mcpServers"]["stack-guardian"]
    assert server["command"] == "uvx" and server["args"][:2] == ["stack-guardian", "mcp"]
    assert server["env"]["STACK_GUARDIAN_NODES"]
    assert "mcp-name: io.github.LuminariSoftwares/stack-guardian" in README.splitlines()


def test_readme_license_section_names_the_licence_file():
    lic = README[README.index("## License"):]
    assert "MIT" in lic and "LICENSE" in lic
