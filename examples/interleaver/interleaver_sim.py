"""interleaver_sim.py — pysim golden harness for the full :class:`~examples.interleaver.interleaver.
Interleaver` composite (Phase 4, Gate 4).

Wires the composite's boundary to a command driver (``s_cmd``), a done sink (``s_done``), and one shared
flat memory reached by both the read (gmem0) and write (gmem1) masters through a 2-master AXI-MM
crossbar.  Memory layout mirrors the sandbox il_bfm: per job, P then X then Y (``nw`` words each) at
``base = j*3*nw``.  Checks the functional golden ``Y[i] = X[P[i]]`` bit-exact, and exposes the per-job
gather-completion timeline (the SOBIF ping-pong + free-running load overlap makes it pipeline).
"""
from __future__ import annotations

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simulation import Simulation

from examples.interleaver.interleaver import Interleaver, InterleaverCmd, InterleaverSob
from examples.interleaver.mem_stream_sim import CmdDriver, WordSink


def _pack(vals: np.ndarray, lw: int) -> np.ndarray:
    """Pack 32-bit *vals* into MEM_DW words: LW elems/word, element i in lane (i % LW)."""
    n = len(vals)
    nw = (n + lw - 1) // lw
    words = np.zeros(nw, dtype=np.uint64)
    for i in range(n):
        words[i // lw] |= (int(vals[i]) & 0xFFFFFFFF) << (32 * (i % lw))
    return words


def run_interleaver(nj: int = 1, n: int = 256, mem_dwidth: int = 64, comp_class=Interleaver):
    """Run the *comp_class* interleaver composite (the stream/SOB-mix :class:`Interleaver` or the
    P-SOB :class:`InterleaverSob`) over *nj* back-to-back jobs (all size *n*) and check
    Y[j][i]=X[j][P[i]] bit-exact.  Returns the composite (gather.job_end_cyc = the completion
    timeline)."""
    sim = Simulation()
    clk = Clock(freq=100e6)
    lw = mem_dwidth // 32
    nw = (n + lw - 1) // lw
    bpw = mem_dwidth // 8

    arena = nj * 3 * nw + 16
    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=mem_dwidth, addr_size=32, nwords_tot=arena * 4)
    mem.alloc(arena)

    P = ((np.arange(n) * 13 + 5) % n).astype(np.uint32)          # permutation (j-independent)
    cmds, expected = [], []
    for j in range(nj):
        base = j * 3 * nw
        pw, xw, yj = base, base + nw, base + 2 * nw
        Xj = ((np.arange(n, dtype=np.uint64) * 2654435761 + 12345 + j * 7919) & 0xFFFFFFFF)
        mem._mem.write(pw * bpw, _pack(P, lw))                       # byte-addressed backing store
        mem._mem.write(xw * bpw, _pack(Xj.astype(np.uint32), lw))
        cmds.append(InterleaverCmd(p_off=pw, x_off=xw, y_off=yj, n=n))
        expected.append((yj, _pack(Xj[P].astype(np.uint32), lw)))   # golden Y[i]=X[P[i]]

    il = comp_class(name="il", sim=sim, mem_dwidth=mem_dwidth, n=n)
    driver = CmdDriver(sim=sim, bitwidth=mem_dwidth, cmds=cmds)
    done_sink = WordSink(sim=sim, bitwidth=mem_dwidth)

    cmd_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    cmd_if.bind(ep_name="master", endpoint=driver.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=il.s_cmd)

    done_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    done_if.bind(ep_name="master", endpoint=il.s_done)
    done_if.bind(ep_name="slave", endpoint=done_sink.stream_ep)

    xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=2, nports_slave=1, bitwidth=mem_dwidth)
    xbar.bind("master_0", il.m_in)          # MemRStream.m_mem (gmem0 read)
    xbar.bind("master_1", il.m_out)         # MemWStream.m_mem (gmem1 write)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(0, arena * bpw)])

    sim.run_sim()

    ok = True
    for j, (yj, exp_words) in enumerate(expected):
        got = mem._mem.read(yj * bpw, nw).astype(np.uint64)
        job_ok = np.array_equal(got, exp_words)
        ok = ok and job_ok
    ndone = len(done_sink.words)
    per_job = [round(c) for c in il.gather.job_end_cyc]
    print(f"[{comp_class.__name__}] nj={nj} n={n} ok={ok} done={ndone} gather_done_cyc={per_job}")
    assert ok, f"{comp_class.__name__} mismatch (Y != X[P])"
    assert ndone == nj, f"expected {nj} done tokens, got {ndone}"
    return il


def run_and_check() -> bool:
    for cc in (Interleaver, InterleaverSob):
        run_interleaver(nj=1, comp_class=cc)                     # single job
        run_interleaver(nj=3, comp_class=cc)                     # back-to-back
    print("interleaver pysim golden (both variants): PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
