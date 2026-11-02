"""policies.py -- scripted explorers, one per arm: the agent-free comparison.

They drive the service through the same ``dselib`` calls an agent makes, so their ledgers are scored by
the same ``score.py``.  The vision asks that the core result not depend on an agent (an agent adds
variance); these give that result, and the agent runs show the advantage survives an autonomous
explorer.

Skeleton: the algorithms are stated, not written (plan step 6).
"""

from __future__ import annotations


def traditional(lib, objective: dict, analytic_cost) -> dict:
    """The systems-team + hardware-team flow.

    1. Sweep accuracy with ``lib.golden`` over the design points that affect it; keep the points that
       meet the accuracy constraints.
    2. Rank them by ``analytic_cost(params)``: a written estimate of the objective (for example LUTs
       from multiplier counts, cycles from trip counts), the spreadsheet a hardware team keeps,
       refined with ``lib.hls_estimate`` on the leading candidates (the kernels' csynth reports).
    3. ``synth`` + ``rtlsim`` them in that order, a batch of ``k`` at a time, until the constraints
       are met and the best verified objective has not improved for one batch.
    4. ``submit`` the best verified design.
    """
    raise NotImplementedError("step 6")


def waveflow(lib, objective: dict, top_k: int) -> dict:
    """The same flow with pysim.

    1. ``lib.pysim`` every point of the space (accuracy exact; cycles and resources predicted).
    2. Keep the predicted-feasible points; rank them by the predicted objective.
    3. ``synth`` + ``rtlsim`` the top ``top_k``; while a measurement disagrees with its prediction
       enough to change the ranking, widen to the next ``top_k``.
    4. ``submit`` the best verified design.
    """
    raise NotImplementedError("step 6")
