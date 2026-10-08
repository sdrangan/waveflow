"""AC5 of ``plans/cpu_model.md`` (host part): every Python twin equals its C kernel, bit for bit.

Each kernel is compiled with the host ``gcc`` (no m5 markers, so the same source runs natively) and
run at its smoke points; the JSON it prints must equal the twin's dict, outputs and counters alike.
Step 11 extends this to every pre-registered point, and to the gem5-run binary's own output.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from waveflow.cpu.calib.kernels import KERNEL_DIR, KERNELS

POINTS = [(name, p) for name, k in KERNELS.items() for p in k.smoke]


@pytest.fixture(scope="module")
def host_bins(tmp_path_factory):
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.fail("the twin test needs a host gcc; none on PATH")
    out = tmp_path_factory.mktemp("kernels")
    bins = {}
    for name, k in KERNELS.items():
        exe = out / name
        subprocess.run(
            [
                gcc,
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-I{KERNEL_DIR}",
                str(k.source),
                "-o",
                str(exe),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        bins[name] = exe
    return bins


@pytest.mark.parametrize("name, point", POINTS, ids=[f"{n}-{p}" for n, p in POINTS])
def test_the_twin_equals_the_c_program(host_bins, name, point):
    k = KERNELS[name]
    run = subprocess.run(
        [str(host_bins[name]), *k.argv(point)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(run.stdout) == k.run_twin(point)


def test_every_kernel_has_a_source_and_smoke_points():
    for k in KERNELS.values():
        assert k.source.is_file(), k.source
        assert k.smoke, k.name
        assert set(k.counters) <= set(k.run_twin(k.smoke[-1])), k.name


def test_a_twin_runs_as_a_sw_function(make_run):
    k = KERNELS["sched_ops"]
    func = k.sw_function(cycles=100)
    run = make_run()

    def proc():
        out = yield from run.cpu.execute(func, op="sort", n=10, seed=3)
        assert out["kernel"] == "sched_ops"

    run.sim.env.process(proc())
    run.run()
    rec = run.cpu.records[0]
    assert rec.feats["n_scanned"] == 26 and rec.feats["n_moved"] == 18
    assert rec.feats["ws"] == 8.0 * 11


# ---------------------------------------------------------------------------
# AC5 on every registered point (step 11), against the host build.
# ---------------------------------------------------------------------------

from waveflow.cpu.calib.sweep import sweep_rows

REGISTERED = [(k, p) for k, p, _ in sweep_rows()]


def test_the_twin_equals_the_c_program_at_every_registered_point(host_bins):
    bad = []
    for name, point in REGISTERED:
        k = KERNELS[name]
        run = subprocess.run(
            [str(host_bins[name]), *k.argv(point)],
            check=True,
            capture_output=True,
            text=True,
        )
        if json.loads(run.stdout) != k.run_twin(point):
            bad.append((name, point))
    assert len(REGISTERED) == 305
    assert not bad, bad
