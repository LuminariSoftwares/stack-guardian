"""Example custom rule: when gpu-box does not answer, offer a typed fix (restart the declared lane)."""
from stack_guardian import finding as F


def ollama_stuck(snapshot, ctx):
    node = (snapshot.get("nodes") or {}).get("gpu-box") or {}
    if node.get("reachable"):
        return None
    return F.make_finding(
        "ollama.stuck", "missing", "WARN", "gpu-box", "gpu-box does not answer",
        evidence=[str(node.get("error", "no reply"))],
        try_=["restart the gpu-box Ollama lane: doctor --fix ollama.stuck --config examples/lanes.json --apply"],
        fix={"action": "restart_lane", "args": {"lane": "gpu_box_ollama"}},
    )


CHECKS = [("ollama.stuck", ollama_stuck)]
