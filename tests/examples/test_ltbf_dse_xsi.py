"""examples/ltbf_dse at RTL (plans/ltbf_dse/ltbf_dse.md Phase 5), ``-m xsi``.

* AC8 (step 5.1): each device's gate system (``hw/devsys.py``) through the framework's system flow --
  the systolic wrapper on a job of each kind at AC9's shapes (``T``, ``U``, ``W``, ``Z``, ``K``), the
  vector unit's wrapper on a CG step and an equalizer job, the Ingest DMA on two symbols -- is RTL =
  pysim on every endpoint, and its top's csynth estimate is within the 4 ns target.

The flows run in the example's root, as ``python -m examples.ltbf_dse.hw.build`` does, so a top's
csynth is reused while its sources are unchanged (the vector unit's takes hours, plan §15).
Vitis HLS / Vivado 2024.1.
"""

from __future__ import annotations

import pytest

from waveflow.toolchain import toolchain

PERIOD_NS = 4.0


def _toolchain() -> None:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis HLS not found; the XSI gate needs it")
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["sys", "vec", "ingest"])
def test_device_rtl_equals_pysim(name):
    from examples.ltbf_dse.hw import build as b
    from waveflow.utils.csynthparse import synth_target
    from waveflow.vecunit.build import build_config

    _toolchain()
    cfg = build_config(b.ROOT)
    cfg.params["synth"] = "build"
    res = b.device_dag((name,)).run(cfg, through=f"{name}_compare")
    bad = {k: (r.message or "")[-2000:] for k, r in res.items() if not r.success}
    assert not bad, bad
    est = synth_target(b.ROOT / f"ltbf_{name}_proj" / "solution1" / "syn" / "report")
    assert est is not None and est["estimated_period_ns"] <= PERIOD_NS, est
