"""The report at the top of the README is the real output of examples/demo_report.py, not a hand-written one."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START, END = "<!-- demo-report:start -->", "<!-- demo-report:end -->"


def readme_block():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    block = text[text.index(START) + len(START):text.index(END)].strip("\n")
    lines = block.split("\n")
    assert lines[0] == "```" and lines[-1] == "```", "the report is one fenced block between the markers"
    return "\n".join(lines[1:-1])


def test_readme_report_is_the_script_output():
    run = subprocess.run([sys.executable, str(ROOT / "examples" / "demo_report.py")],
                         capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert run.returncode == 0, run.stderr
    assert readme_block() == run.stdout.rstrip("\n")


def test_report_shows_ok_and_problems_with_both_grades():
    block = readme_block()
    assert "\nOK " in block and ("\nFAIL " in block or "\nWARN " in block)
    assert "(LIKELY)" in block and "(VERIFIED)" in block
