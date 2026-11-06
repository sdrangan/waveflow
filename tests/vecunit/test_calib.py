"""The calibration's machinery (Stage 7 of ``plans/vecunit_new.md``): pysim charges a job exactly what
the job model says, the pre-registered resource rows build the configurations they name, the filed
fits are what pysim and the docs read, and a slot's capability mask reaches the generated datapath."""

from __future__ import annotations

import json

import numpy as np
import pytest

from tests.vecunit.test_unit import centre, rtl_config, two_ports
from waveflow.vecunit import calib
from waveflow.vecunit import isa as I
from waveflow.vecunit.config import Cap
from waveflow.vecunit.hls import render_cfg
from waveflow.vecunit.model import exec_items
from waveflow.vecunit.unit import FEATURES, VecTiming, _VecPart, job_features
from waveflow.vecunit.workload import random_jobs


@pytest.mark.parametrize("make", [centre, rtl_config])
def test_the_per_item_charges_are_the_job_model(make):
    """What pysim charges item by item (``run_cycles``, ``srun_cycles``, a control instruction) sums
    to :meth:`VecTiming.predict` of the job's features, for any coefficients."""
    cfg = make()
    rng = np.random.default_rng(3)
    t = VecTiming.from_coefs({f: float(rng.uniform(0.5, 9)) for f in FEATURES})
    for _tx, p, _inputs in random_jobs(cfg, 6, seed=5):
        vl, total = cfg.VLMAX, t.job
        for item in exec_items(p.execute, cfg):
            if item.kind == "passes":
                total += t.run_cycles(item.chains, vl)
            elif item.kind == "scalars":
                total += t.srun_cycles(item.chains)
            else:
                x = item.chains[0][0]
                total += t.instr + t.control
                if int(x.funct) == I.COp.SETVL:
                    vl = int(x.imm1)
        assert total == pytest.approx(t.predict(job_features(p.execute, cfg)))


def test_the_resource_rows_build_what_they_name():
    for spec in calib.RESOURCE_FIT + calib.RESOURCE_HOLDOUT + calib.RESOURCE_HOLDOUT2:
        L, NG, S, caps, _fmts = spec
        cfg = calib.resource_config(spec)
        f = calib.config_features(cfg)
        assert (f["L"], f["NG"], f["S"]) == (L, NG, S)
        assert f["n_mul"] == (S if caps == "all" else 1)


def test_the_filed_timing_is_what_pysim_charges():
    """The filed fit is what pysim loads, and its held-out score is recomputed, not read back: each
    scenario's intervals charged by pysim from the filed coefficients, against the RTL intervals the
    record holds (review item R7 of ``plans/vecunit_future.md``)."""
    from waveflow.linalg.cost import TOOL

    path = calib.model_path(calib.TIMING_MODEL)
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["features"] == list(FEATURES) and rec["tool"] == TOOL
    t = VecTiming.from_coefs(rec["coefs"])
    assert _VecPart.timing == t
    pred, meas = [], []
    for scenario, filed in rec["holdout"].items():
        if scenario in ("all", "targets"):
            continue
        build, kind = scenario.split()
        p = calib.pysim_intervals(calib.build_unit(build), kind, t)
        assert calib.errors(p, filed["rtl"])["mean"] == pytest.approx(filed["mean"]), scenario
        pred += p
        meas += filed["rtl"]
    mean = calib.errors(pred, meas)["mean"]
    assert mean == pytest.approx(rec["holdout"]["all"]["mean"])
    assert mean < calib.TARGET_MEAN  # the score the guide reports


def test_a_slot_without_a_multiplier_is_built_without_one():
    """``MULS`` / ``DOTS``: bit ``s`` set when slot ``s`` has a multiplier (``MUL`` or ``DOT``) / an
    accumulator (``DOT``); the datapath tests them, so a masked slot's hardware is not built."""
    cfg = calib.resource_config((4, 32, 3, "mul1", "cg"))
    assert "MULS = 1, DOTS = 1;" in render_cfg(cfg)[0]
    cfg = two_ports()  # slot 1: MOVE | DOT | LOAD | STORE
    assert cfg.slot_caps[1] & Cap.DOT and not cfg.slot_caps[1] & Cap.MUL
    assert "MULS = 3, DOTS = 3;" in render_cfg(cfg)[0]
    assert "MULS = 7, DOTS = 7;" in render_cfg(rtl_config())[0]


def test_the_filed_resource_model():
    """DSP and BRAM are structural (four DSPs a complex multiplier and five more; the memories as they
    bind), and the held-out score the guide reports is the filed one."""
    rec = json.loads(calib.model_path(calib.RESOURCE_MODEL).read_text(encoding="utf-8"))
    for spec in calib.RESOURCE_FIT + calib.RESOURCE_HOLDOUT + calib.RESOURCE_HOLDOUT2:
        f = calib.config_features(calib.resource_config(spec))
        p = calib.predict_resources(rec["counters"], f)
        assert p["dsp"] == pytest.approx(4 * f["n_mul"] * f["L"] + 5)
        assert p["bram"] == pytest.approx(f["bram18"])
    for counter in ("lut", "ff"):
        assert rec["holdout"][counter]["mean"] < calib.TARGET_MEAN
        assert rec["holdout"][counter]["worst"] < calib.TARGET_WORST


def test_the_filed_timing_is_stage7s_form_scored_on_its_preregistered_set():
    """At 2025.1 (``plans/calib_2025_1.md`` Stage 3) the timing is Stage 7's form -- one set of
    coefficients, fitted on the CG build's ``calib`` run of the RTL catalogue -- not Stage 9d's by-phase
    model, and its held-out score covers exactly the pre-registered :data:`calib.TIMING3_HOLDOUT`.
    (Stage 9d's comparison at 2024.1 is in git history with the record it chose.)"""
    rec = json.loads(calib.model_path(calib.TIMING_MODEL).read_text(encoding="utf-8"))
    assert rec["plan"].startswith("plans/calib_2025_1.md Stage 3")
    assert rec["fit"]["set"] == "cg calib" and "comparison" not in rec
    scored = {tuple(s.split()) for s in rec["holdout"] if s not in ("all", "targets")}
    assert scored == set(calib.TIMING3_HOLDOUT)
