"""interleaver_inband_sim.py — pysim golden harness + XSI testbench GRAPH for :class:`InterleaverInband`.

Mirrors ``mem_copy_sim.py``: a testbench **composite graph** (:class:`InterleaverInbandTB`) that the XSI
generator walks (:func:`~waveflow.build.composite_gen.tb_top_spec`), plus a **procedure**
(:class:`InterleaverInbandSim`) that materializes the scenario onto disk and checks the ``Y[i]=X[P[i]]``
golden.  One statement, two backends: pysim builds the graph and runs it; the XSI generator builds the
same graph and emits the BFM harness from it, so they cannot describe different tests.

Layout, per job: ``P`` then ``X`` then ``Y`` (``nw`` words each) in one flat arena; the DUT reads ``P``
(``p_off``) and ``X`` (``x_off``) and writes ``Y`` (``y_off``).  Jobs may be different sizes (variable
length — the in-band descriptor carries ``n``).
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_module import HwParam
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemModel, MemSeg
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

from examples.interleaver.interleaver import InterleaverCmd
from examples.interleaver.interleaver_inband import InterleaverInband


def _pack(vals: np.ndarray, lw: int) -> np.ndarray:
    """Pack 32-bit *vals* into MEM_DW words: LW elems/word, element i in lane (i % LW)."""
    n = len(vals)
    nw = (n + lw - 1) // lw
    words = np.zeros(nw, dtype=np.uint64)
    for i in range(n):
        words[i // lw] |= (int(vals[i]) & 0xFFFFFFFF) << (32 * (i % lw))
    return words


@dataclass
class InterleaverInbandTB(FreeRunMod):
    """The testbench as a component **graph** — a driver on ``s_cmd``, a sink on ``s_done``, one shared
    arena behind both ``m_axi`` bundles, and the :class:`InterleaverInband` DUT — wired by interfaces.
    A composite ``FreeRunMod`` so the graph is *walkable*: the XSI harness and the pysim golden are one
    structure, two backends.  The scenario + check live in :class:`InterleaverInbandSim`."""

    #: A testbench lowers to the XSI harness (Flow 2's TB target), not to a synthesizable kernel.
    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    #: Per-job element counts (all equal = fixed size; mixed = variable length).
    sizes: tuple = (256,)
    mem_dwidth: HwParam[int] = 64
    #: Fixed run bound for the generated XSI main — comfortably past the last completion.
    n_cycles: int = 4000
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    compute_calib_dir: "str | None" = None
    platform_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        lw = w // 32
        self._sizes = [int(n) for n in self.sizes]
        n_max = max(self._sizes)

        # Lay out each job's P/X/Y regions (nw words each) in one flat arena.
        cur, self._layout = 0, []
        for n in self._sizes:
            nw = (n + lw - 1) // lw
            self._layout.append((n, nw, cur, cur + nw, cur + 2 * nw))   # (n, nw, p_off, x_off, y_off)
            cur += 3 * nw
        self.arena_words = cur + 16

        self.mem = MemModel(name=f"{self.name}_mem", sim=self.sim, inline=False, clk=self.clk,
                                word_size=w, addr_size=32, nwords_tot=self.arena_words * 4)
        if self.platform_dir is not None:
            from waveflow.calib.bus_model import BusCalib
            self.mem.s_mm.bus_timing = BusCalib(self.platform_dir, clk_freq=self.clk.freq).bus_timing()
        self.mem.alloc(int(self.mem.nwords_tot))
        self.mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
        self.mem.dump_segs = [MemSeg(0, int(self.mem.nwords_tot), "vectors/out")]
        self._nwords_tot = int(self.mem.nwords_tot)

        self.dut = InterleaverInband(name=f"{self.name}_il", sim=self.sim, mem_dwidth=w, n=n_max,
                                     compute_calib_dir=self.compute_calib_dir)
        self.cmds = [InterleaverCmd(p_off=p, x_off=x, y_off=y, n=n)
                     for (n, nw, p, x, y) in self._layout]
        self.cmd_words = [np.asarray(c.serialize(word_bw=w), dtype=np.uint64) for c in self.cmds]
        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_cmd")
        # s_done is framed (the in-band MemWStream echoes the IlDesc) — has_tlast.
        self.done_sink = StreamSink(sim=self.sim, bitwidth=w, out_bundle="vectors/s_done",
                                    has_tlast=True)

        for c in (self.dut, self.driver, self.done_sink, self.mem):
            self.add_comp(c)

        cmd_if = StreamIF(name=f"{self.name}_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w)
        cmd_if.bind(ep_name="master", endpoint=self.driver.stream_ep)
        cmd_if.bind(ep_name="slave", endpoint=self.dut.s_cmd)
        self.add_if(cmd_if)

        done_if = StreamIF(name=f"{self.name}_done_if", sim=self.sim, clk=self.clk, bitwidth=w)
        done_if.bind(ep_name="master", endpoint=self.dut.s_done)
        done_if.bind(ep_name="slave", endpoint=self.done_sink.stream_ep)
        self.add_if(done_if)

        xbar = AXIMMCrossBarIF(name=f"{self.name}_xbar", sim=self.sim, clk=self.clk,
                               nports_master=2, nports_slave=1, bitwidth=w)
        xbar.bind("master_0", self.dut.m_in)          # gmem0 read (P, X)
        xbar.bind("master_1", self.dut.m_out)         # gmem1 write (Y)
        xbar.bind("slave_0", self.mem.s_mm)
        self.add_if(xbar)
        assign_address_ranges([self.mem.s_mm], [(0, self.arena_words * (w // 8))])


class InterleaverInbandSim:
    """The **procedure** around an :class:`InterleaverInbandTB` graph: materialize the scenario, run the
    pysim golden, and check ``Y[i]=X[P[i]]``.  :meth:`write_scenario` is the single scenario writer both
    backends share."""

    def __init__(self, sizes=(256,), mem_dwidth: int = 64, name: str = "tb",
                 n_cycles: int = 4000, compute_calib_dir: "str | None" = None,
                 platform_dir: "str | None" = None) -> None:
        self.tb = InterleaverInbandTB(name=name, sim=Simulation(), sizes=tuple(sizes),
                                      mem_dwidth=mem_dwidth, n_cycles=n_cycles,
                                      compute_calib_dir=compute_calib_dir, platform_dir=platform_dir)
        self.expected: list[tuple[int, int, np.ndarray]] = []

    def write_scenario(self, root) -> None:
        """Materialize the whole scenario under ``<root>/vectors`` and point the graph at it:
        ``s_cmd`` (the commands), ``mem_in`` (P + X placed in the arena), ``golden`` (the expected Y)."""
        tb = self.tb
        root = Path(root)
        vdir = root / "vectors"
        w = int(tb.mem_dwidth)
        lw = w // 32
        mem_in = np.zeros(tb._nwords_tot, dtype=np.uint64)
        golden = np.zeros(tb._nwords_tot, dtype=np.uint64)
        self.expected = []
        for j, (n, nw, p, x, y) in enumerate(tb._layout):
            P = ((np.arange(n) * 13 + 5) % n).astype(np.uint32)
            Xj = ((np.arange(n, dtype=np.uint64) * 2654435761 + 12345 + j * 7919) & 0xFFFFFFFF)
            mem_in[p:p + nw] = _pack(P, lw)
            mem_in[x:x + nw] = _pack(Xj.astype(np.uint32), lw)
            exp = _pack(Xj[P].astype(np.uint32), lw)
            golden[y:y + nw] = exp
            self.expected.append((y, nw, exp))
        write_burst_bundle(tb.cmd_words, vdir / "s_cmd")
        write_burst_bundle([mem_in], vdir / "mem_in")
        write_burst_bundle([golden], vdir / "golden")
        tb.driver.root = root
        tb.mem.root = root

    def run(self) -> "InterleaverInband":
        """Materialize the scenario into a temp dir, run the SimPy model, and check every gather."""
        with tempfile.TemporaryDirectory() as _root:
            self.write_scenario(_root)
            self.tb.sim.run_sim()
        return self.check()

    def check(self) -> "InterleaverInband":
        """Assert every Y region equals X[P], and one done token landed per job.  Returns the DUT."""
        tb = self.tb
        bpw = int(tb.mem_dwidth) // 8
        ok = True
        for (y, nw, exp) in self.expected:
            got = tb.mem._mem.read(y * bpw, nw).astype(np.uint64)
            ok = ok and np.array_equal(got, exp)
        ndone = len(tb.done_sink.words)
        print(f"[interleaver_inband] sizes={tb._sizes} ok={ok} done_bursts={ndone}")
        assert ok, "interleaver_inband mismatch (Y != X[P])"
        assert ndone >= len(tb._sizes), f"expected >= {len(tb._sizes)} done bursts, got {ndone}"
        return tb.dut


def run_and_check() -> bool:
    InterleaverInbandSim(sizes=(256,)).run()
    InterleaverInbandSim(sizes=(256, 128, 64)).run()          # variable length
    print("interleaver_inband pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
