"""The README documents each 2026-10-14 feature with its exact flag, and the agents section names /metrics and --watch."""
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_readme_documents_new_features():
    agents = README[README.index("## For coding agents"):README.index("## What it does")]
    assert "/metrics" in agents and "--watch" in agents
    for needle in ('"kind": "openai"', "model.expiring", "--watch 30", "--ttft", "model.slow_ttft",
                   "curl http://127.0.0.1:8788/metrics"):
        assert needle in README, needle
