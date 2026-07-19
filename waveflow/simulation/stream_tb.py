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

    The bundle is loaded in :meth:`pre_sim` — the same lifecycle phase the C++ ``AxisMaster`` loads
    ``in_bundle`` in, so both backends read their bytes at the same point ("files exist before the sim
    starts").  Its path is resolved at run time against :attr:`root` (never baked), so it works from
    any working directory.
    """

    #: The bundle path both backends read.  A :class:`DynParam`, so the harness emits
    #: ``s_cmd.in_bundle = "<in_bundle>";`` and the C++ ``AxisMaster`` loads it in ``pre_sim``; pysim's
    #: :meth:`pre_sim` reads the same relative path, resolved against :attr:`root`.  A stable relative
    #: string (e.g. ``"vectors/s_cmd"``) — purity-safe, never a temp path.
    in_bundle: DynParam[str] = ""
    #: pysim run-time anchor for :attr:`in_bundle` (set by whoever materializes the scenario — an
    #: example/build dir or a temp dir).  ``None`` → resolve against the process cwd.  Runtime config,
    #: never part of the structure signature (the codegen testbench never sets it).
    root: Path | None = None
    #: DEPRECATED transitional path: a burst-bundle directory read **eagerly at construction**.  Kept
    #: for callers not yet moved to ``in_bundle`` + :meth:`write_scenario`; removed once they migrate.
    bundle: str | Path | None = None
    bitwidth: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        #: ``None`` until loaded.  The eager ``bundle=`` path fills it here; otherwise :meth:`pre_sim`
        #: loads it from :attr:`in_bundle`.
        self.bursts = None
        if self.bundle is not None:
            self.bursts = read_burst_bundle(self.bundle)
            self.bundle = None   # not retained: a (temp) path would trip the param-purity check
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def pre_sim(self) -> None:
        if self.bursts is not None:
            return               # already loaded via the eager bundle= path
        if not self.in_bundle:
            raise ValueError(
                "StreamDriver has neither bundle= nor in_bundle set; give it a bundle to play "
                "(set in_bundle and materialize the scenario before run_sim).")
        p = Path(self.in_bundle)
        if self.root is not None and not p.is_absolute():
            p = Path(self.root) / p
        self.bursts = read_burst_bundle(p)

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
    #: If set, the generated XSI ``AxisSlave`` dumps its capture (words + arrival cycles) to this
    #: bundle in ``post_sim`` -- a :class:`DynParam` the harness emits as ``s_done.out_bundle = "…";``.
    #: Empty (the default) emits nothing.  Lets Python check the output stream + timing off-line.
    out_bundle: DynParam[str] = ""

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
