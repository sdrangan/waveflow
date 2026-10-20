"""The systolic engine and its memory wrapper, without a harness: the generated top declares the
banked blocks and the port widths, and refused commands and jobs are answered.  The harness runs --
pysim against the model, and the RTL gates -- are the example's (``tests/examples/test_systolic.py``).
"""

from __future__ import annotations

from tests.linalg._hls import reg
from waveflow.build.composite_gen import composite_top_spec, render_top
from waveflow.linalg.message import Status
from waveflow.linalg.systolic import MatmulOp
from waveflow.linalg.systolic_engine import SystolicEngine, command, command_status
from waveflow.linalg.systolic_engine_mem import EngineJobDesc, job_status
from waveflow.simulation.simulation import Simulation

CENTRE = dict(Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8, L=4, form=4,
              a=reg(12, 3), b=reg(12, 4), c=reg(12, 5))


def engine(word_bits: int = 64) -> SystolicEngine:
    return SystolicEngine(name="systolic_engine", sim=Simulation(), word_bits=word_bits, **CENTRE)


def test_engine_top_declares_banked_blocks():
    top = render_top(composite_top_spec(engine(), width=64))
    assert "hls::stream_of_blocks<ap_uint<24>[8][8], 2> a;" in top
    assert "#pragma HLS ARRAY_PARTITION variable=a dim=1 complete" in top
    assert "hls::stream_of_blocks<ap_uint<24>[8][4][4][2], 2> b;" in top
    assert "#pragma HLS ARRAY_PARTITION variable=b dim=3 complete" in top
    assert "#pragma HLS ARRAY_PARTITION variable=b dim=4 complete" in top
    assert "#pragma HLS STREAM variable=paya depth=32" in top   # a whole A: the receiver moves on
    assert "hls::stream<ap_uint<96> > cg;" in top                # C in drain order, to the collector


def test_engine_top_keeps_the_status_port_at_64_bits():
    top = render_top(composite_top_spec(engine(word_bits=128), width=128))
    assert "hls::stream<streamutils::axi4s_word<128> >& s_in," in top
    assert "hls::stream<streamutils::axi4s_word<64> >& m_status" in top


def test_wrapper_top_reads_through_burst_maxi():
    """The feeder requests a tile's panels ahead of their data: its read port is an
    hls::burst_maxi, not a pointer (and the top includes its header); the writer's stays a pointer."""
    from waveflow.linalg.systolic_engine_mem import SystolicEngineMem
    mem = SystolicEngineMem(name="systolic_engine_mem", sim=Simulation(), word_bits=128, **CENTRE)
    top = render_top(composite_top_spec(mem, width=128))
    assert '#include "hls_burst_maxi.h"' in top
    assert "    hls::burst_maxi<ap_uint<128> > m_in," in top
    assert "    ap_uint<128>* m_out," in top


def test_command_rules():
    lim = dict(Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8)
    assert command_status(command(1, MatmulOp.MUL, 8, 5, 16, npanels=3), **lim) == Status.OK
    assert command_status(command(1, 9, 8, 8, 32), **lim) == Status.BAD_OP
    assert command_status(command(1, MatmulOp.MUL, 6, 8, 32), **lim) == Status.BAD_DIMS
    assert command_status(command(1, MatmulOp.MUL, 8, 8, 32, npanels=5), **lim) == Status.BAD_DIMS
    assert command_status(command(1, MatmulOp.MUL, 8, 8, 32, npanels=2, keep_a=True),
                          **lim) == Status.BAD_DIMS
    d = EngineJobDesc()
    d.op, d.M, d.K, d.N = MatmulOp.MUL, 16, 40, 32
    assert job_status(d, Mmax=8, Kmax=8, Nmax=32, Pmax=4) == Status.BAD_DIMS     # 5 panels
    d.K = 32
    assert job_status(d, Mmax=8, Kmax=8, Nmax=32, Pmax=4) == Status.OK
