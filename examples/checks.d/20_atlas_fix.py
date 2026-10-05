"""Example custom rule: when atlas does not answer, offer a typed fix (restart the declared lane)."""
from stack_guardian import finding as F


def atlas_stuck(snapshot, ctx):
    node = (snapshot.get("nodes") or {}).get("atlas") or {}
    if node.get("reachable"):
        return None
    return F.make_finding(
        "atlas.stuck", "missing", "WARN", "atlas", "atlas does not answer",
        evidence=[str(node.get("error", "no reply"))],
        try_=["restart the atlas Ollama lane: doctor --fix atlas.stuck --config examples/lanes.json --apply"],
        fix={"action": "restart_lane", "args": {"lane": "atlas_ollama"}},
    )


CHECKS = [("atlas.stuck", atlas_stuck)]
