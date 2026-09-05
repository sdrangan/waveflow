"""fir_block_sim.py — pysim golden harness + XSI testbench GRAPH for :class:`FirBlock`.

Mirrors ``interleaver_inband_sim.py``: a testbench **composite graph** (:class:`FirBlockTB`) the XSI
generator walks, plus a **procedure** (:class:`FirBlockSim`) that materializes the scenario onto disk
and checks the golden.  One statement, two backends.

The scenario is a **program**, not a single job — because the thing under test is *state*, and state is
only observable across firings.  A program is a sequence of steps::

    ("load", taps)      -> LOAD_TAPS: fetch T coefficients, hold them
    ("filter", nsamp)   -> FILTER:    stream a block through, carrying the tail

and the default program is the one ``plans/add_state.md`` specifies for the Stage-2 gate:
``LOAD_TAPS -> FILTER x2 -> LOAD_TAPS -> FILTER``.  That sequence is chosen, not arbitrary: three-plus
filter firings exercise the *carry* rather than only the first block's zeros, and the mid-stream reload
proves the *held* state is actually replaceable.

**The golden is structurally independent of the DUT.**  It filters the whole signal sample-by-sample,
indexing history globally (``x[i-k]``, zero before the start), with the coefficient set switched at
each reload boundary.  The DUT instead sees one block at a time and must reconstruct that history from
``self.carry``.  So block-wise output == global convolution is exactly the statement "the carry state
is correct", and it cannot pass by sharing a bug with the implementation.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.fixpoint import fixed_sum, from_real, mult, quantize
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemoryMod, MemSeg
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

from examples.fir_block.fir_block import (
    DEFAULT_NTAP,
    DEFAULT_SAMP_I,
    DEFAULT_SAMP_W,
    MEM_DW,
    FirBlock,
    FirCmd,
    FirDesc,
    FirOp,
    _as_fixed,
    lane_width,
    nwords,
    pack_samples,
    samp_type,
)

#: The Stage-2 gate program: a load, two blocks (so the carry matters), a mid-stream reload, one more.
DEFAULT_PROGRAM = ("load", "filter", "filter", "load", "filter")

#: Samples per FILTER block in the default scenario.
DEFAULT_BLK = 64


def _tap_set(idx: int, ntap: int, samp_cls) -> np.ndarray:
    """Coefficient set *idx* as stored integers.  Set 0 is a decaying low-pass-ish window, set 1 is a
    different shape entirely — so a *stale* tap set after the reload gives a loudly wrong answer, not
    an off-by-a-few-LSB one."""
    k = np.arange(ntap, dtype=np.float64)
    if idx == 0:
        real = 0.5 ** (1.0 + k / 4.0)
    else:
        real = np.cos(np.pi * k / ntap) / (1.0 + k)
    return np.asarray(from_real(real, samp_cls)).astype(np.int64)


def _stimulus(nsamp: int, seed: int, samp_cls) -> np.ndarray:
    """A deterministic block of samples as stored integers (no RNG state, so both backends agree)."""
    i = np.arange(nsamp, dtype=np.float64)
    real = 0.7 * np.sin(0.13 * i + 0.31 * seed) + 0.2 * np.cos(0.57 * i + seed)
    return np.asarray(from_real(real, samp_cls)).astype(np.int64)


@dataclass
class FirBlockTB(FreeRunMod):
    """The testbench as a component **graph** — a driver on ``s_cmd``, a sink on ``s_done``, one shared
    arena behind both ``m_axi`` bundles, and the :class:`FirBlock` DUT, wired by interfaces.  A
    composite ``FreeRunMod`` so the graph is *walkable*: the XSI harness and the pysim golden are one
    structure, two backends."""

    #: A testbench lowers to the XSI harness (Flow 2's TB target), not to a synthesizable kernel.
    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    program: tuple = DEFAULT_PROGRAM
    blk: int = DEFAULT_BLK
    ntap: HwParam[int] = DEFAULT_NTAP
    samp_w: HwParam[int] = DEFAULT_SAMP_W
    samp_i: HwParam[int] = DEFAULT_SAMP_I
    #: Which realization the DUT emits -- see FirCompute.unroll_lane.  Same golden either way.
    unroll_lane: HwParam[bool] = False
    mem_dwidth: HwParam[int] = MEM_DW
    #: Fixed run bound for the generated XSI main — comfortably past the last completion.
    n_cycles: int = 8000
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    compute_calib_dir: "str | None" = None
    platform_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        t = int(self.ntap)
        self.samp_cls = samp_type(self.samp_w, self.samp_i)
        self._program = tuple(self.program)

        # --- lay the arena out ------------------------------------------------------------------
        # Offsets and region sizes are WORD coordinates; `n` on a command is a SAMPLE count.  At
        # LW = MEM_DW//W samples per word those differ, and conflating them is how the arena and the
        # DUT's ceil(n/LW) read length drift apart.  `nwords` is the one conversion.
        self.lw = lane_width(self.mem_dwidth, self.samp_w)
        cur = 0
        self._steps: list[dict] = []
        n_load = 0
        n_filter = 0
        for step in self._program:
            if step == "load":
                nw = nwords(t, self.lw)
                self._steps.append({"op": FirOp.LOAD_TAPS, "n": t, "nw": nw, "src": cur, "dst": 0,
                                    "zero_state": 0, "tap_set": n_load, "blk_idx": None})
                cur += nw
                n_load += 1
            elif step == "filter":
                n = int(self.blk)
                nw = nwords(n, self.lw)
                src = cur
                cur += nw
                dst = cur
                cur += nw
                # Only the FIRST block starts from zeros; every later block must inherit the carry.
                self._steps.append({"op": FirOp.FILTER, "n": n, "nw": nw, "src": src, "dst": dst,
                                    "zero_state": 1 if n_filter == 0 else 0,
                                    "tap_set": None, "blk_idx": n_filter})
                n_filter += 1
            else:
                raise ValueError(f"unknown program step {step!r}; expected 'load' or 'filter'")
        if n_load == 0 or n_filter == 0:
            raise ValueError("the program needs at least one 'load' and one 'filter' step")
        self.arena_words = cur + 16

        self.mem = MemoryMod(name=f"{self.name}_mem", sim=self.sim, inline=False, clk=self.clk,
                             word_size=w, addr_size=32, nwords_tot=self.arena_words)
        if self.platform_dir is not None:
            from waveflow.calib.bus_model import BusCalib
            self.mem.s_mm.bus_timing = BusCalib(self.platform_dir, clk_freq=self.clk.freq).bus_timing()
        self.mem.alloc(int(self.mem.nwords_tot))
        self.mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
        self.mem.dump_segs = [MemSeg(0, int(self.mem.nwords_tot), "vectors/out")]
        self._nwords_tot = int(self.mem.nwords_tot)

        self.dut = FirBlock(name=f"{self.name}_fir", sim=self.sim, mem_dwidth=w, ntap=t,
                            samp_w=int(self.samp_w), samp_i=int(self.samp_i),
                            unroll_lane=bool(self.unroll_lane),
                            compute_calib_dir=self.compute_calib_dir,
                            platform_dir=self.platform_dir)
        self.cmds = [FirCmd(op=int(s["op"]), src_off=s["src"], n=s["n"], dst_off=s["dst"],
                            zero_state=s["zero_state"], tx_id=i)
                     for i, s in enumerate(self._steps)]
        self.cmd_words = [np.asarray(c.serialize(word_bw=w), dtype=np.uint64) for c in self.cmds]

        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_cmd")
        # s_done is framed (the in-band writer echoes the FirDesc) -- has_tlast.
        self.done_sink = StreamSink(sim=self.sim, bitwidth=w, out_bundle="vectors/s_done",
                                    has_tlast=True)

        for c in (self.dut, self.driver, self.done_sink, self.mem):
            self.add_comp(c)

        # depth 16: a FirCmd frame is 6 words and the driver presents it in one burst.  A testbench
        # channel, so the depth is a pysim modelling choice (plans/pysim_burst_backpressure.md S2).
        cmd_if = StreamIF(name=f"{self.name}_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w,
                          depth=16)
        cmd_if.bind(ep_name="master", endpoint=self.driver.stream_ep)
        cmd_if.bind(ep_name="slave", endpoint=self.dut.s_cmd)
        self.add_if(cmd_if)

        done_if = StreamIF(name=f"{self.name}_done_if", sim=self.sim, clk=self.clk, bitwidth=w)
        done_if.bind(ep_name="master", endpoint=self.dut.s_done)
        done_if.bind(ep_name="slave", endpoint=self.done_sink.stream_ep)
        self.add_if(done_if)

        xbar = AXIMMCrossBarIF(name=f"{self.name}_xbar", sim=self.sim, clk=self.clk,
                               nports_master=2, nports_slave=1, bitwidth=w)
        xbar.bind("master_0", self.dut.m_in)           # gmem0 read (taps, blocks)
        xbar.bind("master_1", self.dut.m_out)          # gmem1 write (y)
        xbar.bind("slave_0", self.mem.s_mm)
        self.add_if(xbar)
        assign_address_ranges([self.mem.s_mm], [(0, self.arena_words * (w // 8))])


class FirBlockSim:
    """The **procedure** around a :class:`FirBlockTB` graph: materialize the scenario, run the pysim
    model, and check every output block against the globally-convolved golden."""

    def __init__(self, program=DEFAULT_PROGRAM, blk: int = DEFAULT_BLK, ntap: int = DEFAULT_NTAP,
                 samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I,
                 unroll_lane: bool = False, mem_dwidth: int = MEM_DW, name: str = "tb",
                 n_cycles: int = 8000, compute_calib_dir: "str | None" = None,
                 platform_dir: "str | None" = None) -> None:
        self.tb = FirBlockTB(name=name, sim=Simulation(), program=tuple(program), blk=int(blk),
                             ntap=int(ntap), samp_w=int(samp_w), samp_i=int(samp_i),
                             unroll_lane=bool(unroll_lane), mem_dwidth=int(mem_dwidth),
                             n_cycles=int(n_cycles),
                             compute_calib_dir=compute_calib_dir, platform_dir=platform_dir)
        self.expected: list[tuple[int, np.ndarray]] = []

    # --- the golden --------------------------------------------------------------------------

    def _golden(self, taps_by_set, blocks) -> list[np.ndarray]:
        """Filter the **whole signal** sample-by-sample with globally-indexed history.

        The coefficient set in force switches at each reload; history before sample 0 is zero.  This
        never mentions a carry — which is the point: the DUT's per-block carry has to reproduce it."""
        tb = self.tb
        t = int(tb.ntap)
        samp_cls = tb.samp_cls
        # Flatten the filter steps into one signal, remembering which tap set each block runs under.
        sig = np.concatenate([b for b in blocks]) if blocks else np.zeros(0, dtype=np.int64)
        cur_set, tap_of_block, base = 0, [], []
        off = 0
        for step in tb._steps:
            if step["op"] == FirOp.LOAD_TAPS:
                cur_set = step["tap_set"]
            else:
                tap_of_block.append(cur_set)
                base.append(off)
                off += step["n"]

        out: list[np.ndarray] = []
        for bi, blk in enumerate(blocks):
            h = _as_fixed(taps_by_set[tap_of_block[bi]], samp_cls)
            n = len(blk)
            ys = np.zeros(n, dtype=np.int64)
            for i in range(n):
                g = base[bi] + i                       # index into the global signal
                win = np.array([sig[g - k] if g - k >= 0 else 0 for k in range(t)], dtype=np.int64)
                acc = fixed_sum(mult(_as_fixed(win, samp_cls), h))
                ys[i] = int(np.asarray(quantize(acc, samp_cls)).reshape(-1)[0])
            out.append(ys)
        return out

    # --- scenario ----------------------------------------------------------------------------

    def write_scenario(self, root) -> None:
        """Materialize the whole scenario under ``<root>/vectors`` and point the graph at it:
        ``s_cmd`` (the program), ``mem_in`` (taps + input blocks placed in the arena), ``golden``."""
        tb = self.tb
        root = Path(root)
        vdir = root / "vectors"
        t = int(tb.ntap)
        w = int(tb.mem_dwidth)

        mem_in = np.zeros(tb._nwords_tot, dtype=np.uint64)
        golden = np.zeros(tb._nwords_tot, dtype=np.uint64)

        # Every arena region goes through the FRAMEWORK packer, the same one the DUT's hook uses and
        # the twin of the generated read/write_framed_stream_lane.  Hand-rolling the packing here is
        # what would make the golden and the RTL disagree the moment LW > 1.
        taps_by_set: list[np.ndarray] = []
        blocks: list[np.ndarray] = []
        for step in tb._steps:
            if step["op"] == FirOp.LOAD_TAPS:
                h = _tap_set(step["tap_set"], t, tb.samp_cls)
                taps_by_set.append(h)
                mem_in[step["src"]:step["src"] + step["nw"]] = pack_samples(h, tb.samp_cls, w)
            else:
                x = _stimulus(step["n"], step["blk_idx"], tb.samp_cls)
                blocks.append(x)
                mem_in[step["src"]:step["src"] + step["nw"]] = pack_samples(x, tb.samp_cls, w)

        self.expected = []
        ys = self._golden(taps_by_set, blocks)
        filt_steps = [s for s in tb._steps if s["op"] == FirOp.FILTER]
        for s, y in zip(filt_steps, ys):
            words = pack_samples(y, tb.samp_cls, w)
            golden[s["dst"]:s["dst"] + s["nw"]] = words
            self.expected.append((s["dst"], words))

        write_burst_bundle(tb.cmd_words, vdir / "s_cmd")
        write_burst_bundle([mem_in], vdir / "mem_in")
        write_burst_bundle([golden], vdir / "golden")
        tb.driver.root = root
        tb.mem.root = root

    def run(self) -> FirBlock:
        """Materialize the scenario into a temp dir, run the SimPy model, and check every block."""
        with tempfile.TemporaryDirectory() as _root:
            self.write_scenario(_root)
            self.tb.sim.run_sim()
        return self.check()

    def check(self) -> FirBlock:
        """Assert every output block equals the globally-convolved golden, and that **every** job —
        including the no-output ``LOAD_TAPS`` — landed exactly one completion on ``s_done``."""
        tb = self.tb
        bpw = int(tb.mem_dwidth) // 8
        for j, (dst, exp) in enumerate(self.expected):
            got = tb.mem._mem.read(dst * bpw, len(exp)).astype(np.uint64)
            if not np.array_equal(got, exp):
                bad = int(np.argmax(got != exp))
                raise AssertionError(
                    f"fir_block block {j} sample {bad}: 0x{int(got[bad]):08x} != "
                    f"golden 0x{int(exp[bad]):08x} (a wrong carry shows up at sample 0)")
        # One echoed FirDesc per command -- LOAD_TAPS included.  The no-output opcode completing is
        # the token-path property the plan flags as the deadlock risk, so assert the COUNT and the
        # ORDER of the echoed tx_ids; never merely that the run finished, because a deadlock here
        # looks exactly like success (the blocks that DID complete still match their golden).
        w = int(tb.mem_dwidth)
        bursts = tb.done_sink.words
        assert len(bursts) == len(tb._steps), (
            f"fir_block: {len(bursts)} completions on s_done, expected {len(tb._steps)} "
            f"(one per command, LOAD_TAPS included)")
        got_ids = [int(FirDesc().deserialize(np.asarray(b), word_bw=w).tx_id) for b in bursts]
        assert got_ids == list(range(len(tb._steps))), (
            f"fir_block: s_done tx_ids {got_ids} != issue order {list(range(len(tb._steps)))}")
        print(f"[fir_block] program={tb._program} blk={tb.blk} ntap={int(tb.ntap)} "
              f"W={int(tb.samp_w)} blocks_ok={len(self.expected)} completions={len(bursts)}")
        return tb.dut


def run_and_check() -> bool:
    FirBlockSim().run()                                            # the Stage-2 gate program
    FirBlockSim(program=("load", "filter"), blk=32).run()           # the degenerate single block
    print("fir_block pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
