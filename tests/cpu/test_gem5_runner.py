"""Step 9 of ``plans/cpu_model.md``: the gem5 runner and the pre-registration guard.

The unmarked tests need no gem5: the stats parser on a committed fixture, the core configuration,
the guard on a throwaway git repository, and clone-aware symbol sizing on a host build.  The
``gem5``-marked tests run every kernel end to end; under ``-m gem5`` none of them may skip
(``tests/conftest.py``).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from waveflow.cpu.calib.gem5 import (
    Gem5Config,
    Gem5Runner,
    parse_stats_blocks,
    pick_stats,
    roi_stats,
    symbol_sizes,
)
from waveflow.cpu.calib.kernels import KERNEL_DIR, KERNELS
from waveflow.cpu.calib.prereg import PreregistrationError, SweepPlan

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/cpu/stats_sched_ops_add_n10.txt"
)


# ---------------------------------------------------------------------------
# No gem5 needed
# ---------------------------------------------------------------------------


def test_the_measured_region_is_the_first_block():
    text = FIXTURE.read_text()
    assert len(parse_stats_blocks(text)) == 2
    roi = pick_stats(roi_stats(text))
    assert (roi["cycles_raw"], roi["insts"], roi["ops"]) == (304, 178, 192)
    assert roi["l1d_accesses"] == 41
    assert (
        roi["l1d_misses"] == 0
    )  # gem5 omits a counter that never incremented: absent is zero


def test_a_file_without_the_exit_block_is_rejected():
    one = FIXTURE.read_text().split("---------- End Simulation Statistics")[0]
    with pytest.raises(RuntimeError, match="found 1"):
        roi_stats(one)


def test_the_core_configuration_reaches_starter_se():
    args = Gem5Config().starter_args()
    assert args == [
        "--cpu",
        "hpi",
        "--cpu-freq",
        "1.2GHz",
        "--num-cores",
        "1",
        "--mem-type",
        "DDR4_2400_8x8",
        "--mem-channels",
        "1",
    ]
    with pytest.raises(NotImplementedError):
        Gem5Config(l2_bytes=512 * 1024)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


ROWS = [
    ("sched_ops", {"op": "add", "n": 10, "seed": 3}, "fit"),
    ("sched_ops", {"op": "add", "n": 20, "seed": 3}, "validation"),
    ("cdot_q15", {"n": 100, "seed": 5}, "test"),
]


def test_an_uncommitted_plan_is_refused(repo):
    path = SweepPlan.write(repo / "sweep_plan.csv", ROWS)
    with pytest.raises(PreregistrationError, match="not tracked"):
        SweepPlan.load(path)
    _git(repo, "add", "sweep_plan.csv")
    _git(repo, "commit", "-qm", "register")
    path.write_text(path.read_text() + 'gather_hist,"{}",fit\n')
    with pytest.raises(PreregistrationError, match="uncommitted"):
        SweepPlan.load(path)


def test_a_committed_plan_gives_roles_and_its_commit(repo):
    path = SweepPlan.write(repo / "sweep_plan.csv", ROWS)
    _git(repo, "add", "sweep_plan.csv")
    _git(repo, "commit", "-qm", "register")
    added = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    plan = SweepPlan.load(path)
    assert plan.commit == added
    assert plan.role("sched_ops", {"seed": 3, "n": 20, "op": "add"}) == "validation"
    assert plan.role("cdot_q15", {"n": 100, "seed": 5}) == "test"
    with pytest.raises(PreregistrationError, match="not in"):
        plan.role("cdot_q15", {"n": 101, "seed": 5})


def test_a_duplicate_registration_is_refused(repo):
    path = SweepPlan.write(repo / "p.csv", ROWS + [ROWS[0]])
    _git(repo, "add", "p.csv")
    _git(repo, "commit", "-qm", "dup")
    with pytest.raises(PreregistrationError, match="twice"):
        SweepPlan.load(path)


def test_the_runner_refuses_before_it_runs_anything(tmp_path):
    runner = Gem5Runner(gem5_root=tmp_path / "no-gem5", workdir=tmp_path)
    k = KERNELS["cdot_q15"]
    with pytest.raises(PreregistrationError, match="not a smoke point"):
        runner.measure(k, {"n": 12345, "seed": 1}, empty_cycles=0, smoke=True)
    with pytest.raises(PreregistrationError, match="committed SweepPlan"):
        runner.measure(k, {"n": 12345, "seed": 1}, empty_cycles=0)
    assert not (tmp_path / "runs").exists()


def test_symbol_sizes_count_gcc_clones(tmp_path):
    gcc, nm = shutil.which("gcc"), shutil.which("nm")
    if gcc is None or nm is None:
        pytest.fail("needs host gcc and nm")
    k = KERNELS["sched_ops"]
    exe = tmp_path / "sched_ops"
    subprocess.run(
        [gcc, "-O2", f"-I{KERNEL_DIR}", str(k.source), "-o", str(exe)], check=True
    )
    sizes = symbol_sizes(nm, exe, k)
    assert {s.split(".")[0] for s in sizes} == set(k.symbols)
    assert all(v > 0 for v in sizes.values())


# ---------------------------------------------------------------------------
# gem5 gates
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    r = Gem5Runner(workdir=tmp_path_factory.mktemp("gem5"))
    why = r.unavailable()
    if why:
        pytest.skip(why)
    return r


@pytest.fixture(scope="module")
def empty_cycles(runner):
    return runner.empty_region_cycles()


@pytest.mark.gem5
def test_the_empty_region_costs_94_cycles(empty_cycles):
    # gem5 v25.1.0.1, HPI at 1.2 GHz, DDR4_2400_8x8 x1 (plans/cpu_model.md step 2).  gem5 is
    # deterministic, so this is exact for these tools; a change means the tools changed.
    assert empty_cycles == 94


SMOKE = [
    ("sched_ops", {"op": "reprio", "n": 57, "seed": 11}),
    ("cdot_q15", {"n": 100, "seed": 5}),
    ("gather_hist", {"n": 5000, "m": 4096, "seed": 2}),
    ("dispatch", {"n": 1000, "seed": 7}),
    ("ctx_switch", {"k": 100}),
    ("swapcontext", {"k": 5}),
]


@pytest.mark.gem5
@pytest.mark.parametrize("name, point", SMOKE, ids=[s[0] for s in SMOKE])
def test_a_kernel_runs_end_to_end(runner, empty_cycles, name, point):
    k = KERNELS[name]
    row = runner.measure(k, point, empty_cycles=empty_cycles, smoke=True)
    assert row["output_matches_twin"] is True
    assert row["cycles"] > 0 and math.isclose(row["cycles"], row["cycles_raw"] - 94)
    assert row["role"] == "smoke" and row["prereg_commit"] == ""
    for col in ("gem5_commit", "compiler_version", "cflags", "dram", "kernel_sig"):
        assert row[col], col
    assert row["code_bytes"] > 0
    assert json.loads(row["point"]) == point


@pytest.mark.gem5
def test_an_unregistered_point_is_refused_with_a_plan(runner, empty_cycles, repo):
    path = SweepPlan.write(repo / "sweep_plan.csv", ROWS)
    _git(repo, "add", "sweep_plan.csv")
    _git(repo, "commit", "-qm", "register")
    plan = SweepPlan.load(path)
    k = KERNELS["cdot_q15"]
    with pytest.raises(PreregistrationError):
        runner.measure(k, {"n": 7, "seed": 5}, empty_cycles=empty_cycles, plan=plan)
    row = runner.measure(k, {"n": 100, "seed": 5}, empty_cycles=empty_cycles, plan=plan)
    assert (row["role"], row["prereg_commit"]) == ("test", plan.commit)
