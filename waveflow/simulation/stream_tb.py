"""stream_tb.py — reusable pysim testbench participants: a stream driver and a stream sink.

The Python-side counterparts of the XSI BFM models (:mod:`waveflow.build.xsi`): a
:class:`StreamDriver` is what an ``AxisMaster`` is at RTL, a :class:`StreamSink` is an ``AxisSlave``.
Both push/collect the same words; only the timing model differs — SimPy events here, cycle-by-cycle
handshakes there.

Framework, not example code: these lived in ``examples/interleaver/mem_stream_sim.py`` because that
is where the first harness was written, which forced every other example's pysim harness to import
across into a sibling example.  Nothing here knows about any particular kernel — nor about any
**schema**: both participants see only raw words + burst boundaries.  The testbench, which is the one
place that knows the schema, does the ``[c.serialize(bw) for c in cmds]`` conversion and hands the
resulting word arrays here.

They are :class:`~waveflow.simulation.simobj.SimObj`\\ s, not
:class:`~waveflow.hw.component.Component`\\ s: a testbench participant needs a ``run_proc`` and an
endpoint, not a synthesizable body, an endpoint registry, or a codegen target.  (Whether that stays
true is an open question — see ``plans/xsi_tb_codegen.md``, "what kind is a TB participant".)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.hw.hw_component import DynParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave, Words
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.utils.burst_io import read_burst_bundle


@dataclass
class StreamDriver(SimObj):
    """Plays a burst bundle onto a stream — a schema-blind, file-driven source.

    The pysim twin of the XSI ``AxisMaster``: it offers each burst's words in turn and lets the
    consumer take them as fast as it accepts.  It never waits for a *response*, which is what leaves
    downstream jobs free to overlap — the same property the RTL testbench depends on.

    **The only vector input is a burst bundle** (a folder; see
    :mod:`waveflow.utils.burst_io`) — never in-memory arrays and never a schema.  That is the point:
    the same on-disk bundle drives the pysim driver *and* the RTL ``AxisMaster``, so both provably
    play the identical bytes.  The testbench, which is the one place that knows the schema, serializes
    its commands (``[c.serialize(bw) for c in cmds]``) and writes the bundle with
    :func:`~waveflow.utils.burst_io.write_burst_bundle`; this driver just points at it.

    The bundle is read **eagerly at construction**, so its files need only exist when the driver is
    built (a testbench may write it to a temporary directory and let that go away).
    """

    bundle: str | Path | None = None   # a burst-bundle directory (waveflow.utils.burst_io)
    bitwidth: int = 64
    #: The bundle the generated XSI ``AxisMaster`` loads in ``pre_sim`` -- a :class:`DynParam`, i.e.
    #: init-time config the harness emits as ``s_cmd.in_bundle = "<in_bundle>";``.  Empty (the default)
    #: emits nothing.  A path rooted by the run dir, e.g. ``"vectors/s_cmd"``.
    in_bundle: DynParam[str] = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.bundle is None:
            raise ValueError("StreamDriver requires bundle=<burst-bundle directory>")
        self.bursts = read_burst_bundle(self.bundle)
        # The path is consumed here (eager load) and not retained: the loaded bursts are the
        # driver's state, and they are deterministic, whereas a (possibly temp-dir) path is not.
        # Keeping the path would make the driver's structure signature depend on it and trip the
        # elaboration param-purity check.
        self.bundle = None
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def run_proc(self) -> ProcessGen[None]:
        for b in self.bursts:
            yield from self.stream_ep.write(np.asarray(b))

    def bfm_model(self):
        """XSI twin: an ``AxisMaster`` on the port ``stream_ep`` is wired to, constructed with empty
        ctor words (``{}``).  Its stimulus comes from the ``in_bundle`` :class:`DynParam`, which the
        generator emits as a member assignment and the model loads in ``pre_sim`` — the same on-disk
        bundle this driver plays in pysim, so both drive from one source."""
        from waveflow.build.composite_gen import BfmModel
        return BfmModel("AxisMaster", ports=("stream_ep",), extra_args=("{}",))


@dataclass
class StreamSink(SimObj):
    """Collects raw word bursts off a stream — a schema-blind sink.

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
