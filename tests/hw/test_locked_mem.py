"""The lock channel as a *module*: the wire format, the protocol, and the region assertion.

``plans/t2p_lock_chan.md`` S1, checkpoint 1.  These are the claims :mod:`waveflow.hw.locked_mem`
makes without a toolchain and without a consumer.

**The region assertion gets most of the file, because it is the payoff.**  The plan's argument for
building an interface here rather than a one-off abort channel rests on one claim: *region ownership
is checkable in pysim; address collision is not*.  A guard that has never fired is not evidence, and
this guard is unreachable in a correctly wired design — so every one of its refusals is provoked
deliberately, from both sides, including the ordering the whole protocol turns on (an owner that
grants **before** it stops touching the memory).

The counterpart at RTL is a VCD scan for read-during-write, and it means nothing unless paired with
a dirty run known to collide (``reference-xsi-discards-rtl-text``).  These tests are that pairing's
pysim half: the dirty runs are here, by name.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.bram import BramIF, T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.locked_mem import (
    ADDR_BW,
    LOCK_ACQUIRE,
    LOCK_BAD_RANGE,
    LOCK_GRANTED,
    LOCK_RELEASE,
    LOCK_SCHEMA_CLASSES,
    LockedMemMasterIF,
    LockedMemSlaveIF,
    LockedT2pMemIF,
    MemLockCmd,
    MemLockResp,
    check_region,
    lock_bitwidth,
)
from waveflow.hw.interface import StreamIF
from waveflow.simulation.simulation import Simulation

WORD = word_element(64)
NELEM = 64
CHECK_PERIOD = 4


# ---------------------------------------------------------------------------
# The bench: a memory, a lock, and the two ends of it
# ---------------------------------------------------------------------------

class Bench:
    """Everything the protocol needs and nothing else — no modules, no composite, no codegen.

    Deliberately below the :class:`~waveflow.hw.hw_freerun.FreeRunMod` level: what is on trial here
    is the *interface*, and wrapping it in two tasks would mean a failure could be either end's.  The
    bodies are plain SimPy processes written in the test, which is also what lets one of them be
    written **wrong** on purpose.
    """

    def __init__(self, nelem: int = NELEM, check_period: int = CHECK_PERIOD) -> None:
        self.sim = Simulation()
        self.nelem = int(nelem)
        self.clk = Clock(name="clk", freq=250e6)
        self.mem = T2pBram(sim=self.sim, name="mem", element_type=WORD, nelem=self.nelem)
        self.req = LockedMemMasterIF(sim=self.sim, name="loader", element_type=WORD,
                                     nelem=self.nelem, access="write")
        self.own = LockedMemSlaveIF(sim=self.sim, name="player", element_type=WORD,
                                    nelem=self.nelem, access="read",
                                    check_period=int(check_period))
        self.lk = LockedT2pMemIF(sim=self.sim, name="buf_lock", clk=self.clk, element_type=WORD,
                                 nelem=self.nelem, memory=self.mem)
        self.lk.bind("master", self.req)
        self.lk.bind("slave", self.own)

    def run(self, *procs) -> None:
        """The three-phase lifecycle, spelled out — the same one ``run_pysim`` writes by hand.

        ``env.run()`` takes no bound: every process here terminates, and a run that does *not* return
        is a deadlock the test should fail on rather than time out around.
        """
        sim = self.sim
        for obj in sim._sim_objs:
            obj.pre_sim()
        for obj in sim._sim_objs:
            p = obj.run_proc()
            if p is not None:
                sim.env.process(p)
        for p in procs:
            sim.env.process(p)
        sim.env.run()
        for obj in sim._sim_objs:
            obj.post_sim()


def ramp(n: int, base: int = 1000) -> np.ndarray:
    """*n* distinguishable words.  Distinguishable matters: a zeroed memory reads as zeros, so a
    constant payload cannot tell a write that landed from one that never happened."""
    return np.arange(base, base + int(n), dtype=np.uint64)


def owner_body(b: Bench, n_chunks: int, *, grant_before_switch: bool = False, log=None):
    """The owner: ``check_period`` elements of its own work, then exactly one poll.

    *grant_before_switch* is the **dirty run**.  With it ``False`` the body does what the protocol
    requires — stop touching the region, *then* grant — and with it ``True`` it grants while still
    reading, which is the collision the whole interface exists to prevent.  Both are here because a
    clean pass on the correct body says nothing unless the wrong body fails.
    """
    rd = 0
    playing = True
    for _ in range(int(n_chunks)):
        if playing:
            data, _t0 = yield from b.own.read_pipelined(WORD, b.own.check_period, addr=rd)
            if log is not None:
                log.append(np.asarray(data.val).reshape(-1).copy())
            rd = (rd + b.own.check_period) % b.nelem
        else:
            # The filler phase.  It costs the same time as a chunk, because what makes the grant
            # bounded is the POLL RATE, not what the owner does between polls.
            yield b.own.timeout(b.own.check_period / float(b.clk.freq))
        cmd = yield from b.own.poll_nb()
        if cmd is None:
            continue
        if int(cmd.opcode) == LOCK_ACQUIRE:
            if not grant_before_switch:
                playing = False                     # STOP TOUCHING IT, then grant.  Always.
            yield from b.own.grant(int(cmd.start_addr), int(cmd.end_addr))
        else:
            b.own.resume()
            playing = True


def requester_body(b: Bench, lo: int, hi: int, payload: np.ndarray, out: dict,
                   *, touch_outside: bool = False, skip_release: bool = False):
    """The requester: acquire, one anchored burst in, release.

    *touch_outside* is the other **dirty run** — one element past the region it was granted, which
    is the byte-versus-word failure's shape: consistent right up to the edge.
    """
    out["status"] = yield from b.req.acquire(int(lo), int(hi))
    if out["status"] != LOCK_GRANTED:
        return
    yield from b.req.write_pipelined(payload, addr=int(lo))
    if touch_outside:
        b.req.write(int(hi), 0xDEAD)                # one past the end.  Must raise.
    if not skip_release:
        yield from b.req.release()


# ---------------------------------------------------------------------------
# The wire format
# ---------------------------------------------------------------------------

def test_both_messages_are_exactly_one_beat():
    """``8 + 28 + 28 == 64``, and the owner's poll depends on it.

    The poll is a NON-BLOCKING read: half a command is not a command, so a schema that grew past one
    beat would turn "no news" into "a command torn in two" with nothing to say so.
    """
    assert lock_bitwidth() == 64
    for cls in LOCK_SCHEMA_CLASSES:
        assert int(cls.get_bitwidth()) == 64
        assert int(cls.nwords_per_inst(64)) == 1
    assert ADDR_BW == 28


def test_the_region_survives_a_round_trip_through_the_wire():
    """A region put on the wire comes back as the same two numbers.

    The whole reason the bounds are in the format at S1 — where one region is held and the echo is
    redundant — is that S2 must not have to change the format.  A field that could not carry the
    memory's own address range would force exactly that.
    """
    cmd = MemLockCmd()
    cmd.opcode, cmd.start_addr, cmd.end_addr = LOCK_ACQUIRE, 192, 256
    got = MemLockCmd().deserialize(cmd.serialize(word_bw=64), word_bw=64)
    assert (int(got.opcode), int(got.start_addr), int(got.end_addr)) == (LOCK_ACQUIRE, 192, 256)

    resp = MemLockResp()
    resp.status, resp.start_addr, resp.end_addr = LOCK_BAD_RANGE, 0, (1 << ADDR_BW) - 1
    back = MemLockResp().deserialize(resp.serialize(word_bw=64), word_bw=64)
    assert int(back.end_addr) == (1 << ADDR_BW) - 1, "an address field that wraps is not an address"


def test_half_open_regions_are_adjacent_with_no_off_by_one():
    """``[0, 4)`` and ``[4, 8)`` touch and do not overlap — the reason the bounds are half-open."""
    assert check_region(0, 4, NELEM) == LOCK_GRANTED
    assert check_region(4, 8, NELEM) == LOCK_GRANTED
    assert check_region(NELEM, NELEM, NELEM) == LOCK_GRANTED, "an empty region is start == end"
    assert check_region(NELEM - 4, NELEM, NELEM) == LOCK_GRANTED, "the last element is reachable"
    assert check_region(NELEM - 3, NELEM + 1, NELEM) == LOCK_BAD_RANGE
    assert check_region(8, 4, NELEM) == LOCK_BAD_RANGE, "start > end is not an empty region"


# ---------------------------------------------------------------------------
# The protocol, end to end
# ---------------------------------------------------------------------------

def test_a_granted_region_round_trips_and_the_owner_reads_it_back():
    """The happy path: the requester fills ``[0, 8)``, releases, and the owner plays it back.

    The values are checked, not the plumbing.  The likeliest failure here writes the right words at
    the wrong base, which a "did it run" check passes and a ramp does not.
    """
    b = Bench()
    out: dict = {}
    log: list = []
    pay = ramp(8)
    b.run(owner_body(b, n_chunks=12, log=log),
          requester_body(b, 0, 8, pay, out))
    assert out["status"] == LOCK_GRANTED
    assert np.array_equal(b.mem.storage[:8], pay)
    b.lk.assert_handover_happened(1)
    # The owner has to have read the new words back at least once, or the run proved only that a
    # write landed in a numpy array nobody looked at.
    played = np.concatenate(log)
    assert np.isin(pay, played).any(), (
        f"the owner never read a word of the loaded waveform back: {played}")


def test_a_region_at_the_TOP_of_the_memory_round_trips():
    """**A region that wraps.**  ``base + offset`` is the byte-versus-word bug's shape.

    That bug had every BRAM design mis-addressed and ``bram_toy`` stayed green through it, because
    the scaling was *consistent*: a design round-trips perfectly right up to the point its memory
    wraps.  So the gate that matters is not ``[0, n)`` — it is the region whose last element is the
    memory's last element, where a base that is scaled wrongly runs off the end instead of aliasing
    quietly.
    """
    b = Bench()
    lo, hi = NELEM - 8, NELEM
    out: dict = {}
    pay = ramp(8, base=7000)
    b.run(owner_body(b, n_chunks=12), requester_body(b, lo, hi, pay, out))
    assert out["status"] == LOCK_GRANTED
    assert np.array_equal(b.mem.storage[lo:hi], pay)
    assert int(b.mem.storage[lo - 1]) == 0, "the word BELOW the region must not have moved"


def test_a_bad_range_is_refused_and_grants_nothing():
    """``end > nelem`` answers :data:`LOCK_BAD_RANGE`, and the requester then holds **nothing**.

    Refused rather than clamped, for :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN`'s reason: a
    clamped region is a different region, silently.  And a refused requester that still believed it
    held something would be the collision arrived at from the polite direction.
    """
    b = Bench()
    out: dict = {}
    b.run(owner_body(b, n_chunks=8), requester_body(b, NELEM - 4, NELEM + 4, ramp(8), out))
    assert out["status"] == LOCK_BAD_RANGE
    assert b.req.region is None
    assert b.own.region is None, "a refused region must not be yielded"
    assert not b.mem.storage.any(), "a refused acquire must not have written anything"


def test_the_run_ending_mid_handover_is_a_failure_not_a_pass():
    """A requester that never releases leaves the owner permanently off part of its own memory.

    Invisible from a word count and from the sample data, which is why it is a named assertion
    rather than something a byte comparison would have caught.
    """
    b = Bench()
    out: dict = {}
    b.run(owner_body(b, n_chunks=8), requester_body(b, 0, 8, ramp(8), out, skip_release=True))
    assert out["status"] == LOCK_GRANTED
    with pytest.raises(AssertionError, match="mid-handover"):
        b.lk.assert_handover_happened(1)


def test_a_grant_arrives_within_the_declared_check_period():
    """The bound that makes "the owner is busy" distinguishable from a deadlock.

    ``check_period`` elements of the owner's own work, plus the memory's read latency for the chunk
    already in flight, plus the beat the answer takes.  The gate supplies the seconds because the
    interface will not invent the owner's element rate — see ``assert_grant_bounded``.
    """
    b = Bench()
    out: dict = {}
    b.run(owner_body(b, n_chunks=12), requester_body(b, 0, 8, ramp(8), out))
    budget = 2 * (b.own.check_period + b.mem.read_latency + 2) / float(b.clk.freq)
    b.lk.assert_grant_bounded(budget)
    assert b.req.grant_waits and max(b.req.grant_waits) > 0, (
        "a grant that cost no simulated time means the owner was never actually working")


# ---------------------------------------------------------------------------
# The region assertion — every refusal provoked, from both sides
# ---------------------------------------------------------------------------

def test_the_requester_touching_ONE_element_outside_its_region_raises():
    """**The gate this checkpoint exists for.**

    One element past the end of what was granted.  ``BramIFMaster.write`` would take it without a
    word — the address is in range, the access kind is right — and at RTL it is whatever the BRAM's
    read-during-write mode happens to be, with ``bram_t2p.v``'s ``$error`` thrown away by XSI.  The
    lock is the only place this is a *failure* rather than a plausible number.
    """
    b = Bench()
    out: dict = {}
    with pytest.raises(RuntimeError, match=r"touched element 8 while it holds \[0, 8\)"):
        b.run(owner_body(b, n_chunks=12),
              requester_body(b, 0, 8, ramp(8), out, touch_outside=True))


def test_the_owner_that_grants_BEFORE_it_stops_reading_raises():
    """The one ordering everything turns on, as a dirty run.

    Granting while still in the playing state lets the requester write memory the owner is reading —
    precisely the collision.  ``grant()`` takes the region out of the owner's hands before the
    response goes on the wire, so the owner's very next read is the failure, and it names the cause.
    """
    b = Bench()
    out: dict = {}
    with pytest.raises(RuntimeError, match="has YIELDED"):
        b.run(owner_body(b, n_chunks=12, grant_before_switch=True),
              requester_body(b, 0, NELEM, ramp(NELEM), out))


def test_the_owner_may_keep_working_OUTSIDE_the_region_it_yielded():
    """The complement is the point: yielding ``[0, 8)`` does not stop the owner reading ``[8, 64)``.

    Without this the interface would be an abort channel with extra fields — the region would buy
    nothing over "stop everything", and S2's whole case (a writer filling one half while a reader
    drains the other) would have no mechanism behind it.
    """
    b = Bench()
    b.mem.storage[:] = ramp(NELEM, base=500)
    out: dict = {}
    log: list = []

    def owner(bb: Bench):
        # Reads the TOP half only, forever; the requester takes the bottom half.
        rd = NELEM // 2
        for _ in range(10):
            data, _ = yield from bb.own.read_pipelined(WORD, bb.own.check_period, addr=rd)
            log.append(np.asarray(data.val).reshape(-1).copy())
            rd += bb.own.check_period
            if rd >= NELEM:
                rd = NELEM // 2
            cmd = yield from bb.own.handle_nb()
            if cmd is not None and int(cmd.opcode) == LOCK_ACQUIRE:
                # NO state change: it was never touching the bottom half in the first place.
                yield from bb.own.grant(int(cmd.start_addr), int(cmd.end_addr))

    b.run(owner(b), requester_body(b, 0, NELEM // 2, ramp(NELEM // 2, base=9000), out))
    assert out["status"] == LOCK_GRANTED
    assert np.array_equal(b.mem.storage[:NELEM // 2], ramp(NELEM // 2, base=9000))
    assert log, "the owner never read anything; the concurrency claim is untested"


def test_a_pipelined_extent_is_checked_before_any_of_it_lands():
    """A burst that straddles the edge is refused whole, not half-written.

    A half-written refused block is worse than a refused one: the memory then holds a waveform that
    is partly the old one, which is exactly the plausible-samples failure the guard is for.
    """
    b = Bench()
    out: dict = {}

    def over_run(bb: Bench):
        out["status"] = yield from bb.req.acquire(0, 8)
        yield from bb.req.write_pipelined(ramp(9), addr=0)      # nine words into an eight-word hold

    with pytest.raises(RuntimeError, match=r"touched element 8 while it holds \[0, 8\)"):
        b.run(owner_body(b, n_chunks=12), over_run(b))
    assert not b.mem.storage.any(), "the refused burst must not have written its first eight words"


def test_a_second_acquire_before_the_release_is_a_protocol_error():
    """One outstanding request is the S1 contract; a queue is S2's allocator.

    Refused at the call rather than queued, because a queued second request would need a bookkeeping
    scheme that does not exist yet — and inventing one silently is how a contract stops meaning
    anything.
    """
    b = Bench()

    def twice(bb: Bench):
        yield from bb.req.acquire(0, 8)
        yield from bb.req.acquire(8, 16)

    with pytest.raises(RuntimeError, match="already holds"):
        b.run(owner_body(b, n_chunks=12), twice(b))


def test_a_release_with_no_acquire_is_refused_on_both_sides():
    """Neither end may drop a claim it never made.

    The two ends disagreeing about whose turn it is has no recovery in a protocol with one message
    each way, so it is refused at the point it would be entered.
    """
    b = Bench()
    with pytest.raises(RuntimeError, match="holds nothing"):
        b.run(b.req.release())
    b2 = Bench()
    with pytest.raises(RuntimeError, match="yielded nothing"):
        b2.own.resume()


# ---------------------------------------------------------------------------
# Structure: what the interface lowers to, and where it is filed
# ---------------------------------------------------------------------------

def test_the_lock_streams_are_internal_edges_and_the_memory_wires_are_not():
    """The split that makes this interface work at all.

    ``physical_interfaces()`` is what ``derive_internal_edges`` walks, and a ``BramIF`` in it would
    make the kernel's memory ports disappear into a FIFO that does not exist.  ``rtl_interfaces()``
    is the other half, and ``add_if`` files it in the RTL registry.
    """
    b = Bench()
    assert [type(i) for i in b.lk.physical_interfaces()] == [StreamIF, StreamIF]
    assert [type(i) for i in b.lk.rtl_interfaces()] == [BramIF, BramIF]
    assert {i.bitwidth for i in b.lk.physical_interfaces()} == {64}
    assert {i.depth for i in b.lk.physical_interfaces()} == {1}


def test_add_if_files_the_memory_wires_in_the_rtl_registry():
    """One registration, two registries.

    A composite that registered the lock and forgot the two memory wires gets a dangling ``bram``
    port — refused by the wrapper emitter, but only after codegen, and by then the message names a
    port rather than the wiring call that was missing.
    """
    from waveflow.hw.hw_freerun import FreeRunMod

    b = Bench()
    holder = FreeRunMod(sim=b.sim, name="holder")
    holder.add_if(b.lk)
    assert list(holder.interfaces) == [b.lk.name]
    assert sorted(holder.rtl_ifs) == sorted([b.lk.wr_if.name, b.lk.rd_if.name])


def test_the_three_physical_endpoints_are_in_cpp_argument_order():
    """``(memory port, cmd, resp)`` on both ends.

    The order is the endpoint's contract, not a detail: a composite endpoint occupies one name in a
    ``KernelTask`` signature and three arguments in the C++, spliced in at that position — so a
    hand-written body reads them in exactly this sequence.
    """
    b = Bench()
    assert [e.name for e in b.req.physical_endpoints()] == [
        "loader_mem", "loader_cmd", "loader_resp"]
    assert [e.name for e in b.own.physical_endpoints()] == [
        "player_mem", "player_cmd", "player_resp"]
    assert b.req.mem_ep.access == "write" and b.own.mem_ep.access == "read"
    assert b.req.mem_ep.interface is b.lk.wr_if
    assert b.own.mem_ep.interface is b.lk.rd_if


def test_the_writer_is_on_port_A_and_the_reader_on_port_B():
    """``bram_t2p.v``'s ``$error`` is one-sided — *A writes while B touches the same address*.

    A writing port B would be invisible to the design's only real RTL check, so the assignment is
    structural rather than conventional: the requester's ``write`` port is wired to the memory's
    ``wr_port`` by the interface, and nothing in a design gets to choose otherwise at S1.
    """
    b = Bench()
    assert b.lk.wr_if.endpoints["slave"] is b.mem.wr_port
    assert b.lk.rd_if.endpoints["slave"] is b.mem.rd_port
    assert b.mem.port_access == ("write", "read")


@pytest.mark.parametrize("kw, match", [
    ({"element_type": word_element(32)}, "element"),
    ({"nelem": NELEM * 2}, "nelem"),
])
def test_bind_refuses_a_geometry_disagreement(kw, match):
    """Two ends that disagree about the element or the depth are refused at bind.

    The quieter of the two is the element type: two types of the same width put every address in the
    same place and hand back a correctly-shaped wrong number, with nothing downstream in a position
    to notice.
    """
    sim = Simulation()
    mem = T2pBram(sim=sim, name="mem", element_type=WORD, nelem=NELEM)
    lk = LockedT2pMemIF(sim=sim, name="lk", clk=Clock(freq=250e6), element_type=WORD,
                        nelem=NELEM, memory=mem)
    decl = {"element_type": WORD, "nelem": NELEM, "access": "write", **kw}
    bad = LockedMemMasterIF(sim=sim, name="bad", **decl)
    with pytest.raises(ValueError, match=match):
        lk.bind("master", bad)


def test_the_interface_refuses_a_memory_it_does_not_match():
    """The lock's ``nelem`` bounds every region, so it is the memory's or it is a lie."""
    sim = Simulation()
    mem = T2pBram(sim=sim, name="mem", element_type=WORD, nelem=NELEM)
    with pytest.raises(ValueError, match="nelem"):
        LockedT2pMemIF(sim=sim, name="lk", clk=Clock(freq=250e6), element_type=WORD,
                       nelem=NELEM * 2, memory=mem)


def test_the_interface_refuses_the_wrong_endpoint_on_each_side():
    """Master is the requester and slave is the owner, and the names say who drives the channel
    rather than who owns the storage — which is the inversion most likely to be wired backwards."""
    b = Bench()
    sim2 = Simulation()
    mem2 = T2pBram(sim=sim2, name="mem", element_type=WORD, nelem=NELEM)
    lk2 = LockedT2pMemIF(sim=sim2, name="lk", clk=Clock(freq=250e6), element_type=WORD,
                         nelem=NELEM, memory=mem2)
    own2 = LockedMemSlaveIF(sim=sim2, name="own", element_type=WORD, nelem=NELEM)
    with pytest.raises(TypeError, match="the requester"):
        lk2.bind("master", own2)
    with pytest.raises(KeyError):
        b.lk.bind("owner", b.own)


def test_an_owner_that_never_polls_is_refused_at_construction():
    """``check_period`` is the contract that makes a grant bounded; zero is no contract at all."""
    sim = Simulation()
    with pytest.raises(ValueError, match="check_period"):
        LockedMemSlaveIF(sim=sim, name="own", element_type=WORD, nelem=NELEM, check_period=0)


def test_the_lock_is_untimed_only_where_nothing_moves():
    """``resume`` is a plain method and ``grant`` is a generator, and the difference is real.

    Taking a region back is a register bit changing on the beat the message arrives; answering one
    puts a word on a wire.  The missing ``yield`` is the statement that no simulated time passes —
    the same statement ``BramIFMaster.read`` makes.
    """
    import inspect

    assert not inspect.isgeneratorfunction(LockedMemSlaveIF.resume)
    assert inspect.isgeneratorfunction(LockedMemSlaveIF.grant)
    assert inspect.isgeneratorfunction(LockedMemSlaveIF.poll_nb)
    assert inspect.isgeneratorfunction(LockedMemMasterIF.acquire)
    assert not inspect.isgeneratorfunction(LockedMemMasterIF.write)


def test_release_carries_the_region_it_gives_back():
    """The message says which region, even though S1 has only one.

    Same reason the grant echoes: a waveform is readable without cross-referencing, and S2 needs the
    correlation without a format change.
    """
    b = Bench()
    seen: list = []

    def owner(bb: Bench):
        for _ in range(12):
            yield bb.own.timeout(bb.own.check_period / float(bb.clk.freq))
            cmd = yield from bb.own.poll_nb()
            if cmd is None:
                continue
            seen.append((int(cmd.opcode), int(cmd.start_addr), int(cmd.end_addr)))
            if int(cmd.opcode) == LOCK_ACQUIRE:
                yield from bb.own.grant(int(cmd.start_addr), int(cmd.end_addr))
            else:
                bb.own.resume()

    out: dict = {}
    b.run(owner(b), requester_body(b, 16, 24, ramp(8), out))
    assert seen == [(LOCK_ACQUIRE, 16, 24), (LOCK_RELEASE, 16, 24)]
