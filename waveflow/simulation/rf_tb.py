"""rf_tb.py — reusable pysim RF-environment participants: a block source and a block sink.

The RF-domain twins of :mod:`waveflow.simulation.stream_tb`.  A :class:`StreamDriver` plays words
onto an AXI-Stream; an :class:`RfDataSource` plays ``(n_ch, blksize)`` sample blocks onto an
:class:`~waveflow.hw.rf_sample_if.RFSampIF`.  Both follow the same discipline, and for the same
reason: **the on-disk bundle is the single source**, materialized once and read by whichever backend
is running, so two backends can never start from different bytes.

**Framework, not example code.**  These are put here rather than under ``examples/rf_loopback/`` for
the reason recorded in ``stream_tb``: a harness that lives in one example forces every *other*
example's harness to import across into a sibling.  Nothing here knows about a converter, a schema,
or a kernel — a source reads blocks from a file and puts them; a sink takes blocks and keeps them.

**Pysim-only nodes.**  Neither declares ``kernel_task()`` nor ``bfm_model()``, which is not an
omission — it is the third row of the kinds table (``plans/adc_model.md``): a module with neither
hook is a node that exists in the Python graph and nowhere else.  ``check(RfDataSource,
"xsi_bfm_model")`` answers ``False`` and says why.  Stage 2 replaces them at the RF boundary with
file-backed peers under ``plans/behavioral_edges.md``; nothing about them has to change until then.

Bundle format
-------------
One **burst per block**; each burst is ``n_ch * blksize`` words, row-major over ``(n_ch, blksize)``,
each word one ``float64`` sample serialized through the sanctioned array path
(:func:`~waveflow.hw.arrayutils.write_array` over ``FloatField.specialize(bitwidth=64)``).  This
settles the "bundle format for RF-domain vectors" open question **for stage 1 only**: real-valued
``float64``.  Complex and fixed-point RF vectors are stage 2/4 and will need a manifest field rather
than a convention.  The reuse is deliberate — the existing ``uint64`` burst bundle already carries
per-burst boundaries, which is exactly the block framing, so no new file format appears.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.dataschema import FloatField
from waveflow.hw.hw_module import DynParam, HwModule
from waveflow.hw.rf_sample_if import DEFAULT_RF_RX_DEPTH, RFSampIFRx, RFSampIFTx
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils.burst_io import read_burst_bundle, write_burst_bundle

#: The element type an RF bundle word holds — one ``float64`` sample per 64-bit word.
RF_SAMP_TYPE = FloatField.specialize(bitwidth=64)
RF_WORD_BW = 64


def write_rf_bundle(blocks, bundle_dir: str | Path) -> Path:
    """Write a list of ``(n_ch, blksize)`` sample blocks as a burst bundle — one burst per block."""
    bursts = [np.asarray(write_array(np.asarray(b, dtype=np.float64), elem_type=RF_SAMP_TYPE,
                                     word_bw=RF_WORD_BW), dtype=np.uint64)
              for b in blocks]
    return write_burst_bundle(bursts, bundle_dir)


def read_rf_bundle(bundle_dir: str | Path, n_ch: int, blksize: int) -> list[np.ndarray]:
    """Read a burst bundle back into ``(n_ch, blksize)`` sample blocks.

    The shape is supplied rather than stored: it is a property of the *interface* the blocks ride,
    which the reader already has bound.  A burst whose word count disagrees is an error, not a
    reshape — a bundle written for a different ``blksize`` would otherwise silently re-frame.
    """
    n_ch, blksize = int(n_ch), int(blksize)
    out: list[np.ndarray] = []
    for i, burst in enumerate(read_burst_bundle(bundle_dir)):
        if burst.size != n_ch * blksize:
            raise ValueError(
                f"RF bundle {bundle_dir}: burst {i} has {burst.size} words but the interface "
                f"carries {n_ch}x{blksize} = {n_ch * blksize} samples per block")
        arr = read_array(np.asarray(burst), elem_type=RF_SAMP_TYPE, word_bw=RF_WORD_BW,
                         shape=(n_ch, blksize))
        out.append(np.asarray(arr, dtype=np.float64))
    return out


@dataclass
class RfDataSource(HwModule):
    """Plays a bundle of sample blocks onto an :class:`~waveflow.hw.rf_sample_if.RFSampIF`.

    The RF twin of :class:`~waveflow.simulation.stream_tb.StreamDriver`, and file-driven for the same
    reason.  The bundle is loaded in :meth:`pre_sim` — files exist before the sim starts — and its
    path is resolved at run time against :attr:`root`, never baked.

    It carries **no** ``n_ch`` / ``blksize`` of its own: both are read off the bound interface, which
    is where they physically live.  That is also why the bundle can only be read in ``pre_sim`` and
    not in the constructor — the interface may not be bound yet.
    """

    #: The bundle path.  A :class:`~waveflow.hw.hw_module.DynParam`, matching the ``in_bundle``
    #: pattern: a stable relative string (e.g. ``"vectors/rf_in"``), purity-safe, never a temp path.
    in_bundle: DynParam[str] = ""
    #: Run-time anchor for :attr:`in_bundle`, set by whoever materializes the scenario.  ``None``
    #: resolves against the process cwd.  Runtime config, never part of the structure signature.
    root: Path | None = None
    #: Seconds to wait before offering the first block — a **late** producer.  The RF grid does not
    #: wait for it: every block period that elapses first is an underrun on the interface, zero-filled
    #: and counted.  Default ``0.0`` is the on-time producer.
    start_delay: float = 0.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.blocks: list[np.ndarray] | None = None      # loaded in pre_sim from in_bundle
        self.rf_ep = RFSampIFTx(sim=self.sim)
        self.add_endpoint(self.rf_ep)

    def pre_sim(self) -> None:
        if not self.in_bundle:
            raise ValueError(
                f"RfDataSource '{self.name}'.in_bundle is not set; write the bundle and set root "
                f"before run_sim.")
        p = Path(self.in_bundle)
        if self.root is not None and not p.is_absolute():
            p = Path(self.root) / p
        self.blocks = read_rf_bundle(p, self.rf_ep.n_ch, self.rf_ep.blksize)

    def run_proc(self) -> ProcessGen[None]:
        if self.start_delay > 0:
            yield self.timeout(float(self.start_delay))
        for b in self.blocks:
            yield from self.rf_ep.put(b)

    def bfm_model(self):
        """XSI twin: an ``RfFileSource`` bound to the RF channel, sized in samples per block.

        Its stimulus is the ``in_bundle`` :class:`DynParam`, which the generator emits as a member
        assignment and the model loads in ``pre_sim`` — the same on-disk bundle this node reads in
        pysim, so both play the identical bytes.  Bundle I/O on the **node**, never the edge.

        ``blk_samples`` is read off the bound interface rather than restated, exactly as
        :meth:`pre_sim` reads it.
        """
        from waveflow.build.composite_gen import BfmModel

        return BfmModel("RfFileSource", ports=("rf_ep",),
                        extra_args=(str(int(self.rf_ep.n_ch) * int(self.rf_ep.blksize)),))


@dataclass
class RfDataSink(HwModule):
    """Collects sample blocks off an :class:`~waveflow.hw.rf_sample_if.RFSampIF`.

    The RF twin of :class:`~waveflow.simulation.stream_tb.StreamSink`: schema-blind, keeps everything
    it is given, and dumps its capture to :attr:`out_bundle` in ``post_sim`` so the check happens in
    Python off a file rather than inside the run.
    """

    #: If set, ``post_sim`` writes the captured blocks here as an RF bundle — the same format
    #: :class:`RfDataSource` reads, so a loopback is a **file-to-file byte comparison**.
    out_bundle: DynParam[str] = ""
    #: Run-time anchor for :attr:`out_bundle` (see :attr:`RfDataSource.root`).
    root: Path | None = None
    #: Receiver-side queue depth, in blocks.  Handed to the endpoint, which owns it.
    depth: int = DEFAULT_RF_RX_DEPTH
    #: **Fault injection**: after this many blocks the sink stops consuming forever, so its queue
    #: fills and the interface starts counting overruns.  ``None`` (default) consumes everything.
    #: A testbench knob on a testbench node — the point is to make ``overrun`` a number that has
    #: actually been non-zero.
    stall_after: int | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.blocks: list[np.ndarray] = []
        #: Grid index of each captured block — a dropped block leaves a gap here, which is what
        #: makes loss visible in the *data* and not only in the counter.
        self.indices: list[int] = []
        self.rf_ep = RFSampIFRx(sim=self.sim, depth=int(self.depth))
        self.add_endpoint(self.rf_ep)

    def run_proc(self) -> ProcessGen[None]:
        while True:
            if self.stall_after is not None and len(self.blocks) >= int(self.stall_after):
                yield self.event()       # never triggered: the consumer is stalled from here on
            blk = yield from self.rf_ep.get()
            self.indices.append(int(blk.idx))
            self.blocks.append(np.array(blk.data, copy=True))

    def post_sim(self) -> None:
        if not self.out_bundle:
            return
        p = Path(self.out_bundle)
        if self.root is not None and not p.is_absolute():
            p = Path(self.root) / p
        write_rf_bundle(self.blocks, p)

    def bfm_model(self):
        """XSI twin: an ``RfFileSink`` on the RF channel, dumping its capture to ``out_bundle`` in
        ``post_sim`` — the same format :class:`RfDataSource` reads, so a loopback is a file-to-file
        byte comparison in either backend.

        No ``stall_after`` counterpart: that is pysim fault injection, and the C++ sink always
        drains. A stalling sink would make the channel's drop counter measure the *model* rather
        than the design.
        """
        from waveflow.build.composite_gen import BfmModel

        return BfmModel("RfFileSink", ports=("rf_ep",))
