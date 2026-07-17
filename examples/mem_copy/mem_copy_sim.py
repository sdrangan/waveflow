"""mem_copy_sim.py — pysim golden harness for the :class:`~examples.mem_copy.mem_copy.MemCopy`
composite (Phase 2, ``plans/mem_stream_impl.md``).

Wires the composite's boundary endpoints to a driver (``s_cmd``), a done sink (``s_done``), and one
**shared flat memory** reached by both sub-component ``m_mem`` masters through a 2-master AXI-MM
crossbar (modelling the two ``m_axi`` bundles gmem0/gmem1 over one buffer).  Runs the SimPy model and
checks the functional golden: each destination region equals a memcpy of its source region.
"""
from __future__ import annotations

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simulation import Simulation

from examples.mem_copy.mem_copy import CopyCmd, MemCopy
from waveflow.simulation.stream_tb import CmdDriver, WordSink


def run_copy(jobs=((16, 4096 // 8, 128),), mem_dwidth: int = 64) -> "MemCopy":
    """Run the ``MemCopy`` composite over *jobs* and check every copy is bit-exact.

    *jobs* is a list of ``(src_off, dst_off, n_words)`` element-coordinate triples; multiple jobs
    exercise the free-running ``hls::task`` re-fire across commands (back-to-back copies).  Returns
    the composite (``s_done`` token count == number of jobs)."""
    sim = Simulation()
    clk = Clock(freq=100e6)
    bpw = mem_dwidth // 8

    # One flat arena covering every source and destination region (byte-addressed, base 0).
    arena_words = max(max(s, d) + n for s, d, n in jobs) + 16
    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=mem_dwidth, addr_size=32, nwords_tot=arena_words * 4)
    mem.alloc(arena_words)                       # one segment at word 0 (byte addr 0)

    # Pre-load each source region with a known, per-job-distinct pattern.
    expected: list[np.ndarray] = []
    for j, (src, dst, n) in enumerate(jobs):
        known = (np.arange(n, dtype=np.uint64) * 2654435761 + 12345 + j * 7919) \
            & ((1 << mem_dwidth) - 1)
        mem._mem.write(src * bpw, known.astype(np.uint64))
        expected.append(known.astype(np.uint64))

    copier = MemCopy(name="copier", sim=sim, mem_dwidth=mem_dwidth)
    driver = CmdDriver(sim=sim, bitwidth=mem_dwidth,
                       cmds=[CopyCmd(src_off=s, dst_off=d, n_words=n) for s, d, n in jobs])
    done_sink = WordSink(sim=sim, bitwidth=mem_dwidth)

    cmd_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    cmd_if.bind(ep_name="master", endpoint=driver.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=copier.s_cmd)

    done_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    done_if.bind(ep_name="master", endpoint=copier.s_done)
    done_if.bind(ep_name="slave", endpoint=done_sink.stream_ep)

    # Two m_axi bundles (read gmem0, write gmem1) over the one shared memory: a 2-master crossbar.
    xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=2, nports_slave=1, bitwidth=mem_dwidth)
    xbar.bind("master_0", copier.m_in)           # MemRStream.m_mem (read)
    xbar.bind("master_1", copier.m_out)          # MemWStream.m_mem (write)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(0, arena_words * bpw)])

    sim.run_sim()

    ok = True
    for (src, dst, n), exp in zip(jobs, expected):
        got = mem._mem.read(dst * bpw, n).astype(np.uint64)
        job_ok = np.array_equal(got, exp)
        ok = ok and job_ok
        print(f"[copy] src={src} dst={dst} n={n} ok={job_ok}")
    ndone = len(done_sink.words)
    print(f"[copy] jobs={len(jobs)} done_tokens={ndone} all_ok={ok}")
    assert ok, "MemCopy mismatch (dst region != src region)"
    assert ndone == len(jobs), f"expected {len(jobs)} done tokens, got {ndone}"
    return copier


def run_and_check() -> bool:
    run_copy()                                             # single copy
    run_copy(jobs=((16, 600, 128), (200, 900, 64)))        # back-to-back, distinct offsets
    print("mem_copy pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
