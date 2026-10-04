"""stack-guardian: a health doctor for a home AI stack (Ollama nodes, GPUs, tailnet, harness logs).

Check files (checks.d/*.py) export ``CHECKS = [(check_id, fn(snapshot, ctx) -> finding | None)]`` and build findings with
``stack_guardian.finding.make_finding``. ``import sg_finding`` keeps working inside a check file: the package registers
that name for checks written against the flat module.
"""
import sys

from . import finding

__version__ = "0.1.0.dev0"

sys.modules.setdefault("sg_finding", finding)
