"""locked_mem.py — a **lock channel** over a shared true-dual-port memory.

``plans/t2p_lock_chan.md`` S1.  Two modules share one :class:`~waveflow.hw.bram.T2pBram`; one of
them cannot stop, the other arrives with a transaction; and the thing that keeps them off each
other's addresses is a pair of messages rather than a phase the design hopes it is in::

    requester                                  owner
        |                                        | holds everything, working
        |-- ACQUIRE [start, end) --------------->|
        |                                        | finishes at most check_period elements
        |                                        | stops touching [start, end)
        |<------------- GRANTED [start, end) ----|
        | reads/writes [start, end)              | continues outside the region (or idles)
        |-- RELEASE ---------------------------->|
        | must not touch the region              | resumes [start, end)

**The message lock is the wrong tool at block cadence.**  Three transfers per swap is free when a
shot plays for milliseconds and dominates when a block turns over in microseconds — which is why
this does not replace :class:`~waveflow.hw.interface.StreamOfBlocksIF` and is not trying to.  SOB
gives full concurrency for 2x the working set at RAII cost and no channel traffic; this gives
``[start, end)`` at any granularity, 1x the storage at one region, and pays three messages per
handover.  Reach for SOB at block cadence and for this at transaction cadence.

The payoff that is not concurrency
----------------------------------
:meth:`~waveflow.hw.bram.BramIFMaster.read` / :meth:`~waveflow.hw.bram.BramIFMaster.write` check the
access *kind* and the address *range*, and nothing else: the invariant the whole shot design rests
on — writer and reader never overlap — has **zero pysim enforcement**.  It is caught only by scanning
a traced XSI run's VCD, which ``waveflow/build/bram_trace.py`` itself concedes is *"a weaker thing
than the assertion firing — a second implementation of the same predicate."*

With a lock, **both endpoints know which range they hold**, so every access can assert membership —
see :meth:`_RegionGuard._check_held`.  That converts a hazard that is currently silent in pysim and
awkward at RTL into a loud one everywhere, and it survives even if the concurrency argument does not.

What S1 builds, and what it does not
------------------------------------
**One region held at a time, one requester, the writer on port A.**  The region bounds are in the
*wire format* from day one — :class:`MemLockCmd` carries ``[start_addr, end_addr)`` and
:class:`MemLockResp` echoes it — so the format does not change when S2 needs two regions.  What is
not built is the allocator, the multi-holder bookkeeping and a second requester.

**A grant is not a fence at RTL.**  At S1 the owner yields the *whole* memory, so nothing in hardware
stops the requester touching an address outside its region.  pysim catches it; the RTL does not.  The
region is enforcement in one backend and documentation in the other, and saying so is the honest
version of "checked".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import (
    BramIF,
    BramIFMaster,
    T2pBram,
    check_bram_access,
    check_bram_element,
    word_element,
)
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, DataSchema, IntField
from waveflow.hw.interface import (
    Interface,
    InterfaceEndpoint,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.simulation.simobj import ProcessGen

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: Width of an address field on the lock channel.  **Not the memory's** ``addr_bits``, and
#: deliberately not: widening a memory must not move where the wire format's fields sit, the same
#: separation :data:`waveflow.hw.rf_samp_buf.IDX_BW` makes for the sample index.  28 is chosen so
#: ``8 + 28 + 28`` is exactly 64 — one word on the width every design in this arc already speaks, so
#: "one message is one beat" is structural rather than a coincidence that holds at some widths.
ADDR_BW = 28

#: :class:`MemLockCmd` opcodes.
LOCK_ACQUIRE = 0
LOCK_RELEASE = 1

#: :class:`MemLockResp` statuses.
LOCK_GRANTED = 0
#: ``end > nelem``, or ``start > end``.  **Refused, never clamped** — the same discipline
#: :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN` follows, and for its reason: a clamped region is a
#: different region, silently.
LOCK_BAD_RANGE = 1

#: Human-readable names, so an assertion says what happened rather than a number.
LOCK_STATUS_NAMES = {LOCK_GRANTED: "LOCK_GRANTED", LOCK_BAD_RANGE: "LOCK_BAD_RANGE"}
LOCK_OPCODE_NAMES = {LOCK_ACQUIRE: "LOCK_ACQUIRE", LOCK_RELEASE: "LOCK_RELEASE"}

AddrField = IntField.specialize(bitwidth=ADDR_BW, signed=False)
LockOpField = IntField.specialize(bitwidth=8, signed=False)


class MemLockCmd(DataList):
    """Requester -> owner: *give me* ``[start_addr, end_addr)``, or *I am done with it*.

    Half-open ``[start, end)`` throughout, for the reason Python slices are: adjacent regions are
    written ``[0, 256)`` and ``[256, 512)`` with no ±1 anywhere, and an empty region is
    ``start == end`` rather than a special case.

    **No application data rides this channel.**  ``RfShotTx``'s ``nsamp`` and ``nrepeat`` stay on the
    channels they were already on.  Putting them here would be the fastest way to make a general
    primitive domain-specific, and the whole argument for building this rather than a one-off abort
    channel is that it is not.
    """

    include_filename: ClassVar[str | None] = "mem_lock_cmd.h"
    elements = {
        "opcode":     {"schema": LockOpField, "description": "LOCK_ACQUIRE or LOCK_RELEASE"},
        "start_addr": {"schema": AddrField,
                       "description": "first element of the region, INCLUSIVE"},
        "end_addr":   {"schema": AddrField,
                       "description": "one past the last element, EXCLUSIVE"},
    }


class MemLockResp(DataList):
    """Owner -> requester: the verdict on an ``ACQUIRE``, with the region echoed back.

    **The echo is redundant at S1 and is here anyway**, for two reasons: a waveform becomes readable
    without cross-referencing the command that produced it, and S2's multi-region case needs the
    correlation without a format change.

    **A** ``LOCK_RELEASE`` **is not answered.**  With one requester there is nothing to race against,
    and a response would be a second thing to get wrong.  The requester's obligation is a hard
    barrier instead: after writing ``RELEASE``, do not touch the region again — which
    :meth:`LockedMemMasterIF.release` makes true in pysim by dropping the region on the spot.
    """

    include_filename: ClassVar[str | None] = "mem_lock_resp.h"
    elements = {
        "status":     {"schema": LockOpField, "description": "LOCK_GRANTED or LOCK_BAD_RANGE"},
        "start_addr": {"schema": AddrField, "description": "the granted region, echoed"},
        "end_addr":   {"schema": AddrField, "description": "echoed"},
    }


#: Schema classes a build emits C++ headers for.  Two, and the whole vocabulary: a request and a
#: verdict.  See :data:`~waveflow.hw.rf_shot_tx.SHOT_TX_SCHEMA_CLASSES` for the same list next door.
LOCK_SCHEMA_CLASSES = [MemLockCmd, MemLockResp]


def lock_bitwidth() -> int:
    """Width of one lock-channel beat — the schema's own, and **exactly one word**.

    Derived rather than taken from the memory's word width, the way
    :func:`~waveflow.hw.reverse_stream.status_bitwidth` is derived rather than declared: a channel
    whose width can disagree with what travels on it is a disagreement waiting to be found at the
    wrap.  Both schemas must pack to one beat, and a change that broke that would break the
    ``read_nb`` poll in :meth:`LockedMemSlaveIF.poll_nb` — a two-beat command read non-blocking is
    half a command — so it is checked here, once, where the reason can still be said.
    """
    widths = {int(cls.get_bitwidth()) for cls in LOCK_SCHEMA_CLASSES}
    if len(widths) != 1:
        raise ValueError(
            f"the lock schemas pack to different widths ({sorted(widths)}); one channel width "
            f"cannot carry both, and two widths would be two channels.")
    bw = widths.pop()
    for cls in LOCK_SCHEMA_CLASSES:
        nw = int(cls.nwords_per_inst(bw))
        if nw != 1:
            raise ValueError(
                f"{cls.__name__} packs to {nw} words at {bw} bits, expected 1. The owner polls the "
                f"command channel with a NON-BLOCKING read, and half a command is not a command.")
    return bw


def region_str(lo: int | None, hi: int | None) -> str:
    """How a held region reads in an error message — ``"nothing"`` when none is held."""
    return "nothing" if lo is None else f"[{int(lo)}, {int(hi)})"


def check_region(lo: int, hi: int, nelem: int) -> int:
    """The range verdict: :data:`LOCK_GRANTED` or :data:`LOCK_BAD_RANGE`.

    One predicate, called by the owner before it grants **and** available to the requester before it
    asks, so the two ends cannot hold different opinions about what a legal region is.
    """
    lo, hi, n = int(lo), int(hi), int(nelem)
    return LOCK_GRANTED if (0 <= lo <= hi <= n) else LOCK_BAD_RANGE


# ---------------------------------------------------------------------------
# What both endpoints share: a held region, and an assertion over it
# ---------------------------------------------------------------------------

class _RegionGuard:
    """The half of an endpoint that knows which addresses it may touch.

    The two ends hold **complementary** regions and that is the whole asymmetry: a requester holds
    ``[lo, hi)`` and nothing else; an owner holds everything *except* the region it has yielded.  So
    one class carries both polarities and :attr:`_holds_complement` selects, rather than two copies
    of one range test that are free to drift.
    """

    #: ``False`` for the requester (holds the region), ``True`` for the owner (holds its complement).
    _holds_complement: ClassVar[bool] = False

    def _init_region(self) -> None:
        #: The region this endpoint has been granted (requester) or has yielded (owner).  ``None``
        #: means "no region is in play", which for a requester is *nothing* and for an owner is
        #: *everything* — the complement of nothing.
        self._lo: int | None = None
        self._hi: int | None = None
        #: Accesses this endpoint made, and grants it took part in.  Counted rather than inferred: a
        #: run in which the lock never changed hands has not exercised the handover it claims to.
        self.n_access = 0
        self.n_grants = 0

    @property
    def region(self) -> tuple[int, int] | None:
        """The region in play as ``(lo, hi)``, or ``None``.  Read-only; the protocol moves it."""
        return None if self._lo is None else (int(self._lo), int(self._hi))

    def may_touch(self, addr: int) -> bool:
        """Whether this endpoint is allowed to touch element *addr* right now."""
        inside = self._lo is not None and int(self._lo) <= int(addr) < int(self._hi)
        return (not inside) if self._holds_complement else inside

    def _check_held(self, addr: int, op: str) -> None:
        """Refuse an access outside what this endpoint holds.

        **This is the payoff.**  ``bram_t2p.v``'s ``$error`` catches the same collision at RTL and
        XSI throws ``$error`` away (``reference-xsi-discards-rtl-text``), so pysim is where it has to
        be caught — and pysim could not catch it at all before there was a lock to ask.
        """
        if self.may_touch(addr):
            self.n_access += 1
            return
        if self._holds_complement:
            what = (f"it has YIELDED {region_str(self._lo, self._hi)} and has not seen the release "
                    f"yet")
        else:
            what = f"it holds {region_str(self._lo, self._hi)}"
        raise RuntimeError(
            f"{type(self).__name__} '{self.name}'.{op}: touched element {int(addr)} while {what}. "
            f"This is the read-during-write collision bram_t2p.v's $error catches at RTL — and XSI "
            f"discards $error, so pysim is where it has to be caught. On the owner's side the usual "
            f"cause is granting BEFORE switching away from the memory: the state change must "
            f"precede the grant, always.")

    def _check_extent(self, addr: int, count: int, op: str) -> None:
        """The same refusal over a whole extent, applied **before any of it lands**.

        Both ends of the extent and nothing between them: the region is contiguous, so an extent
        whose first and last elements are both inside it is entirely inside it.
        """
        n = int(count)
        if n <= 0:
            raise ValueError(
                f"{type(self).__name__} '{self.name}'.{op}: a transfer of {n} elements is not a "
                f"shorter transfer, it is a different question. Ask for at least one.")
        self._check_held(int(addr), op)
        self._check_held(int(addr) + n - 1, op)


# ---------------------------------------------------------------------------
# The requester — holds nothing by default, asks for a region, gives it back
# ---------------------------------------------------------------------------

@dataclass
class LockedMemMasterIF(_RegionGuard, InterfaceEndpoint):
    """The **requester**: the bursty side, which arrives with a transaction.

    Master because this end initiates — it is what writes the command channel — which is the repo's
    convention and not a statement about who owns the memory.  Ownership runs the other way, and
    that is the point: :class:`LockedMemSlaveIF` holds everything by default and yields on request.

    **The rule that maps the roles, in both directions**: the side that cannot stop is the owner; the
    side that arrives with a transaction is the requester.  On TX the loader is the requester and the
    player the owner; on RX (S2) the window reader is the requester and the capture the owner.

    Three physical channels, spliced into a :class:`~waveflow.hw.mem_stream.KernelTask` signature
    adjacent and in this order: the memory port, the command stream out, the response stream in.
    """

    #: What one memory location holds.  Must match the interface and the memory;
    #: :meth:`LockedT2pMemIF.bind` refuses a disagreement.
    element_type: type[DataSchema] = field(default_factory=lambda: word_element(64))
    #: Memory depth in **elements** — the bound every region is checked against.
    nelem: int = 1024
    #: What the requester does through the memory port.  ``"write"`` is the S1 case (the loader); the
    #: field exists because a window *reader* on RX is the same endpoint with ``"read"``.
    access: str = "write"

    mem_ep: BramIFMaster = field(init=False)
    cmd_ep: StreamIFMaster = field(init=False)
    resp_ep: StreamIFSlave = field(init=False)

    type_name = 'locked_mem_master_if'

    def physical_endpoints(self):
        """``(memory port, cmd out, resp in)``.  **This order is the C++ argument order** — a task
        body taking this endpoint takes the three adjacent, in this sequence."""
        return [self.mem_ep, self.cmd_ep, self.resp_ep]

    def __post_init__(self) -> None:
        super().__post_init__()
        owner = f"LockedMemMasterIF '{self.name}'"
        check_bram_element(self.element_type, owner)
        check_bram_access(self.access, owner)
        if int(self.nelem) <= 0:
            raise ValueError(f"{owner}: nelem must be positive, got {self.nelem!r}.")
        bw = lock_bitwidth()
        self.mem_ep = BramIFMaster(sim=self.sim, name=f"{self.name}_mem",
                                   element_type=self.element_type, nelem=int(self.nelem),
                                   access=self.access)
        self.cmd_ep = StreamIFMaster(sim=self.sim, name=f"{self.name}_cmd", bitwidth=bw,
                                     has_tlast=True)
        self.resp_ep = StreamIFSlave(sim=self.sim, name=f"{self.name}_resp", bitwidth=bw,
                                     has_tlast=True)
        self._init_region()
        #: How long each :meth:`acquire` waited, in SimPy seconds.  The measurement
        #: :attr:`LockedMemSlaveIF.check_period` exists to bound — see
        #: :meth:`LockedT2pMemIF.assert_grant_bounded`.
        self.grant_waits: list[float] = []

    # -- the protocol --------------------------------------------------------------------------

    def acquire(self, lo: int, hi: int) -> ProcessGen[int]:
        """Ask for ``[lo, hi)`` and block until the owner answers.  Returns the status.

        **The wait is bounded, and that is the contract rather than a hope.**  The owner polls the
        command channel once per :attr:`LockedMemSlaveIF.check_period` elements of its own work, so
        a grant arrives within that many element-times.  Without such a bound "the owner is busy"
        would be indistinguishable from a deadlock — the same reason
        :data:`~waveflow.hw.rf_shot_tx.SHOT_END` exists as a quiescence probe.

        A :data:`LOCK_BAD_RANGE` grants nothing and leaves the endpoint holding nothing, so the very
        next access raises rather than reading a region the owner never yielded.
        """
        if self._lo is not None:
            raise RuntimeError(
                f"LockedMemMasterIF '{self.name}'.acquire({lo}, {hi}): it already holds "
                f"{region_str(self._lo, self._hi)}. One outstanding request is the S1 contract — a "
                f"second ACQUIRE before the first is released is a protocol error, not a queued "
                f"request (plans/t2p_lock_chan.md, 'Protocol').")
        cmd = MemLockCmd()
        cmd.opcode, cmd.start_addr, cmd.end_addr = LOCK_ACQUIRE, int(lo), int(hi)
        t0 = self.env.now
        yield from self.cmd_ep.write(cmd)
        resp = yield from self.resp_ep.get_schema(MemLockResp)
        self.grant_waits.append(float(self.env.now) - float(t0))
        status = int(resp.status)
        if status == LOCK_GRANTED:
            # The GRANT's echo, not the request: what the owner yielded is what may be touched, and
            # believing the request instead would make a clamp (which S1 refuses, but S2 may not)
            # invisible on this side.
            self._lo, self._hi = int(resp.start_addr), int(resp.end_addr)
            self.n_grants += 1
        return status

    def release(self) -> ProcessGen[None]:
        """Give the region back.  **A barrier, not a hint** — the owner may resume the instant it
        sees this, so the region is dropped here *before* the message goes out."""
        if self._lo is None:
            raise RuntimeError(
                f"LockedMemMasterIF '{self.name}'.release(): it holds nothing. A release with no "
                f"acquire ahead of it would tell the owner to resume a region it never yielded.")
        cmd = MemLockCmd()
        cmd.opcode, cmd.start_addr, cmd.end_addr = LOCK_RELEASE, int(self._lo), int(self._hi)
        self._lo = self._hi = None
        yield from self.cmd_ep.write(cmd)

    # -- memory access, each gated on the held region --------------------------------------------
    #
    # The endpoint FORWARDS `BramIFMaster`'s access methods rather than reimplementing them: the lock
    # decides *when* they are legal and changes nothing about what they do or what they cost.  There
    # is nothing new to learn, and no second timing model to keep in step with `bram.py`'s.

    def write(self, addr: int, value) -> None:
        """One element, in place.  Refused outside the held region."""
        self._check_held(int(addr), "write")
        self.mem_ep.write(int(addr), value)

    def read(self, addr: int):
        """One element.  Refused outside the held region."""
        self._check_held(int(addr), "read")
        return self.mem_ep.read(int(addr))

    def array_ref(self, addr: int, count: int) -> np.ndarray:
        """A live view of ``[addr, addr+count)``.  Refused unless the whole extent is held."""
        self._check_extent(int(addr), int(count), "array_ref")
        return self.mem_ep.array_ref(int(addr), int(count))

    def read_pipelined(self, element_type: type[DataSchema], count: int, addr: int):
        """``(data, tstart)`` for *count* elements from *addr*.  Refused unless the extent is held."""
        self._check_extent(int(addr), int(count), "read_pipelined")
        return (yield from self.mem_ep.read_pipelined(element_type, int(count), int(addr)))

    def write_pipelined(self, data, addr: int, t_start: float | None = None):
        """Write *data* at *addr*, the burst anchored at *t_start*.

        **The anchor is what makes the memory write free.**  ``write_pipelined`` elapses ``count``
        cycles at II=1, and with *t_start* in the past it elapses less — that shortening *is* the
        overlap.  Passing the anchor a ``get_pipelined`` back-calculated makes the two phases cost
        ``max(a, b)`` instead of ``a + b``, which is what the II=1 RTL actually does: a word arrives
        and a word is stored in the same cycle.  Dropping it charges the design twice for one
        pipeline.
        """
        n = _extent_of(data)
        self._check_extent(int(addr), n, "write_pipelined")
        yield from self.mem_ep.write_pipelined(data, int(addr), t_start=t_start)


def _extent_of(data) -> int:
    """How many elements a ``write_pipelined`` payload covers.

    Asks the ``DataArray`` for its values rather than its shape, because the region check has to see
    the same count :meth:`~waveflow.hw.bram.BramIFMaster.write_pipelined` will store — and that one
    flattens.  A count taken from a nominal shape would check a different extent from the one that
    lands.
    """
    return int(np.asarray(getattr(data, "val", data)).reshape(-1).shape[0])


# ---------------------------------------------------------------------------
# The owner — holds everything, yields on request
# ---------------------------------------------------------------------------

@dataclass
class LockedMemSlaveIF(_RegionGuard, InterfaceEndpoint):
    """The **owner**: the continuous side, which cannot stop.

    Slave because it never initiates — it answers.  It holds the whole memory by default and must
    yield on request, which is the inverse of what the names suggest and is stated here for that
    reason: *master* and *slave* say who drives the channel, not who owns the storage.

    Three physical channels, in this order: the memory port, the command stream **in**, the response
    stream **out**.
    """

    _holds_complement: ClassVar[bool] = True

    element_type: type[DataSchema] = field(default_factory=lambda: word_element(64))
    nelem: int = 1024
    #: What the owner does through the memory port.  ``"read"`` is the S1 case (the player), and it
    #: is also all :class:`~waveflow.hw.bram.T2pBram` permits on port B — ``bram_t2p.v``'s ``$error``
    #: is written one-sided, so a writing owner would be invisible to the design's only real check.
    access: str = "read"
    #: **The contract that makes a grant bounded**: the maximum elements of its own work the owner
    #: may do between polls of the lock channel.  A grant therefore arrives within ``check_period``
    #: element-times, and :meth:`LockedT2pMemIF.assert_grant_bounded` is a gate asserting it.
    #:
    #: It is a *modelling and design* number rather than a wire: nothing on the channel carries it,
    #: and the C++ twin spells it as the trip count of the loop around its ``read_nb``.
    check_period: int = 16

    mem_ep: BramIFMaster = field(init=False)
    cmd_ep: StreamIFSlave = field(init=False)
    resp_ep: StreamIFMaster = field(init=False)

    type_name = 'locked_mem_slave_if'

    def physical_endpoints(self):
        """``(memory port, cmd in, resp out)``.  The C++ argument order, as on the requester."""
        return [self.mem_ep, self.cmd_ep, self.resp_ep]

    def __post_init__(self) -> None:
        super().__post_init__()
        owner = f"LockedMemSlaveIF '{self.name}'"
        check_bram_element(self.element_type, owner)
        check_bram_access(self.access, owner)
        if int(self.nelem) <= 0:
            raise ValueError(f"{owner}: nelem must be positive, got {self.nelem!r}.")
        if int(self.check_period) < 1:
            raise ValueError(
                f"{owner}: check_period={self.check_period!r}. An owner that never polls makes the "
                f"requester's wait unbounded, and an unbounded wait is indistinguishable from a "
                f"deadlock.")
        bw = lock_bitwidth()
        self.mem_ep = BramIFMaster(sim=self.sim, name=f"{self.name}_mem",
                                   element_type=self.element_type, nelem=int(self.nelem),
                                   access=self.access)
        self.cmd_ep = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=bw,
                                    has_tlast=True)
        self.resp_ep = StreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=bw,
                                      has_tlast=True)
        self._init_region()

    # -- the protocol --------------------------------------------------------------------------

    def poll_nb(self) -> ProcessGen["MemLockCmd | None"]:
        """Take a command **if one is waiting**, else ``None``.  Never blocks.

        Called exactly once per :attr:`check_period` elements of the owner's own work, which is what
        makes the requester's wait a stated number.  Non-blocking because the owner cannot stop: an
        empty channel means *no news*, never *wait*.
        """
        return (yield from self.cmd_ep.get_schema_nb(MemLockCmd))

    def grant(self, lo: int, hi: int) -> ProcessGen[int]:
        """Yield ``[lo, hi)`` and answer.  Returns the status that went out.

        **Call this only after the owner has already stopped touching the region.**  Granting while
        still reading it is precisely the collision the whole interface exists to prevent, and it is
        the one ordering everything turns on — so this method takes the region out of the owner's
        hands *before* the response goes on the wire, and :meth:`_RegionGuard._check_held` raises on
        the very next access if the design's own state machine has not moved yet.

        A bad range answers :data:`LOCK_BAD_RANGE` and yields **nothing** — refused, not clamped.
        """
        status = check_region(lo, hi, int(self.nelem))
        if status == LOCK_GRANTED:
            if self._lo is not None:
                raise RuntimeError(
                    f"LockedMemSlaveIF '{self.name}'.grant({lo}, {hi}): it has already yielded "
                    f"{region_str(self._lo, self._hi)} and has not seen the release. One region at "
                    f"a time is the S1 contract; a second holder is S2's allocator.")
            self._lo, self._hi = int(lo), int(hi)
            self.n_grants += 1
        resp = MemLockResp()
        resp.status, resp.start_addr, resp.end_addr = int(status), int(lo), int(hi)
        yield from self.resp_ep.write(resp)
        return status

    def resume(self) -> None:
        """Take the region back on a ``RELEASE``.  Untimed — nothing moves, a claim is dropped.

        A plain method rather than a generator on purpose, the same statement
        :meth:`~waveflow.hw.bram.BramIFMaster.read` makes with its missing ``yield``: no simulated
        time passes, because in hardware this is a register bit changing on the beat the message
        arrives.
        """
        if self._lo is None:
            raise RuntimeError(
                f"LockedMemSlaveIF '{self.name}'.resume(): it has yielded nothing, so there is "
                f"nothing to take back. A RELEASE with no grant ahead of it means the two ends "
                f"disagree about whose turn it is — which is the state the protocol has no way to "
                f"recover from and therefore refuses to enter.")
        self._lo = self._hi = None

    def handle_nb(self) -> ProcessGen["MemLockCmd | None"]:
        """Poll, and apply a ``RELEASE`` on the spot.  Returns the command, or ``None``.

        The convenience the S1 owner actually wants: a ``RELEASE`` needs no decision and no answer,
        so handling it here keeps the design's state machine to the case that *does* — an
        ``ACQUIRE``, which the design must answer only after it has switched away from the region.
        An ``ACQUIRE`` is returned untouched precisely because granting it is not this method's call
        to make.
        """
        cmd = yield from self.poll_nb()
        if cmd is not None and int(cmd.opcode) == LOCK_RELEASE:
            self.resume()
        return cmd

    # -- memory access, each gated on what has NOT been yielded ----------------------------------

    def read(self, addr: int):
        """One element.  Refused inside a region that has been yielded."""
        self._check_held(int(addr), "read")
        return self.mem_ep.read(int(addr))

    def write(self, addr: int, value) -> None:
        """One element.  Refused inside a region that has been yielded."""
        self._check_held(int(addr), "write")
        self.mem_ep.write(int(addr), value)

    def array_ref(self, addr: int, count: int) -> np.ndarray:
        """A live view of ``[addr, addr+count)``.  Refused if any of it has been yielded."""
        self._check_extent(int(addr), int(count), "array_ref")
        return self.mem_ep.array_ref(int(addr), int(count))

    def read_pipelined(self, element_type: type[DataSchema], count: int, addr: int):
        """``(data, tstart)`` for *count* elements from *addr*.  Refused if any of it is yielded."""
        self._check_extent(int(addr), int(count), "read_pipelined")
        return (yield from self.mem_ep.read_pipelined(element_type, int(count), int(addr)))

    def write_pipelined(self, data, addr: int, t_start: float | None = None):
        """Write *data* at *addr*, anchored at *t_start*.  Refused if any of it is yielded."""
        self._check_extent(int(addr), _extent_of(data), "write_pipelined")
        yield from self.mem_ep.write_pipelined(data, int(addr), t_start=t_start)


# ---------------------------------------------------------------------------
# The interface — four channels, two binds
# ---------------------------------------------------------------------------

@dataclass
class LockedT2pMemIF(Interface):
    r"""A shared :class:`~waveflow.hw.bram.T2pBram` plus the two streams that arbitrate it.

    **Four channels under one name, and two of them are not channels.**  The two ``StreamIF``\ s are
    ordinary internal FIFOs and lower as such (:meth:`physical_interfaces`); the two
    :class:`~waveflow.hw.bram.BramIF`\ s are **wrapper wires** and lower through
    :meth:`rtl_interfaces`, because an ``add_if`` edge is an internal channel and both its endpoints
    stop being boundary ports — which is exactly what a memory port must not do.  That split is why
    this interface cannot be *one* registry entry, and why
    :meth:`~waveflow.hw.hw_module.HwModule.add_if` sweeps :meth:`rtl_interfaces` into the RTL
    registry rather than making every consumer remember.

    You do not construct the four and you do not bind them; the two ``bind`` calls are the whole
    wiring, exactly as with :attr:`~waveflow.hw.reverse_stream.AckedStreamIF.fwd_if` / ``ack_if``::

        lk = LockedT2pMemIF(sim=sim, name="buf_lock", clk=clk,
                            element_type=word_element(64), nelem=1024, memory=mem)
        lk.bind("master", req)      # the requester
        lk.bind("slave", own)       # the owner

    **The memory's port A is the writer and port B the reader**, which is not a preference: the
    ``$error`` in ``bram_t2p.v`` is written one-sided (*A writes while B touches the same address*),
    so a writing port B would be invisible to the design's only real RTL check.  S1 keeps the writer
    on A and does not touch that; making the assertion symmetric is S2's decision and it edits every
    example's copied ``xsi/bram_t2p.v``.
    """

    #: The clock the four channels are timed in.  Required: a pipelined memory transfer is measured
    #: in cycles, and the ``BramIF``\ s take theirs from here.
    clk: Clock | None = None
    #: What one memory location holds — must match both endpoints and the memory.
    element_type: type[DataSchema] = field(default_factory=lambda: word_element(64))
    #: Memory depth in **elements**; bounds every region.
    nelem: int = 1024
    #: The :class:`~waveflow.hw.bram.T2pBram` this lock arbitrates.  The interface wires both of its
    #: ports; the *composite* still registers the memory itself with ``add_rtl_mod``, because a
    #: memory is a module in the design and not a detail of a wire.
    memory: T2pBram | None = None
    #: Lock command queue depth in beats.  **1**: there is one outstanding request at S1 by
    #: construction, so a deeper queue could only hold a command for a handover that has already
    #: happened.
    cmd_depth: int = 1
    #: Response queue depth.  1, for the symmetric reason: one answer per request.
    resp_depth: int = 1

    cmd_if: StreamIF = field(init=False)
    resp_if: StreamIF = field(init=False)
    wr_if: BramIF = field(init=False)
    rd_if: BramIF = field(init=False)

    type_name = 'locked_t2p_mem_if'

    def physical_interfaces(self):
        """The two lock streams.  **The memory wires are not here** — see :meth:`rtl_interfaces`.

        In hardware there is no locked memory: there are two FIFOs inside the kernel and two
        ``mode=bram`` port pairs leaving it.
        """
        return [self.cmd_if, self.resp_if]

    def rtl_interfaces(self):
        """The two ``BramIF`` wrapper wires — the kernel's memory ports joined to the memory.

        Registered by :meth:`~waveflow.hw.hw_module.HwModule.add_if` into the *RTL* registry, never
        the internal-edge one.  A ``BramIF`` among the internal edges would make the kernel's memory
        ports disappear into a FIFO that does not exist.
        """
        return [self.wr_if, self.rd_if]

    def __post_init__(self) -> None:
        self.endpoint_names = ('master', 'slave')
        if self.clk is None:
            raise ValueError(
                f"clock must be provided for {type(self).__name__}: the memory transfers on this "
                f"interface are measured in CYCLES, and an untimed one would elapse none.")
        if self.memory is None:
            raise ValueError(
                f"{type(self).__name__} '{self.name}': memory= is required. The lock arbitrates a "
                f"specific T2pBram and wires both of its ports; without one there is nothing to "
                f"hold and the two endpoints would be talking about different storage.")
        super().__post_init__()
        check_bram_element(self.element_type, f"{type(self).__name__} '{self.name}'")
        if self.memory.element_type is not self.element_type:
            raise ValueError(
                f"{type(self).__name__} '{self.name}': the lock's element is "
                f"{self.element_type.__name__} but the memory holds "
                f"{self.memory.element_type.__name__}. Two types of the same width are the quieter "
                f"aliasing bug: every address lines up and every read returns a correctly-shaped "
                f"wrong number.")
        if int(self.memory.nelem) != int(self.nelem):
            raise ValueError(
                f"{type(self).__name__} '{self.name}': nelem={int(self.nelem)} but the memory holds "
                f"{int(self.memory.nelem)}. The region bound and the address range are one number; "
                f"a disagreement grants regions the memory does not have.")
        bw = lock_bitwidth()
        self.cmd_if = StreamIF(name=f"{self.name}_cmd", sim=self.sim, clk=self.clk,
                               bitwidth=bw, depth=int(self.cmd_depth))
        self.resp_if = StreamIF(name=f"{self.name}_resp", sim=self.sim, clk=self.clk,
                                bitwidth=bw, depth=int(self.resp_depth))
        self.wr_if = BramIF(name=f"{self.name}_wr", sim=self.sim, clk=self.clk)
        self.rd_if = BramIF(name=f"{self.name}_rd", sim=self.sim, clk=self.clk)
        # The memory side is bound now; the accessor sides arrive with the endpoints.  Binding the
        # far end here is what makes "you do not bind the four" true.
        self.wr_if.bind("slave", self.memory.wr_port)
        self.rd_if.bind("slave", self.memory.rd_port)

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name not in ('master', 'slave'):
            raise KeyError(
                f"LockedT2pMemIF has only 'master' (the requester) and 'slave' (the owner) sides, "
                f"but got '{ep_name}'")
        want = LockedMemMasterIF if ep_name == 'master' else LockedMemSlaveIF
        role = "the requester" if ep_name == 'master' else "the owner"
        if not isinstance(endpoint, want):
            raise TypeError(
                f"'{ep_name}' side of LockedT2pMemIF must bind to {want.__name__} ({role})")
        # BEFORE the sub-interface binds, so a geometry disagreement names the endpoint's own
        # declaration rather than surfacing as a bare width mismatch inside BramIF.bind.
        if endpoint.element_type is not self.element_type:
            raise ValueError(
                f"LockedT2pMemIF '{self.name}': the lock's element is "
                f"{self.element_type.__name__} but the '{ep_name}' endpoint's is "
                f"{endpoint.element_type.__name__}.")
        if int(endpoint.nelem) != int(self.nelem):
            raise ValueError(
                f"LockedT2pMemIF '{self.name}': nelem={int(self.nelem)} but the '{ep_name}' "
                f"endpoint declares {int(endpoint.nelem)}. The region bound is one number; a "
                f"disagreement means one end can ask for what the other cannot check.")
        # WHICH MEMORY PORT A SIDE GETS IS DECIDED BY WHAT IT DOES, NOT BY WHICH SIDE IT IS.
        #
        # S1 wired master -> port A and slave -> port B, which is right for TX and only for TX: there
        # the requester is the loader (writes) and the owner is the player (reads).  RX inverts the
        # directions and NOT the roles -- the capture cannot stop, so it is the owner, and it WRITES;
        # the window reader arrives with a transaction, so it is the requester, and it READS.  Role
        # and direction are independent axes and S1 accidentally coupled them; binding by `access`
        # uncouples them, and `BramIF.bind` then checks the pairing it always checked.
        #
        # Port A stays the writer's in both directions, and that is not a preference:
        # ``bram_t2p.v``'s ``$error`` is written one-sided (*A writes while B touches the same
        # address*), so a writing port B would be invisible to the memory's only real RTL check.
        # See :meth:`_mem_if_for`.
        self._mem_if_for(endpoint, ep_name).bind("master", endpoint.mem_ep)
        if ep_name == 'master':
            self.cmd_if.bind("master", endpoint.cmd_ep)
            self.resp_if.bind("slave", endpoint.resp_ep)
        else:
            self.cmd_if.bind("slave", endpoint.cmd_ep)
            self.resp_if.bind("master", endpoint.resp_ep)
        super().bind(ep_name, endpoint)

    def _mem_if_for(self, endpoint: InterfaceEndpoint, ep_name: str) -> BramIF:
        """The ``BramIF`` *endpoint* belongs on — port A if it writes, port B if it reads.

        Refused rather than defaulted when both sides declare the same direction: two writers is the
        collision ``bram_t2p.v`` cannot see (its ``$error`` names port A as the writer), and two
        readers is a lock arbitrating a memory nothing ever fills.  Either is a wiring mistake whose
        symptom would be a correct-looking design that moves no data.
        """
        want_write = str(endpoint.access) != "read"          # "write" or "readwrite"
        other = self.endpoints.get('slave' if ep_name == 'master' else 'master')
        if other is not None and (str(other.access) != "read") == want_write:
            raise ValueError(
                f"LockedT2pMemIF '{self.name}': the '{ep_name}' endpoint declares "
                f"access={endpoint.access!r} and the other side declares {other.access!r}, so both "
                f"want the same memory port. A lock arbitrates ONE writer against ONE reader — two "
                f"writers is the collision bram_t2p.v's one-sided $error cannot see, and two readers "
                f"is a memory nothing fills. On TX the requester writes and the owner reads; on RX "
                f"it is the other way round.")
        return self.wr_if if want_write else self.rd_if

    # -- what a gate reads off a finished run ----------------------------------------------------

    @property
    def n_grants(self) -> int:
        """Grants that went out.  A run in which this is zero has not exercised the handover."""
        slave = self.endpoints.get("slave")
        return 0 if slave is None else int(slave.n_grants)

    def assert_handover_happened(self, n_grants: int = 1) -> None:
        """After a run: the lock actually changed hands, and both ends agree how often.

        A guard that never fired is not evidence that the invariant held — it is evidence that
        something ran.  The same statement
        :meth:`~waveflow.hw.rf_shot_buf.RfShotBuf.assert_phases_separated` makes next door.
        """
        m, s = self.endpoints.get("master"), self.endpoints.get("slave")
        if m is None or s is None:
            raise AssertionError(
                f"LockedT2pMemIF '{self.name}' is not bound on both sides; there is nothing to say "
                f"about a run.")
        if int(s.n_grants) != int(n_grants) or int(m.n_grants) != int(n_grants):
            raise AssertionError(
                f"LockedT2pMemIF '{self.name}': the owner granted {int(s.n_grants)} region(s) and "
                f"the requester took {int(m.n_grants)}, expected {int(n_grants)} each. A region "
                f"granted and not taken is a handover that half happened, which the sample data "
                f"cannot show.")
        if s.region is not None or m.region is not None:
            raise AssertionError(
                f"LockedT2pMemIF '{self.name}': the run ended mid-handover — the owner has yielded "
                f"{region_str(*(s.region or (None, None)))} and the requester holds "
                f"{region_str(*(m.region or (None, None)))}. A region that was taken and never "
                f"released leaves the owner permanently off part of its own memory.")

    def assert_grant_bounded(self, max_seconds: float) -> None:
        """After a run: **every** grant arrived within *max_seconds* of being asked for.

        Seconds rather than cycles, and that is deliberate.  ``check_period`` is a count of the
        owner's *own work*, and what an element of that work costs is the owner's business — a player
        paced by a DAC spends a converter word-time per element, not a fabric cycle.  The interface
        will not invent the rate; the gate knows it and passes the product.
        """
        m = self.endpoints.get("master")
        if m is None or not m.grant_waits:
            raise AssertionError(
                f"LockedT2pMemIF '{self.name}': no grant was ever waited for, so a bound on the "
                f"wait proves nothing about this run.")
        worst = max(m.grant_waits)
        if worst > float(max_seconds):
            raise AssertionError(
                f"LockedT2pMemIF '{self.name}': the slowest grant took {worst:.6g} s, over the "
                f"{float(max_seconds):.6g} s check_period bound. Either the owner is polling less "
                f"often than it declared, or it blocked between polls — and an unbounded wait for a "
                f"grant is indistinguishable from a deadlock.")
