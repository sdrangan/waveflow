"""stream_tb.py — reusable pysim testbench participants: stream drivers and sinks.

The Python-side counterparts of the XSI BFM models (:mod:`waveflow.build.xsi`): a
:class:`CmdDriver` is what an ``AxisMaster`` is at RTL, a :class:`WordSink` is an ``AxisSlave``.
Both push/collect the same words; only the timing model differs — SimPy events here, cycle-by-cycle
handshakes there.

Framework, not example code: these lived in ``examples/interleaver/mem_stream_sim.py`` because that
is where the first harness was written, which forced every other example's pysim harness to import
across into a sibling example.  Nothing here knows about any particular kernel — they are typed by a
stream's ``bitwidth`` and driven by whatever schema instances you hand them.

They are :class:`~waveflow.simulation.simobj.SimObj`\\ s, not
:class:`~waveflow.hw.component.Component`\\ s: a testbench participant needs a ``run_proc`` and an
endpoint, not a synthesizable body, an endpoint registry, or a codegen target.  (Whether that stays
true is an open question — see ``plans/xsi_tb_codegen.md``, "what kind is a TB participant".)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.interface import StreamIFMaster, StreamIFSlave, Words
from waveflow.simulation.simobj import ProcessGen, SimObj


@dataclass
class CmdDriver(SimObj):
    """Sends a list of command-schema instances onto a stream (one burst each).

    The pysim twin of the XSI ``AxisMaster``: it offers each command in turn and lets the consumer
    take them as fast as it accepts.  It never waits for a *response*, which is what leaves
    downstream jobs free to overlap — the same property the RTL testbench depends on.
    """

    cmds: list = field(default_factory=list)
    bitwidth: int = 64
    #: C++ expression naming the words this driver presents in the generated XSI testbench.  The
    #: hand-written half of the TB declares it (from the schema's own serialize() — see
    #: `render_vectors_h`); this participant only says *which* symbol to pass.
    xsi_words: str = "cmd_words"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def run_proc(self) -> ProcessGen[None]:
        for c in self.cmds:
            yield from self.stream_ep.write(c)

    def bfm_model(self):
        """XSI twin: an ``AxisMaster`` offering *cmds*' serialized words on the port ``stream_ep``
        is wired to.  Same words as ``run_proc`` above; only the timing model differs."""
        from waveflow.build.composite_gen import BfmModel
        return BfmModel("AxisMaster", ports=("stream_ep",), extra_args=(self.xsi_words,))


@dataclass
class WordDriver(SimObj):
    """Sends raw word bursts onto a stream (a data source, e.g. for ``MemWStream``)."""

    bursts: list = field(default_factory=list)   # list of np.uint word arrays
    bitwidth: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def run_proc(self) -> ProcessGen[None]:
        for b in self.bursts:
            yield from self.stream_ep.write(np.asarray(b))


@dataclass
class WordSink(SimObj):
    """Collects raw word bursts off a stream (a data sink, e.g. for ``MemRStream``).

    The pysim twin of the XSI ``AxisSlave``: always ready, keeps everything it is given.
    """

    bitwidth: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        self.words: list[np.ndarray] = []
        self.stream_ep = StreamIFSlave(
            sim=self.sim, bitwidth=self.bitwidth, has_tlast=False,
            rx_proc=self.rx_proc, queue_size=64)

    def rx_proc(self, words: Words) -> ProcessGen[None]:
        self.words.append(np.array(words, copy=True))
        yield self.timeout(0)

    def bfm_model(self):
        """XSI twin: an ``AxisSlave`` on the port ``stream_ep`` is wired to — always ready, keeps
        everything, and timestamps each word so the testbench can report completion time."""
        from waveflow.build.composite_gen import BfmModel
        return BfmModel("AxisSlave", ports=("stream_ep",))
