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
One **burst per block**; each burst is row-major over ``(n_ch, blksize)``, each word one ``float64``
*component* serialized through the sanctioned array path
(:func:`~waveflow.hw.arrayutils.write_array` over ``FloatField.specialize(bitwidth=64)``).  The reuse
is deliberate — the existing ``uint64`` burst bundle already carries per-burst boundaries, which is
exactly the block framing, so no new file format appears.

**Real or complex**, and the bundle **says which** (``plans/adc_model.md`` stage B).  A real sample
is one word; a complex sample is two, ``(re, im)`` adjacent.  Those two are indistinguishable as
bytes — an ``n``-sample complex block and a ``2n``-sample real block are the same words — so the
kind is a **manifest field**, :data:`RF_ELEMENT_KEY`, and not a convention.  Before it existed the
format could not *express* an I/Q block rather than merely not having one.

A bundle with no such field is **real**, and that default is a **live contract, not backward
compatibility** — the distinction matters because the second reading invites a cleanup that breaks
the RF XSI gates.  Nothing in this repo is a legacy bundle: no bundle is committed, every one is
written at run time.  The default exists because ``BurstBundle::write`` in ``xsi_bundle.h`` emits
exactly four keys and ``rf_element`` is not among them, so **every bundle the C++ ``RfFileSink``
writes today lacks it** and Python reads those back.  Fixed-point RF vectors remain open, and the
field is named after the numpy dtype so that they are a new value rather than a new key.
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
from waveflow.utils.burst_io import read_burst_bundle, read_burst_meta, write_burst_bundle

#: The element type an RF bundle word holds — one ``float64`` **component** per 64-bit word.  A real
#: sample is one word; a complex sample is two, ``(re, im)``.
RF_SAMP_TYPE = FloatField.specialize(bitwidth=64)
RF_WORD_BW = 64

#: The manifest key naming what one sample of an RF bundle is, and its two values.
#:
#: Named after the **numpy dtype** rather than "real"/"complex" because the next kind this format
#: will need is fixed-point (``plans/adc_model.md``), and a dtype name extends to it while a
#: two-valued adjective does not.
RF_ELEMENT_KEY = "rf_element"
RF_ELEMENT_REAL = "float64"
RF_ELEMENT_COMPLEX = "complex128"


def rf_element(complex_samp: bool) -> str:
    """The manifest value for a real / complex bundle."""
    return RF_ELEMENT_COMPLEX if complex_samp else RF_ELEMENT_REAL


def _split_complex(arr: np.ndarray) -> np.ndarray:
    """``(n_ch, blksize)`` complex → ``(n_ch, 2*blksize)`` float64, ``(re, im)`` per sample.

    **Interleaved, not planar**, and adjacent: a complex sample's two components sit side by side,
    which is the same shape the AXIS word uses (``RfdcSampWord.iq_mode`` puts I and Q in adjacent
    slots).  One convention for "where is Q relative to I" rather than two.

    ``iq_order`` does **not** apply here.  That field says which of I and Q takes the *lower bit
    slot* of a packed word — a bit-packing rule, and there is no packing at this end: the bundle
    stores two whole ``float64`` components, and ``re`` is first because a complex number's real part
    is first.  Keeping the two rules apart is what stops a lab correction to ``iq_order`` silently
    re-meaning every file on disk.

    Written with an explicit stack rather than ``arr.view(np.float64)``: the view is the same bytes
    only while the array is C-contiguous, and a slice of a larger block is not.
    """
    return np.stack([arr.real, arr.imag], axis=-1).reshape(arr.shape[0], -1)


def _join_complex(arr: np.ndarray, n_ch: int, blksize: int) -> np.ndarray:
    """The inverse of :func:`_split_complex`."""
    pairs = np.asarray(arr, dtype=np.float64).reshape(n_ch, blksize, 2)
    return (pairs[..., 0] + 1j * pairs[..., 1]).astype(np.complex128)


def write_rf_bundle(blocks, bundle_dir: str | Path, complex_samp: bool | None = None) -> Path:
    """Write a list of ``(n_ch, blksize)`` sample blocks as a burst bundle — one burst per block.

    **Real or complex**, and the kind is **declared in the manifest** (:data:`RF_ELEMENT_KEY`)
    because on the *reading* side the bytes alone cannot say: an ``n``-sample complex block and a
    ``2n``-sample real block are the same words.

    A complex block becomes twice as many words: two ``float64`` components per sample, ``(re, im)``
    adjacent — see :func:`_split_complex`.

    Parameters
    ----------
    complex_samp : bool, optional
        The kind, when the caller knows it.  ``None`` (the default) **infers it from the blocks**,
        which is right whenever there are any: the data is the truth on this side.  A stated value is
        *checked* against the data rather than applied to it — the same direction
        :func:`read_rf_bundle` uses, so neither function can quietly reinterpret what it is given.

        It exists for the one case inference cannot serve: an **empty** capture, which has no dtype
        to read.  A sink that recorded nothing on a complex edge must still say the bundle was
        complex, or the file claims a kind the edge does not have.

    Raises
    ------
    ValueError
        If the blocks are not all of one kind — a list that mixes them has no single element kind to
        declare, and picking one would silently reinterpret the others — or if a stated
        *complex_samp* disagrees with them.
    """
    blks = [np.asarray(b) for b in blocks]
    kinds = {bool(np.iscomplexobj(b)) for b in blks}
    if len(kinds) > 1:
        raise ValueError(
            f"RF bundle {bundle_dir}: the blocks are a mix of real and complex "
            f"({sum(np.iscomplexobj(b) for b in blks)} of {len(blks)} complex). One bundle carries "
            f"one element kind — it is a single manifest field — so a mixed list has nothing to "
            f"declare.")
    found = bool(kinds.pop()) if kinds else None
    if complex_samp is None:
        complex_samp = bool(found)
    elif found is not None and bool(complex_samp) != found:
        raise ValueError(
            f"RF bundle {bundle_dir}: caller declares {RF_ELEMENT_KEY}="
            f"{rf_element(bool(complex_samp))!r} but the blocks are "
            f"{rf_element(found)!r}. Writing the declaration would put a manifest on the file that "
            f"its own bytes contradict.")
    complex_samp = bool(complex_samp)

    bursts = []
    for b in blks:
        flat = _split_complex(np.asarray(b, dtype=np.complex128)) if complex_samp \
            else np.asarray(b, dtype=np.float64)
        bursts.append(np.asarray(write_array(flat, elem_type=RF_SAMP_TYPE, word_bw=RF_WORD_BW),
                                 dtype=np.uint64))
    return write_burst_bundle(bursts, bundle_dir,
                              extra={RF_ELEMENT_KEY: rf_element(complex_samp)})


def read_rf_bundle(bundle_dir: str | Path, n_ch: int, blksize: int,
                   complex_samp: bool = False) -> list[np.ndarray]:
    """Read a burst bundle back into ``(n_ch, blksize)`` sample blocks.

    The shape is supplied rather than stored: it is a property of the *interface* the blocks ride,
    which the reader already has bound.  A burst whose word count disagrees is an error, not a
    reshape — a bundle written for a different ``blksize`` would otherwise silently re-frame.

    *complex_samp* is the caller's **expectation**, and it is **checked against the manifest** rather
    than used to interpret the bytes.  That direction matters: the two kinds differ only in how many
    words a sample takes, so reading a complex bundle as real yields a block of the wrong length
    (caught) *or*, at half the ``blksize``, a plausible block of interleaved nonsense (not caught).
    The file says what it is; the caller says what it expected; a disagreement is an error.

    **A bundle with no** :data:`RF_ELEMENT_KEY` **is real** — a contract with a *current* writer,
    not a concession to old files.  The C++ ``RfFileSink`` emits no such key (``BurstBundle::write``
    writes four, and this is not one), so every bundle it produces relies on this default and the RF
    XSI gates read them back through here.  Deleting it as legacy support breaks those gates the
    same day.

    The clean end state is to teach the C++ writer to emit the key and make a missing one an
    **error**; with no committed bundles anywhere there is no migration to do.  That is a deliberate
    follow-up rather than a fix folded in here — see ``plans/adc_model.md``.
    """
    n_ch, blksize = int(n_ch), int(blksize)
    complex_samp = bool(complex_samp)

    declared = read_burst_meta(bundle_dir).get(RF_ELEMENT_KEY, RF_ELEMENT_REAL)
    want = rf_element(complex_samp)
    if declared != want:
        raise ValueError(
            f"RF bundle {bundle_dir} declares {RF_ELEMENT_KEY}={declared!r} but the reader expects "
            f"{want!r}. The kinds are not interchangeable: a complex sample is two words and a real "
            f"one is one, so reading one as the other reframes every block. Fix whichever "
            f"declaration is wrong — the interface's complex_samp, or the bundle.")

    words_per_samp = 2 if complex_samp else 1
    want_words = n_ch * blksize * words_per_samp
    out: list[np.ndarray] = []
    for i, burst in enumerate(read_burst_bundle(bundle_dir)):
        if burst.size != want_words:
            raise ValueError(
                f"RF bundle {bundle_dir}: burst {i} has {burst.size} words but the interface "
                f"carries {n_ch}x{blksize} = {n_ch * blksize} {declared} samples per block "
                f"({want_words} words)")
        arr = read_array(np.asarray(burst), elem_type=RF_SAMP_TYPE, word_bw=RF_WORD_BW,
                         shape=(n_ch, blksize * words_per_samp))
        out.append(_join_complex(arr, n_ch, blksize) if complex_samp
                   else np.asarray(arr, dtype=np.float64))
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
        # Every one of the three read off the bound interface, which is where each physically
        # lives -- including complex_samp, which the bundle also states in its manifest, so the
        # read is a CHECK of the file against the edge rather than a reinterpretation of it.
        self.blocks = read_rf_bundle(p, self.rf_ep.n_ch, self.rf_ep.blksize,
                                     complex_samp=self.rf_ep.complex_samp)

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
        # The kind is STATED, not inferred, because a capture may legitimately be empty -- a sink
        # that recorded nothing on a complex edge must still write a complex bundle, or the file
        # claims a kind the edge does not have. With blocks present the two are cross-checked.
        write_rf_bundle(self.blocks, p, complex_samp=self.rf_ep.complex_samp)

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
