"""The micro-scheduler example's simulated outcome is pinned (``plans/sim_speed_cpu_linalg.md``, step 2).

``docs/evaluation/sim_speed/cpu.md`` times the whole ``examples/cpu_sched`` system -- the scheduler on
the A53 model, its accelerators behind AXI-Lite -- and makes the processor model faster.  This pins
what that system simulates, unoptimized: every :class:`~waveflow.cpu.task.TaskRecord` and every job's
arrival, completion, result and dispatch count, at the example's default inter-arrival (0.6 us) and at
the 0.8 us the speed page uses (``tests/cpu/_outcomes.py``).

Re-record only for a change that is *meant* to change the outcome:
``python -m tests.examples.test_cpu_sched_outcomes --record``.
"""

from __future__ import annotations

import pytest

from examples.cpu_sched.cpu_sched import run
from tests.cpu import _outcomes as O

SECTION = "cpu_sched"
N_JOBS = 200
#: The inter-arrival times pinned: the example's default, and the speed page's (flat work per job).
INTERARRIVALS = {"ia_0.6us": 0.6e-6, "ia_0.8us": 0.8e-6}
JOB_FIELDS = ("gid", "prio", "x", "a", "b", "t_arrive", "t_done", "y", "dispatches")


def outcome(interarrival_s: float) -> dict:
    res = run(N_JOBS, 2, mean_interarrival_s=interarrival_s)
    assert res.all_correct
    jobs = [res.jobs[g] for g in sorted(res.jobs)]
    return {
        **O.fingerprint(res.records),
        "sim_s": res.sim_s,
        "jobs": O.fingerprint(jobs, JOB_FIELDS, per_record=False),
    }


@pytest.mark.parametrize("key", list(INTERARRIVALS))
def test_the_example_outcome_is_pinned(key):
    got = outcome(INTERARRIVALS[key])
    want = O.load(SECTION)[key]
    O.assert_same(got, want, f"cpu_sched {key} task records")
    O.assert_same(got["jobs"], want["jobs"], f"cpu_sched {key} jobs")
    assert got["sim_s"] == want["sim_s"]


def record() -> None:
    O.save(SECTION, {k: outcome(ia) for k, ia in INTERARRIVALS.items()})


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--record"]:
        sys.exit("usage: python -m tests.examples.test_cpu_sched_outcomes --record")
    record()
    print(f"recorded section '{SECTION}' of {O.FIXTURE}")
