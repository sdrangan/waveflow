"""Every generated adaptor is byte-identical at ``RD_OUTST = 1`` (``plans/bram_fullrate_port.md``, the
first gate).

The front's ``RD_OUTST`` (outstanding reads) and the decoder's order FIFO arrive for the full-rate BRAM
leaf, ``mm_bram_port_pipe.v``.  Every adaptor that holds no such leaf must render exactly as before:
the same text, so the same RTL, the same cycle counts at every XSI gate, and the same instance
parameters in the inventory.  The digests below were taken on ``origin/main`` (``d5500d76``) before
the change, from the generator's two entry points over a matrix of views, from the tops the XSI gates
render, and from the system tops of the examples that render without tools.

A digest that moves is a change to generated RTL.  If that change is deliberate, re-record with
``python -m tests.build.test_adaptor_rtl_unchanged --write`` and say why in the commit; never to make
this test pass.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

DIGESTS = Path(__file__).resolve().parent / "golden" / "adaptor_rtl_rd_outst1.json"


def _views():
    from waveflow.build.mm_adaptor_gen import (
        BramView,
        BramViewP1,
        CreditInView,
        QueueView,
        RegBankView,
    )

    return {
        "qin16": QueueView("qi", "in", axis="k_in", depth=16),
        "qin512": QueueView("qi", "in", axis="k_in", depth=512),
        "qout16": QueueView("qo", "out", axis="k_out", depth=16),
        "rb": RegBankView("rb", ncfg=5, nstat=3, cfg_axis="k_cfg", status_axis="k_st"),
        "crd": CreditInView("cr", axis="k_crd"),
        "bram6": BramView("bw", kport="kb", baw=6),
        "bram9": BramView("bw", kport="kb", baw=9),
        "bramp1": BramViewP1("bp", kport="kp", baw=9),
    }


def _renames(v, name: str):
    from dataclasses import replace

    return replace(v, name=name)


def _slot_cases() -> dict[str, str]:
    """``render_view_slot`` and ``render_adaptor_slot`` over a matrix of views and widths."""
    from waveflow.build.mm_adaptor_gen import render_adaptor_slot, render_view_slot

    views = _views()
    out: dict[str, str] = {}
    for dw in (32, 64, 128, 512):
        for key, v in views.items():
            out[f"view/{key}/dw{dw}"] = render_view_slot(v, "mi0_axi", dw, 32, 1)
    # a single view on a wider window
    from dataclasses import replace

    out["view/qin16_law13/dw64"] = render_view_slot(replace(views["qin16"], law=13), "mi0_axi", 64,
                                                    32, 2)
    out["view/bram9_law13/dw64"] = render_view_slot(replace(views["bram9"], law=13), "mi0_axi", 64,
                                                    32, 2)
    sets = {
        "one": ["bram9"],
        "two": ["qin16", "bram9"],
        "three": ["qin16", "qout16", "bram9"],     # a hole: span 4
        "four": ["rb", "qin512", "qout16", "crd"],
        "five": ["qin16", "qout16", "rb", "crd", "bramp1"],   # holes 5..7
    }
    for sname, keys in sets.items():
        vs = [_renames(views[k], f"v{i}") for i, k in enumerate(keys)]
        for dw in (64, 128):
            for idw in (1, 2):
                out[f"adaptor/{sname}/dw{dw}/idw{idw}"] = render_adaptor_slot("ad", vs, "mi0_axi", dw,
                                                                              32, idw)
    return out


def _gate_tops() -> dict[str, str]:
    """The tops the ``-m xsi`` gates of ``tests/build`` render (pure Python: no tool runs here)."""
    from tests.build import (
        test_mm_bram_order_xsi,
        test_mm_queue_memw_xsi,
        test_mm_queue_xsi,
        test_mm_regbank_xsi,
    )

    return {
        "gate/bram_order_one_front": test_mm_bram_order_xsi.render_top("bo_top", "one_front"),
        "gate/bram_order_per_view": test_mm_bram_order_xsi.render_top("bo_top", "per_view"),
        "gate/queue": test_mm_queue_xsi.render_top("q_top"),
        "gate/regbank": test_mm_regbank_xsi.render_top("rb_top"),
        "gate/memw": test_mm_queue_memw_xsi.render_top("memw_q_top"),
    }


def _system_tops() -> dict[str, str]:
    """System tops walked from examples that need no csynth to render, and their inventories'
    instance parameters (the identity keys move with any source edit, so they are not compared)."""
    from examples.markov.markov_build import system as markov_system
    from examples.mm_fir.mm_fir import mm_fir_system
    from examples.mm_fir.mm_fir_build import PLAN
    from waveflow.build.system_top import SystemTopSpec, system_top_spec

    out: dict[str, str] = {}

    def add(name: str, spec) -> None:
        out[f"system/{name}"] = spec.render_verilog()
        out[f"inventory/{name}"] = json.dumps(
            [(i.path, i.module, i.params) for i in spec.instances()], sort_keys=True, default=str)

    for topo in ("one_front", "per_view"):
        s = mm_fir_system(x=[0], plan=PLAN, one_front=topo == "one_front")
        add(f"mm_fir_{topo}", system_top_spec(s.xbar, [s.fir], top="mm_fir_top"))
    m = markov_system()
    add("markov", system_top_spec(m.xbar, [m.gen, m.chain, m.mem], top="markov_top"))
    # An on-chip memory alone in its harness: the memory slot, with each BRAM leaf a memory names.
    from examples.ltbf_cg_mem_dse.hw.system import mem_script, shared_mem
    from waveflow.hw.clock import Clock
    from waveflow.hw.harness import memory_harness

    for rate in (1, 2):
        h = memory_harness(lambda sim, r=rate: shared_mem(sim, 1 << 12, Clock(freq=250e6),
                                                          calibrated=False, rate=r),
                           mem_script("test", 1 << 12), name=f"umem_r{rate}")
        add(f"memory_rate{rate}", SystemTopSpec.from_system(h))
    return out


def rendered() -> dict[str, str]:
    return {**_slot_cases(), **_gate_tops(), **_system_tops()}


def digests() -> dict[str, str]:
    return {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in sorted(rendered().items())}


def test_generated_adaptors_are_byte_identical_at_one_outstanding_read():
    want = json.loads(DIGESTS.read_text(encoding="utf-8"))
    got = digests()
    assert sorted(got) == sorted(want), "the case list changed: re-record deliberately (see the docstring)"
    moved = [k for k in sorted(want) if got[k] != want[k]]
    assert not moved, f"generated adaptor RTL moved at RD_OUTST = 1: {moved}"


def test_the_cases_cover_every_view_type_and_a_hole():
    r = rendered()
    assert len(r) >= 60
    assert any("_hole" in v and "_hole_rsp" in v for k, v in r.items() if k.startswith("adaptor/three"))
    for mod in ("mm_queue_in", "mm_queue_out", "mm_regbank", "mm_credit_in", "mm_bram_port ",
                "mm_bram_port_p1", "axi_slave_front"):
        assert any(mod in v for v in r.values()), mod


if __name__ == "__main__":
    if "--write" in sys.argv:
        DIGESTS.parent.mkdir(parents=True, exist_ok=True)
        DIGESTS.write_text(json.dumps(digests(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {DIGESTS}")
    else:
        pytest.main([__file__, "-q"])
