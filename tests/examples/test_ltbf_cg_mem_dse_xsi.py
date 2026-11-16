"""examples/ltbf_cg_mem_dse at RTL (plans/ltbf_dse/ltbf_dse.md Phase 5), ``-m xsi``.

* AC8 (step 5.1): each device's gate system (``hw/devsys.py``) through the framework's system flow --
  the systolic wrapper on a job of each kind at AC9's shapes (``T``, ``U``, ``W``, ``Z``, ``K``), the
  vector unit's wrapper on a CG step and an equalizer job, the Ingest DMA on two symbols -- is RTL =
  pysim on every endpoint, and its top's csynth estimate is within the 4 ns target;
* AC9 (step 5.2): the system -- the systolic wrapper, the vector unit's, the Ingest DMA, the Resp hub
  and Shared Mem on one crossbar -- at its AC9 configuration, its host the C++ twin
  (``hw/ltbf_host.h``) running the static policy over one long-term update and one slot.  The Resp
  FIFO takes records in completion order, which is timing's, so the gate is the example's (plan §9 e):
  each engine's Job FIFO writes, the Ingest DMA's Ctrl writes and the O-RU's symbols word for word;
  the Resp records per source in order; every burst of data the host read or wrote (its Python
  stages' and A53 jobs' operands and results, the ``Z`` the DMRS estimate reads, every ``x̂`` the
  measurement reads) as a multiset; and pysim's end within 5% of RTL's.

The flows run in the example's root, as ``python -m examples.ltbf_cg_mem_dse.hw.build`` does, so a top's
csynth is reused while its sources are unchanged (the vector unit's takes hours, plan §15).
Vitis HLS / Vivado 2025.1 (first passed at 2024.1; plans/calib_2025_1.md Stage 1).
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
@pytest.mark.parametrize(
    "name", ["sys", "vec", "ingest"], ids=["systolic", "vec", "ingest"]
)
def test_device_rtl_equals_pysim(name):
    from examples.ltbf_cg_mem_dse.hw import build as b
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


def _bursts(d) -> list[tuple]:
    from waveflow.utils.burst_io import read_burst_bundle

    return [tuple(int(w) for w in b) for b in read_burst_bundle(d)]


@pytest.mark.xsi
def test_system_at_rtl():
    import json
    import re

    from examples.ltbf_cg_mem_dse.hw import build as b
    from examples.ltbf_cg_mem_dse.hw.records import Resp
    from waveflow.vecunit.build import build_config

    _toolchain()
    cfg = build_config(b.ROOT)
    cfg.params["synth"] = "build"
    dag = b.system_dag()
    for through in (
        "ltbf_pysim",
        "ltbf_system_xsi",
    ):  # not ltbf_compare: endpoint by endpoint
        res = dag.run(cfg, through=through)
        bad = {k: (r.message or "")[-2000:] for k, r in res.items() if not r.success}
        assert not bad, bad
    work = b.ROOT / b.WORK_DIR / "ltbf"
    py, rtl = work / "pysim_traces", work / "traces"
    for ep in ("qjob_sys", "qjob_vec", "ing_ctrl", "qsym"):
        assert _bursts(py / ep) == _bursts(rtl / ep), ep

    def by_source(d) -> dict:
        out: dict = {}
        for burst in _bursts(d):
            r = Resp().deserialize(list(burst), word_bw=64)
            out.setdefault(int(r.src), []).append(
                (int(r.status), int(r.tx_id), int(r.seq))
            )
        return out

    assert by_source(py / "qresp") == by_source(rtl / "qresp")
    # The host's data; its one-word credit writes batch as timing has it.
    assert sorted(x for x in _bursts(py / "bus") if len(x) > 1) == sorted(
        x for x in _bursts(rtl / "bus") if len(x) > 1
    )
    t_py = json.loads((work / "pysim.json").read_text())["cycles"]
    out = json.loads((work / "report.json").read_text())["output"]
    t_rtl = int(re.search(r"DONE done=1 cycles=(\d+)", out).group(1))
    assert abs(t_py / t_rtl - 1) <= 0.05, (t_py, t_rtl)


#: plans/dse_cg.md steps 1a and 1d: the system with the options the DSE uses -- the per-design formats,
#: and Shared Mem modelled (simulated, never synthesized) with ``mem_banks`` 1 or 2 and ``mem_rate`` 2
#: (the BRAM leaf as built) or 1 (``PipelinedMemoryMod``) -- at AC9's design: (banks, rate, slot).  The
#: ``lt_`` gates run what the DSE times, the long-term update alone (``slot=False``, the C++ host's
#: ``n_rt == 0`` path); ``e2e_b1r2`` is step 1d's end-to-end run at RTL, once, at the default point.
MODELLED = {"lt_b1r2": (1, 2, False), "lt_b2r2": (2, 2, False), "lt_b1r1": (1, 1, False),
            "lt_b2r1": (2, 1, False), "e2e_b1r2": (1, 2, True)}  # fmt: skip


def _bursts_or_none(d) -> list[tuple]:
    """A trace endpoint's bursts, or none when the run never wrote it (no slot: no symbols)."""
    return _bursts(d) if (d / "words.bin").exists() else []


@pytest.mark.xsi
@pytest.mark.parametrize("tag", list(MODELLED))
def test_modelled_memory_at_rtl(tag):
    """The gate of ``test_system_at_rtl``, in the DSE's configuration: every Job FIFO, Ctrl and symbol
    write word for word, the Resp records per source in order, every data burst as a multiset, and
    pysim's end within 5% of RTL's.  Built in a root of its own per bank count (``hw/build/dse_gate_b<n>``,
    local; the example's root keeps AC9's sources), sharing one csynth between its runs."""
    import json
    import re
    import shutil
    from dataclasses import replace

    from examples.ltbf_cg_mem_dse.hw import build as b
    from examples.ltbf_cg_mem_dse.hw import system as sy
    from examples.ltbf_cg_mem_dse.hw.records import Resp
    from waveflow.vecunit.build import build_config

    _toolchain()
    banks, rate, slot = MODELLED[tag]
    p = replace(sy.AC9, formats="design", mem_banks=banks, mem_rate=rate, slot=slot)
    root = b.ROOT / "hw" / "build" / f"dse_gate_b{banks}"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(b.ROOT / "src", root / "src", dirs_exist_ok=True)
    cfg = build_config(root)
    cfg.params["synth"] = "build"
    dag = b.variant_dag({tag: p})
    for through in (f"{tag}_pysim", f"{tag}_system_xsi"):
        res = dag.run(cfg, through=through)
        bad = {k: (r.message or "")[-2000:] for k, r in res.items() if not r.success}
        assert not bad, bad
    work = root / b.WORK_DIR / tag
    py, rtl = work / "pysim_traces", work / "traces"
    for ep in ("qjob_sys", "qjob_vec", "ing_ctrl", "qsym"):
        assert _bursts_or_none(py / ep) == _bursts_or_none(rtl / ep), ep
    assert _bursts(py / "qjob_sys") and _bursts(py / "qjob_vec")
    assert bool(_bursts_or_none(py / "qsym")) == slot

    def by_source(d) -> dict:
        out: dict = {}
        for burst in _bursts(d):
            r = Resp().deserialize(list(burst), word_bw=64)
            out.setdefault(int(r.src), []).append((int(r.status), int(r.tx_id), int(r.seq)))
        return out

    assert by_source(py / "qresp") == by_source(rtl / "qresp")
    assert sorted(x for x in _bursts(py / "bus") if len(x) > 1) == sorted(
        x for x in _bursts(rtl / "bus") if len(x) > 1
    )
    t_py = json.loads((work / "pysim.json").read_text())["cycles"]
    out = json.loads((work / "report.json").read_text())["output"]
    t_rtl = int(re.search(r"DONE done=1 cycles=(\d+)", out).group(1))
    print(f"{tag}: pysim {t_py} RTL {t_rtl} cycles ({t_py / t_rtl - 1:+.2%})")
    assert abs(t_py / t_rtl - 1) <= 0.05, (t_py, t_rtl)
