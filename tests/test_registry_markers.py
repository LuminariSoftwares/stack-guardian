"""The official MCP Registry markers: mcpName in dsh/package.json and the mcp-name line in the README PyPI receives."""
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "io.github.luminarisoftwares/stack-guardian"


def test_registry_markers_exist_and_agree():
    package = json.loads((ROOT / "dsh" / "package.json").read_text(encoding="utf-8"))
    with open(ROOT / "pyproject.toml", "rb") as fh:
        readme_file = tomllib.load(fh)["project"]["readme"]
    readme = (ROOT / readme_file).read_text(encoding="utf-8")  # the file setuptools ships as the PyPI description
    lines = [ln for ln in readme.splitlines() if ln.startswith("mcp-name:")]
    assert lines == ["mcp-name: " + NAME], lines
    assert package.get("mcpName") == NAME
    assert re.fullmatch(r"mcp-name: (\S+)", lines[0]).group(1) == package["mcpName"]
    # near the top: before the first section heading
    assert readme.index(lines[0]) < readme.index("\n## ")
