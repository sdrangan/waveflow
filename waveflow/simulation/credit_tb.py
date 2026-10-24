"""credit_tb.py — testbench stand-ins for a credit stream (``plans/unit_harness.md`` D3).

A kernel whose port is one end of a :class:`~waveflow.hw.reverse_stream.CreditStreamIF` cannot be
harnessed with a plain driver or sink: the credit half must **react** to the data half, so it cannot be
a recorded stimulus.  These two stand-ins are the other end, each with a C++ twin of the same name in
``waveflow/build/xsi/xsi_bfm.h``:

* :class:`CreditSink` faces a kernel that **produces** into a credit stream: it takes every burst
  (always ready, recording what it took, as a :class:`~waveflow.simulation.stream_tb.StreamSink`) and
  reports the cumulative words consumed once ``crd_every`` are unreported -- the
  :class:`~waveflow.hw.reverse_stream.CreditStreamSlaveIF` rule;
* :class:`CreditSource` faces a kernel that **consumes** one: it plays its ``in_bundle``'s bursts,
  each written only once the window has room for all of it -- the
  :class:`~waveflow.hw.reverse_stream.CreditStreamMasterIF` rule.

The window is the kernel's: a producer has its consumer's queue depth compiled in, so the harness
builds the link at that depth (the kernel class states it, ``credit_links``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.hw.hw_module import DynParam, HwModule
from waveflow.hw.reverse_stream import CreditStreamMasterIF, CreditStreamSlaveIF
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.stream_tb import _chunk_args
from waveflow.utils.burst_io import read_burst_bundle


@dataclass
class CreditSink(HwModule):
    """The consumer end, for a kernel that produces into a credit stream: takes every burst, keeps it,
    and reports the cumulative words consumed every ``crd_every`` words."""

    bitwidth: int = 64
    crd_every: int = 1
    #: Dumped like a :class:`~waveflow.simulation.stream_tb.StreamSink`'s, relative to ``root``.
    out_bundle: DynParam[str] = ""
    #: Stage 5 policy (:class:`~waveflow.simulation.policy.StandInPolicy`): the percent of cycles this
    #: stand-in stalls on, and the pattern's seed.  ``0`` (the default) emits nothing: polite.
    stall_pct: DynParam[int] = 0
    stall_seed: DynParam[int] = 0
    #: Stage 5 policy (starved credit): each credit report is held this many cycles after it is due.
    credit_delay: DynParam[int] = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = CreditStreamSlaveIF(name=f"{self.name}_ep", sim=self.sim,
                                             bitwidth=self.bitwidth, crd_every=int(self.crd_every))
        self.add_endpoint(self.stream_ep)
        #: The two physical halves, by name and registered, for the C++ twin's ports (data in, credit
        #: out): at RTL a credit stream is two AXI-Stream ports.
        self.fwd, self.crd = self.stream_ep.fwd_ep, self.stream_ep.crd_ep
        for half in (self.fwd, self.crd):
            self.add_endpoint(half)
        self.words: list[np.ndarray] = []

    def run_proc(self) -> ProcessGen[None]:
        from waveflow.simulation.policy import stall_cycles

        ep = self.stream_ep
        while True:
            w = yield from ep.fwd_ep.get()
            self.words.append(np.array(w, copy=True))
            per = 1.0 / ep.fwd_ep.interface.clk.freq
            if self.credit_delay:
                # Starved credit: the report goes out late, the data keeps flowing.  Each report in its
                # own process, all held the same time, so they stay in order.
                self.env.process(self._report_late(w, self.credit_delay * per))
            else:
                yield from ep._consume(w)
            stall = stall_cycles(len(w), self.stall_pct)
            if stall:
                yield self.timeout(stall * per)

    def _report_late(self, w, delay: float) -> ProcessGen[None]:
        yield self.timeout(delay)
        yield from self.stream_ep._consume(w)

    def post_sim(self) -> None:
        super().post_sim()
        root = getattr(self, "root", None)
        if not self.out_bundle or root is None:
            return
        from waveflow.utils.burst_io import write_burst_bundle

        p = Path(self.out_bundle)
        write_burst_bundle(list(self.words), p if p.is_absolute() else Path(root) / p)

    def bfm_model(self):
        """XSI twin: ``CreditSink`` on the data and credit ports this stand-in's halves face."""
        from waveflow.build.composite_gen import BfmModel
        return BfmModel("CreditSink", ports=(("fwd", "crd"),),
                        extra_args=(str(int(self.crd_every)),) + _chunk_args(self.bitwidth))


@dataclass
class CreditSource(HwModule):
    """The producer end, for a kernel that consumes a credit stream: plays ``in_bundle``'s bursts,
    each once the window has room for all of it."""

    bitwidth: int = 64
    #: The bursts to play, relative to ``root`` (written by the harness's scenario).
    in_bundle: DynParam[str] = ""
    #: Stage 5 policy (:class:`~waveflow.simulation.policy.StandInPolicy`): the percent of cycles this
    #: stand-in stalls on, and the pattern's seed.  ``0`` (the default) emits nothing: polite.
    stall_pct: DynParam[int] = 0
    stall_seed: DynParam[int] = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = CreditStreamMasterIF(name=f"{self.name}_ep", sim=self.sim,
                                              bitwidth=self.bitwidth)
        self.add_endpoint(self.stream_ep)
        self.fwd, self.crd = self.stream_ep.fwd_ep, self.stream_ep.crd_ep
        for half in (self.fwd, self.crd):
            self.add_endpoint(half)
        self.bursts = None

    def pre_sim(self) -> None:
        super().pre_sim()
        p = Path(self.in_bundle)
        root = getattr(self, "root", None)
        if root is not None and not p.is_absolute():
            p = Path(root) / p
        self.bursts = read_burst_bundle(p)

    def run_proc(self) -> ProcessGen[None]:
        from waveflow.simulation.policy import stall_cycles

        for b in self.bursts or []:
            yield from self.stream_ep.write(np.asarray(b))
            stall = stall_cycles(len(b), self.stall_pct)
            if stall:
                yield self.timeout(stall / self.stream_ep.fwd_ep.interface.clk.freq)

    def bfm_model(self):
        """XSI twin: ``CreditSource`` on the data and credit ports, with the window the link has."""
        from waveflow.build.composite_gen import BfmModel
        ep = self.stream_ep
        return BfmModel("CreditSource", ports=(("fwd", "crd"),),
                        extra_args=(str(int(ep.depth)), str(int(ep.resp_words)))
                        + _chunk_args(self.bitwidth))


__all__ = ["CreditSink", "CreditSource"]
