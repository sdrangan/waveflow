"""interleaver_sim.py — pysim golden harness for the canonical
:class:`~examples.interleaver.interleaver.InterleaverCanon` composite.

Wires the composite's boundary to a command driver (``s_cmd``), a done sink (``s_done``), and one shared
flat memory reached by both the read (gmem0) and write (gmem1) masters through a 2-master AXI-MM
crossbar.  Memory layout mirrors the sandbox il_bfm: per job, P then X then Y (``nw`` words each) at
``base = j*3*nw``.  Checks the functional golden ``Y[i] = X[P[i]]`` bit-exact, and exposes the per-job
gather-completion timeline (the SOBIF ping-pong + free-running load overlap makes it pipeline).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simulation import Simulation

from examples.interleaver.interleaver import InterleaverCanon, InterleaverCmd
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle


def _pack(vals: np.ndarray, lw: int) -> np.ndarray:
    """Pack 32-bit *vals* into MEM_DW words: LW elems/word, element i in lane (i % LW)."""
    n = len(vals)
    nw = (n + lw - 1) // lw
    words = np.zeros(nw, dtype=np.uint64)
    for i in range(n):
        words[i // lw] |= (int(vals[i]) & 0xFFFFFFFF) << (32 * (i % lw))
    return words


def run_interleaver(nj: int = 1, n: int = 256, mem_dwidth: int = 64, comp_class=InterleaverCanon,
                    platform_dir: "str | None" = None, compute_calib_dir: "str | None" = None):
    """Run the *comp_class* interleaver composite (the canonical :class:`InterleaverCanon`) over *nj*
    back-to-back jobs (all size *n*) and check Y[j][i]=X[j][P[i]] bit-exact.  Returns the composite
    (gather.job_end_cyc = the completion timeline).

    ``platform_dir`` loads the platform's shipped **bus law** onto the memory, so the m_axi read/write
    transfers are charged their calibrated cost (the two-level infra half — see the calibration guide).
    ``compute_calib_dir`` points the custom gather's loop model at a fitted ``params.json`` (the custom
    half).  Both ``None`` (default) keeps the plain, uncalibrated timing — the fast functional path."""
    sim = Simulation()
    clk = Clock(freq=100e6)
    lw = mem_dwidth // 32
    nw = (n + lw - 1) // lw
    bpw = mem_dwidth // 8

    arena = nj * 3 * nw + 16
    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=mem_dwidth, addr_size=32, nwords_tot=arena * 4)
    mem.alloc(arena)
    # Platform bus model on the memory slave: every m_axi burst the read/write masters issue is charged
    # the calibrated transfer cost, so the sim's timeline reflects the real interconnect. Shared across
    # accelerators — the interleaver reuses the same bus law mem_copy fit.
    if platform_dir is not None:
        from waveflow.calib.bus_model import BusCalib
        mem.s_mm.bus_timing = BusCalib(platform_dir, clk_freq=clk.freq).bus_timing()

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

    il = comp_class(name="il", sim=sim, mem_dwidth=mem_dwidth, n=n,
                    compute_calib_dir=compute_calib_dir)
    # Schema-blind, file-driven driver: serialize each command to words, write a burst bundle, point
    # the driver at it.  The driver loads it in pre_sim, so the temp dir must live across run_sim.
    words = [np.asarray(c.serialize(word_bw=mem_dwidth), dtype=np.uint64) for c in cmds]
    _vd = tempfile.TemporaryDirectory()
    write_burst_bundle(words, Path(_vd.name) / "cmd")
    driver = StreamDriver(sim=sim, bitwidth=mem_dwidth, in_bundle="cmd", root=Path(_vd.name))
    # The done stream is framed (has_tlast) when the composite's writer is an in-band MemWStream, plain
    # otherwise — match the sink to whatever the composite exposes.
    done_sink = StreamSink(sim=sim, bitwidth=mem_dwidth,
                           has_tlast=bool(getattr(il.s_done, "has_tlast", False)))

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
    run_interleaver(nj=1)                     # single job
    run_interleaver(nj=3)                     # back-to-back
    print("interleaver_canon pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
