"""StreamFramer (``waveflow/streams/framer.py``): a stream framed by count into a framed one, TLAST every
``frame_words`` words -- so a StreamDemux can sit after a unit that frames by count (SsrFft).

pysim against the golden at three frame lengths, with bursts that do not line up with the frames; the
model's frame state across bursts; the body's signature and step; the generated top; and, at RTL
(``-m xsi``), its harness's recording equal to pysim's.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from tests.streams.test_arbiter_demux import ROOT
from waveflow.build.build import BuildConfig
from waveflow.hw.harness import harness
from waveflow.simulation.simulation import Simulation
from waveflow.streams import StreamFramer, StreamFramerStep, rtl
from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path


def _golden_matches(sysm):
    for port, want in sysm.golden_outputs().items():
        got = [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[port].words]
        assert len(got) == len(want), port
        for g, w in zip(got, want):
            np.testing.assert_array_equal(g, np.asarray(w, dtype=np.uint64))


@pytest.mark.parametrize("frame_words", [1, 4, 16])
@pytest.mark.parametrize("kind", ["test", "long"])
def test_the_framer_matches_the_golden(frame_words, kind):
    sysm = harness(StreamFramer(name="stream_framer", sim=Simulation(), frame_words=frame_words), workload=kind)
    sysm.run()
    _golden_matches(sysm)


def test_a_frame_spans_bursts_and_a_burst_holds_frames():
    """The test scenario's bursts (1, 3, 8, 3, 5, ... words at frame_words 4) do not line up with the
    frames, and every output burst is still exactly one frame."""
    mod = StreamFramer(name="stream_framer", sim=Simulation(), frame_words=4)
    lengths = [len(b.words) for b in mod.workload("test")["s_in"]]
    assert any(n < 4 for n in lengths) and any(n > 4 for n in lengths)
    sysm = harness(mod, workload="test")
    sysm.run()
    assert {len(b) for b in sysm.stand_ins["m_out"].words} == {4}


def test_it_refuses_an_empty_frame():
    with pytest.raises(ValueError, match="frame_words"):
        StreamFramer(name="f", sim=Simulation(), frame_words=0)


def test_its_body_and_step():
    mod = StreamFramer(name="f", sim=Simulation(), frame_words=16, bitwidth=64)
    kt = mod.kernel_task()
    assert kt.signature == ("s_in", "m_out") and kt.template_args == (64, 16)
    assert StreamFramer.drains_inputs
    s = StreamFramerStep(output_dir="include")
    assert "stream_framer_task" in s.generate(s.KEY, BuildConfig(root_dir=".", params={}))


def test_the_generated_top(tmp_path):
    mod, _top = rtl.make("framer", 2, 32, 16)
    rtl.generate_for(tmp_path, mod)
    (top,) = (tmp_path / "gen").glob("*.cpp")
    assert "stream_framer_task<32, 16," in top.read_text()


@pytest.mark.xsi
def test_rtl_output_matches_pysim():
    """THE gate: the framed output's recording at RTL equal to pysim's, TLAST for TLAST, not empty."""
    if not (find_vivado_path() and find_vitis_path()):
        pytest.skip("XSI gate prerequisite missing: Vitis HLS + Vivado")
    root = ROOT / "frm16"
    root.mkdir(parents=True, exist_ok=True)
    dag, top = rtl.build_block_dag(root / "xw", "framer", 2, 32, 16)
    res = dag.run(BuildConfig(root_dir=root, params={"synth": "build"}), through="compare")
    bad = {k: r.message for k, r in res.items() if not r.success}
    assert not bad, bad
    work = root / "xw" / top
    assert (work / "traces" / "m_out" / "words.bin").stat().st_size > 0
    assert json.loads((work / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []
