"""AC11 of ``plans/cpu_model.md`` is measured by ``python -m waveflow.cpu.bench`` and recorded in the
plan's progress log, not gated here: speed depends on the machine's load.  This test only checks the
bench runs and finishes every task, for both arrival patterns.
"""

from __future__ import annotations

import pytest

from waveflow.cpu.bench import main, run_bench


@pytest.mark.parametrize("pattern", ["stream", "burst"])
def test_the_bench_finishes_every_task(pattern):
    r = run_bench(pattern, 500, n_cores=4)
    assert r["tasks"] == 500
    assert r["sim_s"] > 0 and r["tasks_per_s"] > 0


def test_the_cli_runs(capsys):
    assert main(["--pattern", "burst", "--tasks", "50"]) == 0
    assert "tasks/s" in capsys.readouterr().out
