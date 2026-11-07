"""prereg.py -- what is fixed before any scored run (plan ``plans/dse_cg.md``, *Fairness rules* 4).

Committed in one commit before the first scored run, and not edited after; a change means a new
pre-registration with a new date, and the old runs stay scored against the old one.  The grid G1 --
the design's whole space (``ltbf_cg_mem.GRID``), resized once by the plan's rule on Phase 2c's
measured costs -- and its integration builds are pre-registered separately, before any of their
builds run (plan step 4).

Python rather than JSON: the repository's ``.gitignore`` ignores ``*.json``.

The objectives are fixed (step 4, :data:`OBJECTIVES_DATE`); every other value is a placeholder,
marked TBD, fixed at step 7.
"""

from __future__ import annotations

#: The date of this pre-registration.  TBD
DATE = None

#: The design (``ltbf_cg_mem.DESIGNS``).
DESIGN = "ltbf_cg_mem"

#: The date the objectives were fixed: plan step 4, chosen by the user before any result of G1 was
#: examined (the rest of this file is fixed at step 7).
OBJECTIVES_DATE = "2026-11-07"

#: The objectives; each run gets one.  Three, each binding a different constraint -- the memory axis
#: through ``T_upd``, ``W`` through accuracy, the shapes through resources -- so that no single answer
#: can be learned.  ``T_upd`` is one sector's long-term update (``long_term``) at the stated ``t``; the
#: accuracy at the same ``t`` and the SINR targets [-6, 3] dB; LUT and DSP are the built parts'
#: synthesized totals.
OBJECTIVES = [
    {
        "id": "lut_tupd5_loss01",
        "text": "Minimize LUTs subject to T_upd <= 5 ms (t = 3) and median SINR loss <= 0.1 dB at SINR "
                "targets [-6, 3] dB.",
        "minimize": "lut",
        "subject_to": [
            {"metric": "t_upd_ms", "workload": "long_term", "at": {"t": 3}, "op": "<=", "value": 5},
            {"metric": "median_loss_db", "workload": "accuracy", "at": {"t": 3, "target": "-6_3"},
             "op": "<=", "value": 0.1},
        ],
    },
    {
        "id": "tupd_lut100k_loss05",
        "text": "Minimize T_upd (t = 3) subject to LUTs <= 100,000 and median SINR loss <= 0.5 dB at SINR "
                "targets [-6, 3] dB.",
        "minimize": "t_upd_ms",
        "workload": {"name": "long_term", "t": 3},
        "subject_to": [
            {"metric": "lut", "op": "<=", "value": 100_000},
            {"metric": "median_loss_db", "workload": "accuracy", "at": {"t": 3, "target": "-6_3"},
             "op": "<=", "value": 0.5},
        ],
    },
    {
        "id": "dsp_tupd4_t2_loss03",
        "text": "Minimize DSPs subject to T_upd <= 4 ms (t = 2) and median SINR loss <= 0.3 dB at SINR "
                "targets [-6, 3] dB (t = 2).",
        "minimize": "dsp",
        "subject_to": [
            {"metric": "t_upd_ms", "workload": "long_term", "at": {"t": 2}, "op": "<=", "value": 4},
            {"metric": "median_loss_db", "workload": "accuracy", "at": {"t": 2, "target": "-6_3"},
             "op": "<=", "value": 0.3},
        ],
    },
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

#: The agent's model, identical in both arms.  TBD
MODEL = None

#: The agent's effort (the Agent SDK's ``effort``), identical in both arms.  TBD
EFFORT = None

#: The turn limit of a run (the Agent SDK's ``max_turns``).  TBD
MAX_TURNS = None

#: The wall-clock limit of a run, seconds: a guard against a stuck session (the budget is tool time,
#: on the service's clock).  TBD
MAX_WALL_S = None

#: The agent's container (``agent/Dockerfile``): what its file tools and shell can see.  Its image
#: digest is recorded here when the pre-registration is frozen.
AGENT_IMAGE = "waveflow-dse-agent:1"

#: The harness: the Agent SDK and the Claude Code it bundles (``run_agent.py``).
AGENT_SDK = "claude-agent-sdk 0.2.179 (Claude Code 2.1.319)"

#: How calibration cost enters the Waveflow arm: reported both ways, headline "upfront".
CALIBRATION_CHARGE = ("upfront", "amortized")
