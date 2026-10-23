"""The support kit stays: SUPPORT.md exists, blank issues stay off, the issue forms stay well-formed.

Read as text on purpose (no YAML parser in the test deps), like tests/test_release_workflow.py: a form field is a
`  - type: X` block under `body:`, and each checks only the lines inside its own block.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
FORMS = ("bug_report.yml", "feature_request.yml")
SECURITY_URL = "https://github.com/LuminariSoftwares/stack-guardian/security/advisories/new"


def _blocks(text):
    """Every `  - type: X` block under `body:` as (type, lines)."""
    body = text.split("\nbody:\n", 1)[1]
    blocks, current = [], None
    for line in body.splitlines():
        m = re.match(r"^  - type: (\w+)\s*$", line)
        if m:
            current = (m.group(1), [])
            blocks.append(current)
        elif current is not None:
            current[1].append(line)
    return blocks


def _id(lines):
    m = [re.match(r"^    id: (\S+)\s*$", ln) for ln in lines]
    m = [x for x in m if x]
    return m[0].group(1) if m else None


def test_support_md_exists_and_promises_only_the_acknowledgement():
    text = (ROOT / "SUPPORT.md").read_text(encoding="utf-8")
    assert text.startswith("# Support")
    assert "acknowledgement within a day" in text
    assert "no Discord, mailing list or paid support" in text
    assert not re.search(r"\b(fix(ed)?|resolv\w+)\b[^.\n]{0,40}\bwithin\b", text, re.I), "SUPPORT.md must not promise a fix time"


def test_blank_issues_stay_disabled():
    config = (TEMPLATES / "config.yml").read_text(encoding="utf-8")
    assert re.search(r"^blank_issues_enabled: false\s*$", config, re.M), "blank issues must stay off"
    assert not re.search(r"blank_issues_enabled:\s*true", config)
    assert SECURITY_URL in config


def test_issue_forms_are_well_formed():
    for name in FORMS:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert re.search(r"^name: \S", text, re.M) and re.search(r"^description: \S", text, re.M), name
        blocks = _blocks(text)
        assert blocks, f"{name}: no body fields"
        ids = [_id(lines) for _, lines in blocks]
        assert all(ids), f"{name}: every field needs an id"
        assert len(ids) == len(set(ids)), f"{name}: duplicate field ids"
        for kind, lines in blocks:
            assert kind in {"markdown", "input", "textarea", "checkboxes", "dropdown"}, f"{name}: {kind}"
            if kind != "markdown":
                assert any(ln.strip() == "label:" or ln.strip().startswith("label: ") for ln in lines), f"{name}: {kind} has no label"


def test_bug_report_asks_for_what_support_md_promises():
    text = (TEMPLATES / "bug_report.yml").read_text(encoding="utf-8")
    assert re.search(r'^labels: \["needs-info"\]\s*$', text, re.M)
    blocks = {_id(lines): (kind, lines) for kind, lines in _blocks(text)}
    for field in ("version", "os", "python", "command", "output", "expected"):
        assert field in blocks, field
    kind, lines = blocks["quick-start"]
    assert kind == "checkboxes"
    joined = "\n".join(lines)
    assert "I ran the quick start as written" in joined
    assert re.search(r"^\s+required: true\s*$", joined, re.M), "the quick-start checkbox must be required"


def test_contributing_points_at_support():
    assert "(SUPPORT.md)" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert (ROOT / "CODE_OF_CONDUCT.md").is_file()
