"""prereg.py -- what is fixed before any scored run (plan ``plans/dse_cg.md``, *Fairness rules* 4).

Committed in one commit before the first scored run, and not edited after; a change means a new
pre-registration with a new date, and the old runs stay scored against the old one.  The ground-truth
grid G1 and its integration builds are pre-registered separately, before any of their builds run
(plan step 4).

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
        "text": "Minimize LUTs subject to median loss <= 0.1 dB at SINR targets [-6, 3] dB and T_LT <= 10 ms.",
        "minimize": "lut",
        "subject_to": [
            {"metric": "median_loss_db", "workload": "accuracy", "at": {"target": "-6_3"}, "op": "<=", "value": 0.1},
            {"metric": "t_lt_ms", "workload": "long_term", "op": "<=", "value": 10},
        ],
    },
    # TBD: two or three more (the other SINR targets; minimize T_LT under a LUT budget; ...)
]

#: Budget per run, tool-seconds.  TBD (a fraction of the full grid's cost, so that exhausting
#: the grid is not an option)
BUDGET_S = None

#: Worker slots for synth / rtlsim jobs (what the lab machine runs at once).  TBD
K_WORKERS = None

#: "Within EPSILON of the optimum" for time-to-target, as a fraction of the objective.  TBD
EPSILON = 0.05

#: Runs per arm per objective.
N_RUNS = 10

#: The agent's model and settings, identical in both arms.  TBD
MODEL = None

#: How calibration cost enters the Waveflow arm: reported both ways, headline "upfront".
CALIBRATION_CHARGE = ("upfront", "amortized")
