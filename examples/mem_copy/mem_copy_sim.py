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
from types import SimpleNamespace
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_module import HwParam, kernel_name
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemoryMod, MemSeg
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.simulation.simulation import Simulation

from examples.mem_copy.mem_copy import CopyCmd, CopyJob, MemCopy
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle, write_run_params


@dataclass
class MemCopyTB(FreeRunMod):
    """The testbench as a component **graph** — PURE structure: three participants + the DUT, wired by
    interfaces (a driver on ``s_cmd``, a sink on ``s_done``, one shared arena behind both ``m_axi``
    bundles, and the :class:`MemCopy` DUT).

    Declaring it as a composite :class:`FreeRunMod` (sub-components, not a ``run_iter`` body) is what
    makes it *walkable*: **a function body is code; a component graph is data** — only data can be
    introspected.  ``composite_top_spec`` / ``tb_top_spec`` cannot read statements that have already
    executed, so a generator learns the participants and their wiring from this graph.  The same graph
    generates the XSI testbench and runs the pysim golden — one structure, two backends.

    What is deliberately **not** here: the scenario (source patterns, expectation) and the run/check
    procedure.  Those are *code*, not structure, and live in :class:`MemCopySim`, which owns a
    ``MemCopyTB`` and drives it.  ``__post_init__`` builds only the graph.

    ``jobs`` is a list of ``(src_off, dst_off, n_words)`` element-coordinate triples.  Multiple jobs
    exercise the free-running ``hls::task`` re-fire, and — because the driver never waits for a
    completion — they overlap, which is the whole point of the design.
    """

    #: A testbench is not a synthesizable kernel — it lowers to the XSI harness (Flow 2's TB target),
    #: not to ``composite_kernel`` (which it would otherwise inherit as a composite ``FreeRunMod``).
    #: This is what makes ``check(MemCopyTB, "sequential_xsi_tb")`` reach gate 4 (tb_top_spec).
    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    #: The scenario: each a :class:`~examples.mem_copy.mem_copy.CopyJob` (word coordinates).  Bare
    #: ``(src, dst, n)`` tuples are accepted too and coerced.
    jobs: tuple = (CopyJob(src_off=16, dst_off=512, n_words=128),)
    mem_dwidth: HwParam[int] = 64
    #: Fixed run bound for the generated XSI main (comfortably past the ~2908 completion; the drain
    #: tail is a testbench constant, not the design's latency -- see the cycles note in the checker).
    n_cycles: int = 3400
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Forwarded to the DUT's writer: when set, the pysim run records per-firing timing (and applies
    #: any fitted delay).  ``None`` (default) is the plain, uncalibrated run.
    calib_dir: "str | None" = None
    #: The PLATFORM's bus-transfer calibration directory.  When set, the memory's slave loads a
    #: :class:`~waveflow.hw.memif.BusTiming` from it, so pysim charges the real m_axi burst cost —
    #: the platform half of the two-level split, shared across accelerators.  ``None`` = the plain
    #: word_bw fallback.
    platform_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        parts = wire_mem_copy(self.sim, name=self.name, jobs=self.jobs,
                              mem_dwidth=int(self.mem_dwidth), clk=self.clk,
                              calib_dir=self.calib_dir, platform_dir=self.platform_dir)
        for k in ("_jobs", "arena_words", "mem", "dut", "cmds", "cmd_words", "driver", "done_sink",
                  "_nwords_tot"):
            setattr(self, k, getattr(parts, k))
        # Insertion order is the order the emitter walks; the DUT is found by its `boundary`.
        for c in parts.comps:
            self.add_comp(c)
        for i in parts.ifs:
            self.add_if(i)


def wire_mem_copy(sim, *, name: str = "tb", jobs=(CopyJob(src_off=16, dst_off=512, n_words=128),),
                  mem_dwidth: int = 64, clk: "Clock | None" = None, calib_dir: "str | None" = None,
                  platform_dir: "str | None" = None) -> SimpleNamespace:
    """The :class:`MemCopy` kernel and its testbench participants, built on *sim* and wired: a
    driver on ``s_cmd``, a sink on ``s_done``, one shared arena behind both ``m_axi`` bundles (a
    2-master crossbar models their contention in pysim).  The one wiring both forms use --
    :func:`mem_copy_system` (``HwSystem(sim, dut)``, the system flow) and :class:`MemCopyTB` (the
    composite graph the calibration tools and the kernel-flow testbench walk).

    Returns the parts: ``dut`` (the kernel), ``driver``, ``done_sink``, ``mem``, ``xbar``, ``comps``
    and ``ifs`` (in the order a composite registers them), the scenario's ``_jobs`` (as
    :class:`CopyJob`), ``cmds`` / ``cmd_words`` (the command bursts the driver plays),
    ``arena_words`` / ``_nwords_tot`` (the arena the DUT addresses, and the memory's size), and
    ``mem_dwidth``.  Names are ``<name>_mem``, ``<name>_copier``, ``<name>_cmd_if``, ..."""
    w = int(mem_dwidth)
    bpw = w // 8
    clk = clk if clk is not None else Clock(freq=100e6)
    # Accept CopyJobs or bare (src, dst, n) tuples; work in CopyJobs from here on.
    jobs = [CopyJob.coerce(j) for j in jobs]

    # One flat arena covering every source and destination region (byte-addressed, base 0).
    arena_words = max(max(job.src_off, job.dst_off) + job.n_words for job in jobs) + 16
    mem = MemoryMod(name=f"{name}_mem", sim=sim, inline=False, clk=clk,
                    word_size=w, addr_size=32, nwords_tot=arena_words * 4)
    # Platform bus model: the memory's slave charges the calibrated m_axi transfer cost, so the
    # component's residual is just its own control cost.  Shared across accelerators (fit once).
    if platform_dir is not None:
        from waveflow.calib.bus_model import BusCalib
        mem.s_mm.bus_timing = BusCalib(platform_dir, clk_freq=clk.freq).bus_timing()
    # Allocate the full capacity so the memory is the same size as the RTL FlatMemory and the whole
    # vectors/mem_in image loads directly in pre_sim (no clip).  The DUT still addresses only
    # [0, arena_words) -- the extra is headroom for the image.
    mem.alloc(int(mem.nwords_tot))
    # Both backends seed the memory from vectors/mem_in in pre_sim (load_segs) and the RTL memory
    # dumps vectors/out in post_sim (dump_segs).  These are DynParams the harness emits; pysim's
    # MemoryMod.pre_sim loads the same bundle (root set by the scenario writer).
    mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
    mem.dump_segs = [MemSeg(0, 0, "vectors/out")]       # 0 = the whole arena

    dut = MemCopy(name=f"{name}_copier", sim=sim, mem_dwidth=w, calib_dir=calib_dir,
                  platform_dir=platform_dir)
    # The testbench owns the schema: it serializes each command into raw stream words.  Those words
    # are the ONE source -- the scenario writer materializes them to <root>/vectors/s_cmd, the driver
    # loads that bundle in pre_sim (pysim) exactly as the XSI AxisMaster loads in_bundle, and the
    # XSI vectors are the same bytes.  `cmds` is kept for introspection.
    cmds = [CopyCmd(src_off=job.src_off, dst_off=job.dst_off, n_words=job.n_words, tx_id=j)
            for j, job in enumerate(jobs)]
    cmd_words = [np.asarray(c.serialize(word_bw=w), dtype=np.uint64) for c in cmds]
    # in_bundle is the DynParam the XSI harness emits AND the path pysim's driver reads in pre_sim
    # (resolved against the root the scenario writer sets).  No temp dir, no eager read.
    driver = StreamDriver(sim=sim, bitwidth=w, in_bundle="vectors/s_cmd")
    # The sink dumps its capture (completion words + per-word arrival cycles) so Python checks the
    # output stream AND the completion cycle off-line -- no golden in the generated C++ main.
    done_sink = StreamSink(sim=sim, bitwidth=w, out_bundle="vectors/s_done", has_tlast=True)

    cmd_if = StreamIF(name=f"{name}_cmd_if", sim=sim, clk=clk, bitwidth=w)
    cmd_if.bind(ep_name="master", endpoint=driver.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=dut.s_cmd)

    done_if = StreamIF(name=f"{name}_done_if", sim=sim, clk=clk, bitwidth=w)
    done_if.bind(ep_name="master", endpoint=dut.s_done)
    done_if.bind(ep_name="slave", endpoint=done_sink.stream_ep)

    # Two m_axi bundles (read gmem0, write gmem1) over the one shared memory: a 2-master
    # crossbar.  NOTE: the crossbar models contention; the XSI slave models do not — see
    # the xsi_tb_codegen plan (commit 3052952).  The two describe different systems on purpose.
    xbar = AXIMMCrossBarIF(name=f"{name}_xbar", sim=sim, clk=clk,
                           nports_master=2, nports_slave=1, bitwidth=w)
    xbar.bind("master_0", dut.m_in)          # MemRStream.m_mem (read)
    xbar.bind("master_1", dut.m_out)         # MemWStream.m_mem (write)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(0, arena_words * bpw)])
    return SimpleNamespace(dut=dut, driver=driver, done_sink=done_sink, mem=mem, xbar=xbar,
                           comps=(dut, driver, done_sink, mem), ifs=(cmd_if, done_if, xbar),
                           _jobs=jobs, cmds=cmds, cmd_words=cmd_words, arena_words=arena_words,
                           _nwords_tot=int(mem.nwords_tot), mem_dwidth=w)


class MemCopySim:
    """The **procedure** around a :class:`MemCopyTB` graph — the code half of the testbench.

    A graph is data (walkable → the XSI harness); this is the code that *drives* it: materialize a
    scenario onto disk, run the pysim golden, and check the result.  Splitting it out is the point:
    ``MemCopyTB.__post_init__`` builds only structure, so nothing a generator walks is entangled with
    file I/O or the golden.  :meth:`write_scenario` is still the **single** scenario writer both
    backends share — pysim (:meth:`run`) and XSI (``write_mem_copy_xsi_bundles``) — so the two can
    never start from different bytes.
    """

    def __init__(self, jobs=(CopyJob(src_off=16, dst_off=512, n_words=128),),
                 mem_dwidth: int = 64, name: str = "tb", calib_dir: "str | None" = None,
                 platform_dir: "str | None" = None, n_cycles: "int | None" = None) -> None:
        kw = {} if n_cycles is None else {"n_cycles": int(n_cycles)}
        self.tb = MemCopyTB(name=name, sim=Simulation(), jobs=tuple(jobs), mem_dwidth=mem_dwidth,
                            calib_dir=calib_dir, platform_dir=platform_dir, **kw)
        #: The per-job source patterns, filled by :meth:`write_scenario` and read back by :meth:`check`.
        self.expected: list[np.ndarray] = []

    @classmethod
    def on(cls, parts) -> "MemCopySim":
        """The procedure around parts already built -- :func:`wire_mem_copy`'s, with ``n_cycles``
        set -- rather than a :class:`MemCopyTB` of its own: what :func:`mem_copy_system` drives."""
        self = cls.__new__(cls)
        self.tb = parts
        self.expected = []
        return self

    def write_scenario(self, root) -> None:
        """Materialize **the whole scenario** under ``<root>/vectors`` and point the graph's
        participants at it.

        The single scenario writer for both backends.  Computes the source patterns once (a seeded PRNG
        per job, full-width so every one of the ``w`` bits is exercised and a dropped high half would
        show; reproducible from the seed), stores :attr:`expected` for the check, and writes:

        - ``vectors/s_cmd``  — the command stream the driver plays (the TB's ``cmd_words``);
        - ``vectors/mem_in`` — the source arena **both** memories load in ``pre_sim``;
        - ``vectors/golden`` — the expected arena after the copy.

        Then points the driver and memory at *root* so their ``pre_sim`` resolves the relative bundle
        paths against it — the same on-disk bundles the XSI harness reads.
        """
        tb = self.tb
        root = Path(root)
        vdir = root / "vectors"
        w = int(tb.mem_dwidth)
        mem_in = np.zeros(tb._nwords_tot, dtype=np.uint64)
        golden = np.zeros(tb._nwords_tot, dtype=np.uint64)
        self.expected = []
        for j, job in enumerate(tb._jobs):
            rng = np.random.default_rng(0xC0FFEE + j)
            known = rng.integers(0, 1 << w, size=job.n_words, dtype=np.uint64)
            mem_in[job.src_off:job.src_off + job.n_words] = known
            golden[job.dst_off:job.dst_off + job.n_words] = known
            self.expected.append(known)
        write_burst_bundle(tb.cmd_words, vdir / "s_cmd")
        write_burst_bundle([mem_in], vdir / "mem_in")
        write_burst_bundle([golden], vdir / "golden")
        # The scenario's sizes, read by the XSI testbench at run time (wfbfm::run_param): so a
        # testbench generated for one scenario runs any other -- a workload sweep compiles it once.
        # Written at every scenario, the default too: a run reads whatever run.json its vectors/
        # holds, and a previous point's must not outlive it.
        write_run_params(vdir, n_cycles=int(tb.n_cycles), mem_words=int(tb._nwords_tot))
        tb.driver.root = root
        tb.mem.root = root

    def run(self) -> "MemCopy":
        """Materialize the scenario into a temp dir (the driver reads it in ``pre_sim``), run the SimPy
        model, and check every copy is bit-exact.  Returns the DUT."""
        with tempfile.TemporaryDirectory() as _root:
            self.write_scenario(_root)
            self.tb.sim.run_sim()
        return self.check()

    def check(self) -> "MemCopy":
        """Assert every destination region equals its source pattern, and one ``CopyResp`` landed per
        job.  Returns the DUT."""
        tb = self.tb
        bpw = int(tb.mem_dwidth) // 8
        ok = True
        for job, exp in zip(tb._jobs, self.expected):
            got = tb.mem._mem.read(job.dst_off * bpw, job.n_words).astype(np.uint64)
            job_ok = np.array_equal(got, exp)
            ok = ok and job_ok
            print(f"[copy] src={job.src_off} dst={job.dst_off} n={job.n_words} ok={job_ok}")
        ndone = len(tb.done_sink.words)
        print(f"[copy] jobs={len(tb._jobs)} done_bursts={ndone} all_ok={ok}")
        assert ok, "MemCopy mismatch (dst region != src region)"
        # Each job emits ONE framed s_done burst -- the echoed CopyResp (tx_id).
        assert ndone == len(tb._jobs), f"expected {len(tb._jobs)} done bursts, got {ndone}"
        return tb.dut


def run_copy(jobs=(CopyJob(src_off=16, dst_off=512, n_words=128),),
             mem_dwidth: int = 64) -> "MemCopy":
    """Build a :class:`MemCopySim`, run it, and check every copy is bit-exact — a thin convenience
    over ``MemCopySim(jobs, mem_dwidth).run()``.  Returns the DUT."""
    return MemCopySim(jobs=tuple(jobs), mem_dwidth=mem_dwidth).run()


def run_and_check() -> bool:
    run_copy()                                                    # single copy
    run_copy(jobs=(CopyJob(16, 600, 128), CopyJob(200, 900, 64)))  # back-to-back, distinct offsets
    print("mem_copy pysim golden: PASSED")
    return True



def mem_copy_system(jobs=(CopyJob(src_off=16, dst_off=512, n_words=128),), mem_dwidth: int = 64,
                    n_cycles: int = 3400, calib_dir: "str | None" = None,
                    platform_dir: "str | None" = None) -> HwSystem:
    """mem_copy as a **system**, ``HwSystem(sim, dut)``: the kernel and its testbench built on one
    simulation (:func:`wire_mem_copy`), then the DUT named -- the :class:`MemCopy` kernel alone, no
    bus inside it.  Everything else on the simulation is the testbench: the command driver, the
    completion sink, the memory, and the crossbar that models the two ``m_axi`` bundles' contention in
    pysim.  That crossbar is *outside* the cut, so the kernel's two ``m_axi`` bundles are top ports
    facing the memory model, and its two streams are top ports facing the driver and the sink.

    The scenario is :class:`MemCopySim`'s -- the command bursts, the source arena, the golden, and its
    ``run.json`` (the arena size beside the bound *n_cycles*); ``run()`` returns its golden check (the
    kernel).  The parts are attributes of the system (``sysm.kernel``, ``sysm.driver``,
    ``sysm.done_sink``, ``sysm.mem``, ``sysm.xbar``)."""
    sim = Simulation()
    parts = wire_mem_copy(sim, jobs=jobs, mem_dwidth=mem_dwidth, calib_dir=calib_dir,
                          platform_dir=platform_dir)
    parts.n_cycles = int(n_cycles)
    runner = MemCopySim.on(parts)
    dut = DutTop.of(f"{kernel_name(parts.dut)}_dut", sim, comps=[parts.dut])
    sysm = HwSystem(sim, dut, n_cycles=int(n_cycles), inputs=runner.write_scenario,
                    check=lambda s: runner.check(),
                    fresh=lambda: mem_copy_system(jobs, mem_dwidth, n_cycles, calib_dir,
                                                  platform_dir))
    sysm.clk, sysm.data_width = parts.dut.clk, int(mem_dwidth)
    sysm.jobs, sysm.runner = tuple(jobs), runner
    sysm.kernel, sysm.driver, sysm.done_sink = parts.dut, parts.driver, parts.done_sink
    sysm.mem, sysm.xbar = parts.mem, parts.xbar
    return sysm


if __name__ == "__main__":
    run_and_check()
