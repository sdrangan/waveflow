"""The systolic engine's resource model on the platform (``plans/calib_2025_1.md`` Stage 4): DSP and
block RAM counted exactly on every calibration point, the filed fits are what the record holds, the
held-out score is recomputed and meets the targets, and ``estimate_system``'s kernel price is the
composed estimate's total.

Reads ``examples/systolic/engine_resources.json`` (written by ``examples/systolic/engine_resources.py``)
as data; each point's kernel is elaborated from the record's own description of it, so nothing here
imports the example.  Needs no toolchain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.linalg import cost
from waveflow.linalg.systolic_engine import SystolicEngine
from waveflow.linalg.systolic_engine_mem import SystolicEngineMem
from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode

RECORD = Path(__file__).resolve().parents[2] / "examples" / "systolic" / "engine_resources.json"
REC = json.loads(RECORD.read_text(encoding="utf-8"))
COUNTERS = ("lut", "ff", "dsp", "bram")


def _kernel(name: str):
    spec = REC["points"][name]["kernel"]
    fmts = {f: Format(W, I, s, QMode(q), OMode(o)) for f, (W, I, s, q, o) in spec["formats"].items()}
    cls = {"SystolicEngine": SystolicEngine, "SystolicEngineMem": SystolicEngineMem}[spec["cls"]]
    return cls(name=spec["name"], sim=Simulation(), **spec["params"], **fmts)


def _kernel_price(name: str) -> dict:
    k = _kernel(name)
    model = type(k).get_rm(cost.platform())
    assert isinstance(model, cost.EngineKernelModel)
    return dict(model.predict(k))


def test_the_record_is_the_platforms():
    assert REC["tool"] == cost.TOOL and REC["part"] == cost.PART
    held = {n for n, p in REC["points"].items() if p["role"] == "holdout"}
    assert held == set(REC["holdout"]["set"]) and len(held) == 2
    assert len(REC["points"]) == 23


@pytest.mark.parametrize("name", sorted(REC["points"]))
def test_dsp_and_bram_are_counted_exactly(name):
    """Every point, the held-out ones too: the multiplies and the 2025.1 binding of the buffers and
    FIFOs (:func:`cost.engine_bram18`) give csynth's DSP and BRAM18 with no fitted parameter."""
    pred, meas = _kernel_price(name), REC["points"][name]["top"]
    assert (pred["dsp"], pred["bram"]) == (meas["dsp"], meas["bram"])


def test_the_filed_models_price_what_the_record_holds():
    """The record's predictions are the filed models' (recomputed, not read back)."""
    for name, p in REC["points"].items():
        assert _kernel_price(name) == p["predicted"], name


def test_the_held_out_score_is_recomputed_and_meets_the_targets():
    held, t = REC["holdout"], REC["targets"]
    for name in held["set"]:
        pred, meas = _kernel_price(name), REC["points"][name]["top"]
        for c in ("lut", "ff"):
            assert (pred[c] - meas[c]) / meas[c] == pytest.approx(held["errors"][name][c])
    s = held["summary"]
    assert min(s["dsp"]["exact_share"], s["bram"]["exact_share"]) >= t["dsp_bram_exact_share"]
    assert max(s["lut"]["mean"], s["ff"]["mean"]) <= t["lut_ff_mean"]


@pytest.mark.parametrize("name", ["centre", "ltbf_r4c8_p22"])
def test_a_composed_estimate_is_the_kernel_price(name):
    """``add_rm`` then ``compose``: each task its model, each composite its own share (and the
    memory writer priced by its wrapper) -- the same total as the kernel's class model, nothing
    counted twice."""
    from waveflow.calib.resource_model import compose
    from waveflow.hw.hw_module import HwModule

    k = _kernel(name)
    HwModule._RM_CACHE.clear()
    k.add_rm(cost.platform())
    est = compose(k)
    assert {c: est.total[c] for c in COUNTERS} == _kernel_price(name)


def test_the_binding_rule():
    """From the reports: a buffer of 384 or 768 bits in flip-flops, 1,024 bits in two 18-bit
    columns; a FIFO of 1,024 bits in shift registers, 1,040 in four."""
    assert cost.engine_bram18("mem", 16, 24) == 0
    assert cost.engine_bram18("mem", 32, 24) == 0
    assert cost.engine_bram18("mem", 32, 32) == 2
    assert cost.engine_bram18("mem", 128, 96) == 6
    assert cost.engine_bram18("mem", 32, 256) == 15
    assert cost.engine_bram18("fifo", 8, 128) == 0
    assert cost.engine_bram18("fifo", 16, 65) == 4
    assert cost.engine_bram18("fifo", 16, 96) == 6


def test_the_core_index_products():
    assert [cost.engine_index_dsps(R, C) for R, C in ((2, 4), (4, 4), (4, 8), (8, 8), (8, 16))] == [
        5, 5, 4, 3, 3]
