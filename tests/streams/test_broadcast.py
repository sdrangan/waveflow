"""StreamBroadcast (``waveflow/streams/broadcast.py``): one framed stream in, ``n_out`` copies out.

pysim in the block's harness -- every output's bursts equal to the input's, at several widths and output
counts -- and the RTL gate (``-m xsi``): the block in its own harness through the system flow, every
output's recording at RTL equal to pysim's.  The body is variadic in the output count and templated on
every port's word type; the RTL gate is what shows Vitis accepts both.

Builds into a short directory under the repo (csynth fails silently past the Windows path limit) and
re-csynths only when the RTL is stale.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.hw.harness import harness
from waveflow.simulation.simulation import Simulation
from waveflow.streams import StreamBroadcast, StreamBroadcastStep
from waveflow.streams import rtl
from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path

ROOT = Path(__file__).resolve().parents[2] / "work" / "streams"


def _bcast(n_out: int, bitwidth: int) -> StreamBroadcast:
    return StreamBroadcast(name="bcast", sim=Simulation(), n_out=n_out, bitwidth=bitwidth)


@pytest.mark.parametrize("n_out,bitwidth", [(1, 32), (2, 32), (3, 64), (4, 16)])
def test_every_output_carries_the_input_bursts(n_out, bitwidth):
    sysm = harness(_bcast(n_out, bitwidth), workload="test")
    sysm.run()
    want = [np.asarray(b.words, dtype=np.uint64) for b in sysm.dut().workload("test")["s_in"]]
    for k in range(n_out):
        got = [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[f"m_out_{k}"].words]
        assert len(got) == len(want)
        for g, w in zip(got, want):
            np.testing.assert_array_equal(g, w)


def test_ports_and_body():
    b = _bcast(3, 64)
    assert b.ports == ("s_in", "m_out_0", "m_out_1", "m_out_2")
    kt = b.kernel_task()
    assert kt.signature == b.ports and kt.framed == b.ports and kt.template_args == (64,)


def test_n_out_must_be_positive():
    with pytest.raises(ValueError):
        _bcast(0, 32)


def test_the_step_copies_the_body(tmp_path):
    step = StreamBroadcastStep(output_dir="include")
    text = step.generate("stream_broadcast_task", BuildConfig(root_dir=tmp_path, params={}))
    assert "template <int W, typename TI, typename... TO>" in text


@pytest.mark.xsi
@pytest.mark.parametrize("n_out,bitwidth", [(2, 32), (3, 64)], ids=["2x32", "3x64"])
def test_rtl_outputs_match_pysim(n_out, bitwidth):
    """THE gate: the block in its own harness at RTL -- every output's recording equal to pysim's."""
    if not (find_vivado_path() and find_vitis_path()):
        pytest.skip("XSI gate prerequisite missing: Vitis HLS + Vivado")
    root = ROOT / f"b{n_out}w{bitwidth}"
    root.mkdir(parents=True, exist_ok=True)
    res = rtl.build_dag(root / "xw", n_out, bitwidth).run(
        BuildConfig(root_dir=root, params={"synth": "build"}), through="compare")
    bad = {n: r.message for n, r in res.items() if not r.success}
    assert not bad, bad
    work = root / "xw" / rtl.top_name(n_out, bitwidth)
    assert sorted(p.name for p in (work / "traces").iterdir()) == [f"m_out_{k}" for k in range(n_out)]
    assert json.loads((work / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []
