"""run_agent.py -- one agent run: build the sandbox, start the service, run the agent, keep the record.

    python -m examples.dse_cg.run_agent --arm waveflow --objective lut_at_mse20_cyc4000 --seed 3

A run is the user flow (README, *Using it yourself*) in a fresh session with no context of its own.

1. ``brief.build_sandbox`` into a fresh directory under the run root.
2. A ``service.Service`` for this design, arm, objective and budget (``prereg.py``), on the replay
   backend; ``service.serve`` at an address passed to the agent's environment as ``DSE_SERVICE``.
3. The agent, headless, with the sandbox as its working directory: file and shell tools inside it,
   no network but the API and the service, the pre-registered model and settings, the same in both
   arms.  Project settings only (the sandbox's ``.claude/skills``), never the user's ``~/.claude``:
   no other CLAUDE.md, memory or skills reach the agent.  The prompt is ``brief.prompt("DSE.md",
   objective)``, the line a user would type.
4. On ``submit``, on an exhausted budget, or on a turn limit: stop.  Keep the transcript, the token
   counts, the ledger, and the sandbox as the agent left it.  Check from the transcript that the
   skill loaded; a run where it did not is void, not scored.

Before writing step 3, load the ``claude-api`` skill rather than working from memory: the Agent SDK's
options (settings sources, skills, tools) and the model id are easy to get subtly wrong.

Skeleton (plan step 7).
"""

from __future__ import annotations

import argparse
from pathlib import Path


def run_one(arm: str, objective_id: str, seed: int, root: Path) -> Path:
    """Run one agent and return its record directory."""
    raise NotImplementedError("step 7")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", choices=("traditional", "waveflow"), required=True)
    ap.add_argument("--objective", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--root", type=Path, default=Path("dse_runs"))
    a = ap.parse_args(argv)
    run_one(a.arm, a.objective, a.seed, a.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
