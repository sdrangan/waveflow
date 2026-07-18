"""mem_copy_sim.py — pysim golden harness for the :class:`~examples.mem_copy.mem_copy.MemCopy`
composite (Phase 2, ``plans/mem_stream_impl.md``).

Wires the composite's boundary endpoints to a driver (``s_cmd``), a done sink (``s_done``), and one
**shared flat memory** reached by both sub-component ``m_mem`` masters through a 2-master AXI-MM
crossbar (modelling the two ``m_axi`` bundles gmem0/gmem1 over one buffer).  Runs the SimPy model and
checks the functional golden: each destination region equals a memcpy of its source region.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_component import HwParam
from waveflow.hw.hw_composite import CompositeComp
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemComponent, MemSeg
from waveflow.simulation.simulation import Simulation

from examples.mem_copy.mem_copy import CopyCmd, MemCopy
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle


@dataclass
class MemCopyTB(CompositeComp):
    """The testbench as a component graph: three participants + the DUT, wired by interfaces.

    This is the same structure ``run_copy`` used to build inline as statements — a driver on
    ``s_cmd``, a sink on ``s_done``, one shared arena behind both ``m_axi`` bundles, and the
    :class:`MemCopy` DUT.  Declaring it as a :class:`CompositeComp` changes nothing about the
    simulation; it changes what the structure *is*.  **A function body is code; a component graph is
    data** — and only data can be walked.  ``composite_top_spec`` cannot introspect statements that
    have already executed, so a generator has no way to learn which participants exist or how they
    are wired.  As a graph, the same information generates the XSI testbench
    (:func:`~waveflow.build.composite_gen.tb_top_spec`) as well as running the pysim golden: one
    statement, two backends.

    ``jobs`` is a list of ``(src_off, dst_off, n_words)`` element-coordinate triples.  Multiple jobs
    exercise the free-running ``hls::task`` re-fire, and — because the driver never waits for a
    completion — they overlap, which is the whole point of the design.
    """

    #: A testbench is not a synthesizable kernel — it lowers to the XSI harness (Flow 2's TB target),
    #: not to ``composite_kernel`` (which it would otherwise inherit as a composite ``FreeRunComp``).
    #: This is what makes ``check(MemCopyTB, "sequential_xsi_tb")`` reach gate 4 (tb_top_spec).
    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    jobs: tuple = ((16, 4096 // 8, 128),)
    mem_dwidth: HwParam[int] = 64
    #: Fixed run bound for the generated XSI main (comfortably past the ~2835 completion; the drain
    #: tail is a testbench constant, not the design's latency -- see the cycles note in the checker).
    n_cycles: int = 3400
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        bpw = w // 8
        jobs = list(self.jobs)

        # One flat arena covering every source and destination region (byte-addressed, base 0).
        self.arena_words = max(max(s, d) + n for s, d, n in jobs) + 16
        self.mem = MemComponent(name=f"{self.name}_mem", sim=self.sim, inline=False, clk=self.clk,
                                word_size=w, addr_size=32, nwords_tot=self.arena_words * 4)
        self.mem.alloc(self.arena_words)             # one segment at word 0 (byte addr 0)
        # XSI: the memory seeds itself from vectors/mem_in in pre_sim and dumps vectors/out in post_sim
        # -- DynParams the generated harness emits, so the source pattern lives once, in Python (see
        # mem_copy.write_mem_copy_xsi_bundles).  Unused by the pysim run below, which seeds mem directly.
        self.mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
        self.mem.dump_segs = [MemSeg(0, int(self.mem.nwords_tot), "vectors/out")]

        # Pre-load each source region with a known, per-job-distinct pattern; keep the expectation.
        self.expected: list[np.ndarray] = []
        for j, (src, dst, n) in enumerate(jobs):
            known = (np.arange(n, dtype=np.uint64) * 2654435761 + 12345 + j * 7919) \
                & ((1 << w) - 1)
            self.mem._mem.write(src * bpw, known.astype(np.uint64))
            self.expected.append(known.astype(np.uint64))

        self.dut = MemCopy(name=f"{self.name}_copier", sim=self.sim, mem_dwidth=w)
        # The testbench owns the schema: it serializes each command into raw stream words, writes them
        # as a burst bundle, and points the schema-blind StreamDriver at that bundle -- the one vector
        # form the driver accepts (and, once wired, the same bundle the XSI harness reads).  The
        # bundle is read eagerly, so the temp dir can go away right after construction.  `self.cmds` is
        # kept so the XSI vectors can be re-derived from the very commands the driver sends.
        self.cmds = [CopyCmd(src_off=s, dst_off=d, n_words=n, tx_id=j)
                     for j, (s, d, n) in enumerate(jobs)]
        words = [np.asarray(c.serialize(word_bw=w), dtype=np.uint64) for c in self.cmds]
        with tempfile.TemporaryDirectory() as _vd:
            write_burst_bundle(words, Path(_vd) / "cmd")
            # `bundle` is what the pysim driver plays (a temp dir); `in_bundle` is the DynParam the
            # generated XSI harness emits -- the bundle its AxisMaster loads, rooted at the s_cmd port.
            self.driver = StreamDriver(sim=self.sim, bitwidth=w, bundle=Path(_vd) / "cmd",
                                       in_bundle="vectors/s_cmd")
        # The sink dumps its capture (completion words + per-word arrival cycles) so Python checks the
        # output stream AND the completion cycle off-line -- no golden in the generated C++ main.
        self.done_sink = StreamSink(sim=self.sim, bitwidth=w, out_bundle="vectors/s_done")

        # Insertion order is the order the emitter walks; the DUT is found by its `boundary`.
        for c in (self.dut, self.driver, self.done_sink, self.mem):
            self.add_comp(c)
        self.ordered_subcomps = [self.dut, self.driver, self.done_sink, self.mem]

        cmd_if = StreamIF(name=f"{self.name}_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w)
        cmd_if.bind(ep_name="master", endpoint=self.driver.stream_ep)
        cmd_if.bind(ep_name="slave", endpoint=self.dut.s_cmd)
        self.add_if(cmd_if)

        done_if = StreamIF(name=f"{self.name}_done_if", sim=self.sim, clk=self.clk, bitwidth=w)
        done_if.bind(ep_name="master", endpoint=self.dut.s_done)
        done_if.bind(ep_name="slave", endpoint=self.done_sink.stream_ep)
        self.add_if(done_if)

        # Two m_axi bundles (read gmem0, write gmem1) over the one shared memory: a 2-master
        # crossbar.  NOTE: the crossbar models contention; the XSI slave models do not — see
        # plans/xsi_tb_codegen.md.  The two describe different systems on purpose.
        xbar = AXIMMCrossBarIF(name=f"{self.name}_xbar", sim=self.sim, clk=self.clk,
                               nports_master=2, nports_slave=1, bitwidth=w)
        xbar.bind("master_0", self.dut.m_in)          # MemRStream.m_mem (read)
        xbar.bind("master_1", self.dut.m_out)         # MemWStream.m_mem (write)
        xbar.bind("slave_0", self.mem.s_mm)
        self.add_if(xbar)
        assign_address_ranges([self.mem.s_mm], [(0, self.arena_words * bpw)])


def run_copy(jobs=((16, 4096 // 8, 128),), mem_dwidth: int = 64) -> "MemCopy":
    """Run the :class:`MemCopyTB` graph and check every copy is bit-exact.

    Returns the DUT (``s_done`` token count == number of jobs).  The structure now lives in
    ``MemCopyTB``; this is the driver: build it, run it, check it."""
    sim = Simulation()
    bpw = mem_dwidth // 8
    tb = MemCopyTB(name="tb", sim=sim, jobs=tuple(jobs), mem_dwidth=mem_dwidth)
    mem, copier, done_sink, expected = tb.mem, tb.dut, tb.done_sink, tb.expected

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
