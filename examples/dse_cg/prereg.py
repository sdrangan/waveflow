"""prereg.py -- what is fixed before any scored run (plan ``plans/dse_cg.md``, *Fairness rules* 4).

Committed in one commit before the first scored run, and not edited after; a change means a new
pre-registration with a new date, and the old runs stay scored against the old one.  The grid G1 --
the design's whole space (``ltbf_cg_mem.GRID``), resized once by the plan's rule on Phase 2c's
measured costs -- and its integration builds are pre-registered separately, before any of their
builds run (plan step 4).

Python rather than JSON: the repository's ``.gitignore`` ignores ``*.json``.

Skeleton: every value below is a placeholder, marked TBD.
"""

from __future__ import annotations

#: The date of this pre-registration.  TBD
DATE = None

#: The design (``ltbf_cg_mem.DESIGNS``).
DESIGN = "ltbf_cg_mem"

#: The objectives; each run gets one.  Several, so that no single answer can be learned.  TBD
OBJECTIVES = [
    {
        "id": "lut_at_loss01_tlt10",
        # TBD: the T_upd bound, from the measured grid's range (one sector's update at N_rx 64, 4
        # lanes, factored, t 3 in the 2025.1 study: 6.6 ms as built, 4.5 ms with a one-word-a-cycle
        # Shared Mem).
        "text": "Minimize LUTs subject to median loss <= 0.1 dB at SINR targets [-6, 3] dB and T_upd <= 6 ms.",
        "minimize": "lut",
        "subject_to": [
            {"metric": "median_loss_db", "workload": "accuracy", "at": {"target": "-6_3"}, "op": "<=", "value": 0.1},
            {"metric": "t_upd_ms", "workload": "long_term", "op": "<=", "value": 6},
        ],
    },
    # TBD: two or three more (the other SINR targets; minimize T_upd under a LUT budget; ...)
]

#: Budget per run, tool-seconds.  TBD: a fraction of the grid's measured cost (plan step 7), so that
#: exhausting the grid is not an option.
BUDGET_S = None

#: Worker slots for synth / rtlsim jobs: the lab machine's limit for heavy jobs (plan, 2026-11-06).
K_WORKERS = 2

#: "Within EPSILON of the optimum" for time-to-target, as a fraction of the objective.  TBD
EPSILON = 0.05

#: Runs per arm per objective.
N_RUNS = 10

#: The agent's model and settings, identical in both arms.  TBD
MODEL = None

#: How calibration cost enters the Waveflow arm: reported both ways, headline "upfront".
CALIBRATION_CHARGE = ("upfront", "amortized")
