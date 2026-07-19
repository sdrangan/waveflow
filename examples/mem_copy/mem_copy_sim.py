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
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemComponent, MemSeg
from waveflow.simulation.simulation import Simulation

from examples.mem_copy.mem_copy import CopyCmd, CopyJob, MemCopy
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle


@dataclass
class MemCopyTB(FreeRunComp):
    """The testbench as a component graph: three participants + the DUT, wired by interfaces.

    This is the same structure ``run_copy`` used to build inline as statements — a driver on
    ``s_cmd``, a sink on ``s_done``, one shared arena behind both ``m_axi`` bundles, and the
    :class:`MemCopy` DUT.  Declaring it as a composite :class:`FreeRunComp` (one with sub-components,
    not a ``run_iter`` body) changes nothing about the simulation; it changes what the structure *is*.
    **A function body is code; a component graph is
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

    #: The scenario: each a :class:`~examples.mem_copy.mem_copy.CopyJob` (word coordinates).  Bare
    #: ``(src, dst, n)`` tuples are accepted too and coerced.
    jobs: tuple = (CopyJob(src_off=16, dst_off=512, n_words=128),)
    mem_dwidth: HwParam[int] = 64
    #: Fixed run bound for the generated XSI main (comfortably past the ~2835 completion; the drain
    #: tail is a testbench constant, not the design's latency -- see the cycles note in the checker).
    n_cycles: int = 3400
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        bpw = w // 8
        # Accept CopyJobs or bare (src, dst, n) tuples; work in CopyJobs from here on.
        self._jobs = [CopyJob.coerce(j) for j in self.jobs]

        # One flat arena covering every source and destination region (byte-addressed, base 0).
        self.arena_words = max(max(job.src_off, job.dst_off) + job.n_words
                               for job in self._jobs) + 16
        self.mem = MemComponent(name=f"{self.name}_mem", sim=self.sim, inline=False, clk=self.clk,
                                word_size=w, addr_size=32, nwords_tot=self.arena_words * 4)
        self.mem.alloc(self.arena_words)             # one segment at word 0 (byte addr 0)
        # XSI: the memory seeds itself from vectors/mem_in in pre_sim and dumps vectors/out in post_sim
        # -- DynParams the generated harness emits, so the source pattern lives once, in Python (see
        # mem_copy.write_mem_copy_xsi_bundles).  Unused by the pysim run below, which seeds mem directly.
        self.mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
        self.mem.dump_segs = [MemSeg(0, int(self.mem.nwords_tot), "vectors/out")]

        # Pre-load each source region with a per-job-distinct, FULL-WIDTH pattern; keep the
        # expectation.  A seeded PRNG (not arange*k) so every one of the w bits is exercised -- a
        # codegen bug that dropped the high word half would slip past a low-magnitude ramp -- while
        # staying reproducible: a failure replays exactly from the seed.
        self.expected: list[np.ndarray] = []
        for j, job in enumerate(self._jobs):
            rng = np.random.default_rng(0xC0FFEE + j)
            known = rng.integers(0, 1 << w, size=job.n_words, dtype=np.uint64)
            self.mem._mem.write(job.src_off * bpw, known)
            self.expected.append(known)

        self.dut = MemCopy(name=f"{self.name}_copier", sim=self.sim, mem_dwidth=w)
        # The testbench owns the schema: it serializes each command into raw stream words.  Those words
        # are the ONE source -- write_scenario materializes them to <root>/vectors/s_cmd, the driver
        # loads that bundle in pre_sim (pysim) exactly as the XSI AxisMaster loads in_bundle, and the
        # XSI vectors are the same bytes.  `self.cmds` is kept for introspection.
        self.cmds = [CopyCmd(src_off=job.src_off, dst_off=job.dst_off, n_words=job.n_words, tx_id=j)
                     for j, job in enumerate(self._jobs)]
        self.cmd_words = [np.asarray(c.serialize(word_bw=w), dtype=np.uint64) for c in self.cmds]
        # in_bundle is the DynParam the XSI harness emits AND the path pysim's driver reads in pre_sim
        # (resolved against the root write_scenario sets).  No temp dir, no eager read.
        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_cmd")
        # The sink dumps its capture (completion words + per-word arrival cycles) so Python checks the
        # output stream AND the completion cycle off-line -- no golden in the generated C++ main.
        self.done_sink = StreamSink(sim=self.sim, bitwidth=w, out_bundle="vectors/s_done")

        # Insertion order is the order the emitter walks; the DUT is found by its `boundary`.
        for c in (self.dut, self.driver, self.done_sink, self.mem):
            self.add_comp(c)

        self._nwords_tot = int(self.mem.nwords_tot)

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


    # -- the scenario as word images -------------------------------------------------------------
    #
    # The source pattern is written ONCE, into the arena above.  These read it back out, so the XSI
    # bundles (`vectors/mem_in` / `vectors/golden`) and the pysim run are the same bytes by
    # construction rather than by two formulas agreeing -- the same reason the command bundle is
    # taken from `self.driver.bursts` rather than re-serialized.

    @property
    def mem_image(self) -> np.ndarray:
        """The seeded arena: what the XSI memory loads in ``pre_sim`` as ``vectors/mem_in``.

        Sized to the memory's full word count, but only ``arena_words`` is allocated (and only the
        source regions within it are non-zero) — so the readable segment is read back and the rest
        left zero, matching the flat arena the XSI ``FlatMemory`` starts from.
        """
        img = np.zeros(self._nwords_tot, dtype=np.uint64)
        img[:self.arena_words] = np.asarray(self.mem._mem.read(0, self.arena_words),
                                            dtype=np.uint64)
        return img

    @property
    def golden_image(self) -> np.ndarray:
        """The expected result as ``vectors/golden``.

        Only the **destination** regions are populated — those are what the checker compares, and
        each holds the very ``expected`` array the pysim golden asserts against.
        """
        g = np.zeros(self._nwords_tot, dtype=np.uint64)
        for job, exp in zip(self._jobs, self.expected):
            g[job.dst_off:job.dst_off + job.n_words] = exp
        return g

    def write_scenario(self, root) -> None:
        """Materialize **the whole scenario** under ``<root>/vectors`` and point the driver at it.

        The single scenario writer for both backends — pysim (``run_copy`` calls it before ``run_sim``)
        and XSI (``write_mem_copy_xsi_bundles`` calls it with the ``xsi/`` dir).  Writes:

        - ``vectors/s_cmd``  — the command stream the driver plays (``cmd_words``);
        - ``vectors/mem_in`` — the source arena the XSI ``FlatMemory`` loads in ``pre_sim``;
        - ``vectors/golden`` — the expected arena after the copy.

        Sets ``driver.root`` so the driver's ``pre_sim`` resolves ``vectors/s_cmd`` against *root* — the
        same on-disk bundle the XSI harness reads.  (pysim still reads the memory from the in-process
        seed; ``mem_in`` is written for XSI and for when the memory load is unified.)
        """
        root = Path(root)
        vdir = root / "vectors"
        write_burst_bundle(self.cmd_words, vdir / "s_cmd")
        write_burst_bundle([self.mem_image], vdir / "mem_in")
        write_burst_bundle([self.golden_image], vdir / "golden")
        self.driver.root = root


def run_copy(jobs=(CopyJob(src_off=16, dst_off=512, n_words=128),),
             mem_dwidth: int = 64) -> "MemCopy":
    """Run the :class:`MemCopyTB` graph and check every copy is bit-exact.

    ``jobs`` are :class:`~examples.mem_copy.mem_copy.CopyJob`\\ s (bare ``(src, dst, n)`` tuples are
    coerced).  Returns the DUT (``s_done`` token count == number of jobs).  The structure lives in
    ``MemCopyTB``; this is the driver: build it, run it, check it."""
    sim = Simulation()
    bpw = mem_dwidth // 8
    tb = MemCopyTB(name="tb", sim=sim, jobs=tuple(jobs), mem_dwidth=mem_dwidth)
    mem, copier, done_sink = tb.mem, tb.dut, tb.done_sink

    # Materialize the command bundle into a temp dir that lives across the run (the driver reads it in
    # pre_sim), then run.  The memory is seeded in-process by the TB, so only the command bundle needs
    # a home on disk.
    with tempfile.TemporaryDirectory() as _root:
        tb.write_scenario(_root)
        sim.run_sim()

    ok = True
    for job, exp in zip(tb._jobs, tb.expected):
        got = mem._mem.read(job.dst_off * bpw, job.n_words).astype(np.uint64)
        job_ok = np.array_equal(got, exp)
        ok = ok and job_ok
        print(f"[copy] src={job.src_off} dst={job.dst_off} n={job.n_words} ok={job_ok}")
    ndone = len(done_sink.words)
    print(f"[copy] jobs={len(tb._jobs)} done_tokens={ndone} all_ok={ok}")
    assert ok, "MemCopy mismatch (dst region != src region)"
    assert ndone == len(tb._jobs), f"expected {len(tb._jobs)} done tokens, got {ndone}"
    return copier


def run_and_check() -> bool:
    run_copy()                                                    # single copy
    run_copy(jobs=(CopyJob(16, 600, 128), CopyJob(200, 900, 64)))  # back-to-back, distinct offsets
    print("mem_copy pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
