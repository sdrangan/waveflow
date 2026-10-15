"""mm_tb.py — a scripted AXI-MM master: the testbench stand-in for a module's memory-mapped slave port
(``plans/unit_harness.md`` D3, Stage 4c).

A memory, or any AXI-MM slave, is harnessed by a master that plays a fixed list of transactions and
records when each ran -- the measurement a calibration of the slave fits.  :class:`MmScript` is that
master in pysim; its C++ twin of the same name (``waveflow/build/xsi/xsi_bfm.h``, over ``AxiMmMaster``
with ``overlap_rw``) plays the same file at RTL.

**The script** (``in_bundle``) is a burst bundle, one burst per op: ``[kind, addr, nwords, not_before,
data...]`` -- kind 1 a write (its ``nwords`` data words follow), 0 a read; ``addr`` a byte address;
``not_before`` the earliest cycle the op may start.  As on the C++ side, reads go in order on the read
channel and writes in order on the write channel, the two independent: one of each may be outstanding,
so a script makes a read and a write collide by giving them the same ``not_before``.

It is the system's **host** by role (a bus master outside the DUT), so it keeps a host's contract:
the script is ``<scenario>/vectors/script``, and what it records goes to its ``trace_dir`` --
``rdata``, each read's data, one burst per read (a host's traces are compared whole, RTL against pysim:
the slave's function) -- and beside it, ``<trace_dir>_timing``: per op, in script order, ``[t_start,
t_end]`` in cycles (not compared: the two backends' timings are what a calibration relates).
``t_start`` is the cycle the op's address phase is presented, ``t_end`` that of its B (write) or RLAST
(read) beat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import DynParam, HwModule
from waveflow.hw.memif import MMIFMaster
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils.burst_io import read_burst_bundle, write_burst_bundle

READ, WRITE = 0, 1


def timing_dir(trace_dir) -> Path:
    """Where a run's op timing goes: beside its traces, ``<trace_dir>_timing``."""
    d = Path(trace_dir)
    return d.parent / f"{d.name}_timing"


def script_op(kind: int, addr: int, nwords: int, not_before: int = 0, data=None) -> np.ndarray:
    """One op of a script: ``[kind, addr, nwords, not_before, data...]``."""
    head = [int(kind), int(addr), int(nwords), int(not_before)]
    body = [] if kind == READ else [int(w) for w in np.asarray(data, dtype=np.uint64)]
    if kind == WRITE and len(body) != nwords:
        raise ValueError(f"a write of {nwords} words carries {len(body)}")
    return np.asarray(head + body, dtype=np.uint64)


@dataclass
class MmScript(HwModule):
    """A bus master that plays a script of reads and writes (see the module docstring)."""

    bitwidth: int = 64
    #: Its clock: the cycles its script and its timing count.
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: The scenario root (the script is ``vectors/script`` under it) and where the traces go: set by
    #: the system flow, as a software host's are.
    scenario: DynParam[str] = ""
    trace_dir: DynParam[str] = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        # One read and one write outstanding, each channel in order: AxiMmMaster's overlap_rw.
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=self.bitwidth,
                            max_outstanding=1)
        self.add_endpoint(self.m)
        #: The script itself, for a pysim run with no scenario on disk (the harness sets it).
        self.script: list | None = None
        self.ops: list[np.ndarray] = []
        self.times: list[tuple[float, float]] = []
        self.rdata: list[np.ndarray] = []
        self.ndone = 0

    def pre_sim(self) -> None:
        super().pre_sim()
        root = self.scenario or getattr(self, "root", None)
        if root:
            self.ops = [np.asarray(b, dtype=np.uint64)
                        for b in read_burst_bundle(Path(root) / "vectors" / "script")]
        elif self.script is not None:            # a pysim run with no scenario on disk
            self.ops = [np.asarray(o, dtype=np.uint64) for o in self.script]
        else:
            raise ValueError(f"{self.name}: no scenario to read the script from, and no script")
        self.times = [(-1.0, -1.0)] * len(self.ops)
        self.rdata = [None] * len(self.ops)

    def _channel(self, kind: int) -> ProcessGen[None]:
        per = float(self.clk.period)
        for i, op in enumerate(self.ops):
            if int(op[0]) != kind:
                continue
            addr, n, nb = int(op[1]), int(op[2]), int(op[3])
            if self.env.now < nb * per:
                yield self.timeout(nb * per - self.env.now)
            t0 = self.env.now
            if kind == WRITE:
                yield from self.m.write(np.asarray(op[4:4 + n], dtype=np.uint64), addr)
            else:
                self.rdata[i] = np.asarray((yield from self.m.read(n, addr)), dtype=np.uint64)
            self.times[i] = (t0 / per, self.env.now / per)
            self.ndone += 1

    def run_proc(self) -> ProcessGen[None]:
        self.env.process(self._channel(WRITE))
        yield from self._channel(READ)

    def done(self) -> bool:
        return self.ndone == len(self.ops)

    def post_sim(self) -> None:
        super().post_sim()
        if not self.trace_dir:
            return
        d = Path(self.trace_dir)
        write_burst_bundle([r for r in self.rdata if r is not None], d / "rdata")
        write_burst_bundle([np.asarray([round(a), round(b)], dtype=np.uint64) for a, b in self.times],
                           timing_dir(d))

    def bfm_model(self):
        """XSI twin: ``MmScript`` (``xsi_bfm.h``) on the slave group ``m`` is wired to; ``scenario``
        and ``trace_dir`` are member assignments, as a software host's are."""
        from waveflow.build.composite_gen import BfmModel
        return BfmModel("MmScript", ports=("m",), extra_args=(str(int(self.bitwidth) // 8),))


__all__ = ["MmScript", "READ", "WRITE", "script_op", "timing_dir"]
