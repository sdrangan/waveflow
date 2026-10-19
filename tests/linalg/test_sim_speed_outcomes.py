"""The linalg units' simulated outcome is pinned (``plans/sim_speed_cpu_linalg.md``, step 6).

``docs/evaluation/sim_speed/linalg.md`` makes the pysim of the systolic unit and of the CG vector unit
faster; a speed-up is one only if the simulation still does the same thing.  Ten full-size jobs of
each unit, in its memory-fed bench at the centre configuration (``tests/linalg/_sim_speed.py``), are
pinned to their unoptimized outcome: the simulated time every reply reaches ``s_done``, a digest of
every reply's words, and the time the run ends -- with every reply bit-exact.  Firing times are not
pinned: a free-running module records them only with a timing model attached.

Re-record only for a change that is *meant* to change the outcome:
``python -m tests.linalg.test_sim_speed_outcomes --record``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.linalg import _sim_speed as S

FIXTURE = Path(__file__).resolve().parent / "data" / "sim_speed_outcomes.json"
N_JOBS = 10
SEED = 6


@pytest.mark.parametrize("block", S.BLOCKS)
def test_the_unit_outcome_is_pinned(block):
    got = S.outcome(block, N_JOBS, SEED)
    assert got["exact"], f"{block}: a reply differs from the model"
    assert got["replies"] == N_JOBS * S.replies_per_job(block)
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))[block]
    assert got["arrivals"] == want["arrivals"], (
        f"{block}: reply arrival times moved; first at index "
        f"{next(i for i, (a, b) in enumerate(zip(got['arrivals'], want['arrivals'])) if a != b)}"
        if len(got["arrivals"]) == len(want["arrivals"])
        else f"{block}: {len(got['arrivals'])} replies, pinned {len(want['arrivals'])}"
    )
    assert got["words_digest"] == want["words_digest"]
    assert got["end"] == want["end"]


def record() -> None:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    data = {b: {"commit": commit, **S.outcome(b, N_JOBS, SEED)} for b in S.BLOCKS}
    for b, d in data.items():
        if not d["exact"]:
            raise SystemExit(f"{b}: not bit-exact; nothing recorded")
    FIXTURE.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--record"]:
        sys.exit("usage: python -m tests.linalg.test_sim_speed_outcomes --record")
    record()
    print(f"recorded {FIXTURE}")
