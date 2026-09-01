"""Infinite play: change the waveform without stopping the converter.

``plans/t2p_lock_chan.md`` S1, checkpoint 3 — the lock's **first real consumer**, and the gate the
whole plan is aimed at: load a shot, play it indefinitely, load a second shot *mid-play*, and watch
the output switch waveform with filler in between.

The plan's argument for building an interface rather than a one-off abort channel is that an
un-consumed interface is unverified — ``CreditStreamIF`` is fully written, documented with three
named rules, and instantiated by no design anywhere, so nothing has ever tried to satisfy its
contract.  This file is the trying.

**Both dirty runs are here by name**, because a clean pass proves nothing on its own: a player that
grants before it stops reading, and a loader that touches outside its region.  Their pysim failures
are what makes the clean runs above them mean something.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.locked_mem import LOCK_ACQUIRE
from waveflow.hw.rf_relayout import to_slots
from waveflow.hw.rf_shot_loop import FILLER, RfShotTxLoop, ShotLoopPlay
from waveflow.hw.rf_shot_tx import (
    SHOT_END,
    SHOT_LOAD,
    SHOT_LOADED,
    SHOT_LOOP,
    SHOT_SHORT,
    SHOT_WRONG_LEN,
    SHOT_ZERO_LEN,
    ShotTxHdr,
)
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.simulation.simulation import Simulation

#: The 4x2's word: four 14-in-16 samples in 64 bits.  ``justify_shift() == 2``, so the last stage is
#: a real conversion rather than a pair of wires — the condition a gate has to hold or it is
#: measuring nothing.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)

WORD_BW = int(WORD.bitwidth)
SPW = 4
DEPTH = 64
NWORD = 16
BLK_WORDS = 4
#: Words per second the "DAC" takes.  One word every 250 ns, so a whole pass is 4 us and a handover
#: is a handful of blocks — slow enough that a gap is visible on the output, fast enough that a run
#: is a few hundred firings.
DAC_WORD_RATE = 4e6


def dense(base: int, n: int = NWORD) -> np.ndarray:
    """*n* densely-packed words whose samples are distinguishable.

    Distinguishable matters twice over: a zeroed memory reads as zeros, so a constant payload cannot
    tell a write that landed from one that never happened — and the whole claim here is that the
    output *switches*, which a second waveform sharing values with the first could not show.
    """
    from waveflow.hw.arrayutils import write_array
    from waveflow.hw.rf_relayout import dense_elem_type

    stored = np.arange(base, base + n * SPW, dtype=np.int64)
    out = write_array(stored, elem_type=dense_elem_type(WORD), word_bw=WORD_BW)
    return np.asarray(out, dtype=np.uint64).ravel()


def frame(opcode: int, tid: int, nsamp: int, payload: np.ndarray) -> np.ndarray:
    """One ``TLAST``-delimited frame: the header, then the samples.

    Header **and** payload on one port, which is what makes the frame boundary the mechanism rather
    than a convenience: a payload word and a header word are the same 64 bits, so without ``TLAST``
    there is no in-band way to say *that was the end*.
    """
    h = ShotTxHdr()
    h.opcode, h.tid, h.nsamp, h.nrepeat = int(opcode), int(tid), int(nsamp), 1
    return np.concatenate([np.asarray(h.serialize(word_bw=WORD_BW), dtype=np.uint64).ravel(),
                           np.asarray(payload, dtype=np.uint64).ravel()])


class Bench:
    """The design, a frame source, and a sink — no converter, and that is deliberate.

    ``examples/rf_shot_play`` puts a real ``Rfdc`` on the end because the property *that design*
    claims is that it keeps a DAC fed.  What is on trial here is the **handover**, and a converter
    would add a second thing that can fail while proving nothing extra about it: the player's
    metronome is handed over directly, so the pacing is the same either way.
    """

    def __init__(self, *, base: int = 0, player_cls=ShotLoopPlay) -> None:
        self.sim = Simulation()
        self.clk = Clock(name="axis_clk", freq=250e6)
        self.dut = RfShotTxLoop(sim=self.sim, name="dut", bitwidth=WORD_BW, samp_per_word=SPW,
                                depth=DEPTH, nword=NWORD, base=int(base),
                                shift=int(WORD.justify_shift()), blk_words=BLK_WORDS,
                                dac_word_rate=DAC_WORD_RATE, clk=self.clk)
        if player_cls is not ShotLoopPlay:
            self.dut.play.__class__ = player_cls        # the dirty run, wired into the real graph
        self.src = StreamIFMaster(sim=self.sim, name="src", bitwidth=WORD_BW, has_tlast=True)
        self.resp_snk = StreamIFSlave(sim=self.sim, name="resp_snk", bitwidth=WORD_BW,
                                      has_tlast=True)
        self.samp_snk = StreamIFSlave(sim=self.sim, name="samp_snk", bitwidth=WORD_BW,
                                      has_tlast=True)
        for nm, m, s in (("cmd", self.src, self.dut.s_in),
                         ("resp", self.dut.resp_out, self.resp_snk),
                         ("samp", self.dut.samp_out, self.samp_snk)):
            ifc = StreamIF(name=f"tb_{nm}", sim=self.sim, clk=self.clk, bitwidth=WORD_BW, depth=2)
            ifc.bind("master", m)
            ifc.bind("slave", s)
        self.played: list[np.ndarray] = []

    def _drain(self):
        while True:
            self.played.append(np.asarray((yield from self.samp_snk.get())).ravel())

    def run(self, schedule, until: float) -> None:
        """*schedule* is ``[(t, frame), ...]`` — when the host pushes each frame.

        ``until`` is a testbench constant, not a latency: the player is a free-running source that
        never exhausts (that is what infinite play *means*), so an unbounded ``env.run()`` would
        never return.
        """
        def drive():
            t = 0.0
            for when, f in schedule:
                if when > t:
                    yield self.src.timeout(when - t)
                    t = when
                yield from self.src.write(np.asarray(f, dtype=np.uint64))

        self.run_procs(drive(), until=until)

    def run_procs(self, *procs, until: float) -> None:
        """The three-phase lifecycle plus *procs* — the graph, driven by whatever is handed in.

        Separate from :meth:`run` so a dirty run reads as "the design, plus this one illegal access"
        rather than as a second testbench with its own wiring to get wrong.
        """
        sim = self.sim
        for obj in sim._sim_objs:
            obj.pre_sim()
        for obj in sim._sim_objs:
            p = obj.run_proc()
            if p is not None:
                sim.env.process(p)
        for proc in procs:
            sim.env.process(proc)
        sim.env.process(self._drain())
        try:
            sim.env.run(until=float(until))
        except Exception:
            for obj in sim._sim_objs:
                obj.error_cleanup()
            raise
        for obj in sim._sim_objs:
            obj.post_sim()

    # -- reading what came out ----------------------------------------------------------------

    @property
    def out(self) -> np.ndarray:
        """Every word the design handed the converter, in order."""
        return np.concatenate(self.played) if self.played else np.zeros(0, dtype=np.uint64)

    def segments(self) -> list[tuple[bool, np.ndarray]]:
        """The output split into ``(is_filler, words)`` runs.

        Filler is a run of :data:`~waveflow.hw.rf_shot_loop.FILLER`, and splitting on it is how the
        *shape* of a handover is read: a gap between two waveforms, rather than one waveform that
        happens to contain some zeros.
        """
        out = self.out
        segs: list[tuple[bool, np.ndarray]] = []
        if out.size == 0:
            return segs
        mark = out == FILLER
        start = 0
        for i in range(1, out.size + 1):
            if i == out.size or mark[i] != mark[start]:
                segs.append((bool(mark[start]), out[start:i]))
                start = i
        return segs


def two_shot_run(base: int = 0, player_cls=ShotLoopPlay) -> tuple[Bench, np.ndarray, np.ndarray]:
    """Load waveform A, let it play, load waveform B **mid-play**, let that play.

    The whole user story in one function: nothing stops, nothing is refused, and the second load
    arrives while the first is coming out of the memory.  Under
    :class:`~waveflow.hw.rf_shot_tx.RfShotTx` this second frame is
    :data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY`.
    """
    a, b = dense(1000), dense(5000)
    bench = Bench(base=base, player_cls=player_cls)
    nsamp = NWORD * SPW
    bench.run([(0.0, frame(SHOT_LOOP, 0, nsamp, a)),
               (20e-6, frame(SHOT_LOOP, 1, nsamp, b))],
              until=40e-6)
    return bench, a, b


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", [0, DEPTH - NWORD],
                         ids=["region_at_zero", "region_at_the_top"])
def test_a_second_shot_arrives_mid_play_and_the_waveform_switches(base):
    """**The gate this whole plan exists for.**

    Load, play indefinitely, load again mid-play, and the output switches — with filler in between,
    because one region means a handover is a *gap* rather than a crossfade.  Both loads are accepted:
    the ``SHOT_BUSY`` that made this impossible has no way to be produced here.

    ``region_at_the_top`` is the region that **wraps**: its last element is the memory's last.
    ``base + offset`` is the shape of the byte-versus-word bug ``bram_toy`` stayed green through —
    consistently mis-scaled addressing round-trips perfectly right up to the top of the address
    space — so a gate that only ever loaded at zero would be measuring nothing.
    """
    bench, a, b = two_shot_run(base)
    dut = bench.dut
    nsamp = NWORD * SPW

    assert dut.resps == [(0, SHOT_LOADED, nsamp), (1, SHOT_LOADED, nsamp)], (
        f"both loads must be accepted under infinite play; got {dut.resps}")
    assert np.array_equal(dut.mem.storage[base:base + NWORD], b), (
        "the second waveform is not in the memory at the region the design declared")
    if base:
        assert int(dut.mem.storage[base - 1]) == 0, "the word BELOW the region moved; the base is off"

    # Three claims the counters make and the samples cannot: the lock changed hands exactly twice,
    # every grant was released, the player really went to filler, and it came back.
    dut.assert_handover(2, max_grant_seconds=4 * BLK_WORDS / DAC_WORD_RATE)

    want_a, want_b = to_slots(WORD, a), to_slots(WORD, b)
    runs = [w for is_filler, w in bench.segments() if not is_filler]
    assert len(runs) == 2, (
        f"the output has {len(runs)} non-filler run(s); a handover is exactly one gap between two "
        f"waveforms, so two is what a load-play-load-play run looks like")
    for want, got, which in ((want_a, runs[0], "A"), (want_b, runs[1], "B")):
        assert got.size >= want.size, (
            f"waveform {which} produced {got.size} words, less than one whole pass of {want.size}")
        assert np.array_equal(got[:want.size], want), (
            f"waveform {which} did not come out as loaded — the first pass differs")
        # A play that stopped part way carries the right samples as far as it got, so the ALIGNMENT
        # is what says the loop is a loop: every pass starts at word 0 of the region.
        whole = got.size - (got.size % want.size)
        assert np.array_equal(got[:whole].reshape(-1, want.size), np.tile(want, (whole // want.size, 1))), (
            f"waveform {which} does not repeat from its own start; the read pointer is not wrapping "
            f"to the region's beginning")


def test_the_first_load_is_preceded_by_filler_and_not_by_an_unwritten_memory():
    """Before anything is loaded the player emits **filler**, not the contents of a zeroed array.

    The distinction is the whole of the plausible-samples failure this repo keeps meeting: a memory
    that was never written reads as zeros in pysim and as X (or stale data) at RTL, and either way it
    looks exactly like a quiet signal.  Starting in filler makes "nothing is loaded" a state the
    design is *in* rather than a value it happens to produce.
    """
    bench, a, _ = two_shot_run()
    segs = bench.segments()
    assert segs and segs[0][0], "the run did not start in filler"
    assert bench.dut.play.n_words > 0, "the player never read the memory at all"
    # And the player did not touch the memory before the first grant was released: every word it
    # read is accounted for by the chunks it played, which the region guard enforced during the run.
    assert bench.dut.play.n_words == (bench.dut.play.n_chunks - bench.dut.play.n_filler) * BLK_WORDS


def test_the_converter_edge_never_sees_a_short_burst():
    """Every burst is ``blk_words`` long, filler and payload alike.

    pysim's quantum on that edge is a *block*: ``Rfdc``'s DAC takes one whole burst per event and
    refuses a partial one.  A handover that emitted a short block would deadlock a real converter
    rather than gap it, and the counters above would all still look right.
    """
    bench, _, _ = two_shot_run()
    sizes = {int(b.size) for b in bench.played}
    assert sizes == {BLK_WORDS}, f"bursts of {sorted(sizes)} words, expected only {BLK_WORDS}"


def test_the_handover_gap_is_bounded_by_the_load_and_not_by_the_poll():
    """The gap is as long as the transfer, and no longer.

    What the *grant* costs is one poll period, which ``assert_grant_bounded`` already checks.  What
    the *gap* costs is the whole transaction — acquire, write, release — and it is the number a user
    of this design actually feels.  Bounding it here is what makes "a handover is a gap" a
    measurement rather than a description.
    """
    bench, _, _ = two_shot_run()
    gaps = [w.size for is_filler, w in bench.segments() if is_filler]
    assert len(gaps) == 2, f"expected the startup gap and one handover gap, got {len(gaps)}"
    # The transfer is NWORD words at the fabric's rate plus the poll that granted it; the player is
    # paced by the DAC, so the gap is measured in ITS blocks.  Generous, because what is being
    # excluded is a gap that scales with the run rather than with the load.
    assert gaps[1] <= 4 * BLK_WORDS, (
        f"the handover gap was {gaps[1]} words ({gaps[1] // BLK_WORDS} blocks). A gap longer than "
        f"the transfer means the player is not being told to resume promptly — or is not polling.")


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("opcode, nsamp, want", [
    (SHOT_LOOP, 0, SHOT_ZERO_LEN),
    (SHOT_LOOP, NWORD * SPW + SPW, SHOT_WRONG_LEN),
    (SHOT_LOAD, NWORD * SPW, SHOT_WRONG_LEN),
])
def test_a_refused_command_answers_and_touches_nothing(opcode, nsamp, want):
    """Three refusals, and the third is the interesting one.

    :data:`~waveflow.hw.rf_shot_tx.SHOT_LOAD` asks for ``nrepeat`` plays and then quiet, which this
    design cannot provide — so it is refused rather than silently reinterpreted as a loop.  A command
    answered as something other than what it asked for is precisely the failure a verdict exists to
    prevent, and it would be invisible: the samples would look perfect.
    """
    bench = Bench()
    bench.run([(0.0, frame(opcode, 7, nsamp, dense(1000)))], until=20e-6)
    assert bench.dut.resps == [(7, want, 0)]
    assert bench.dut.load.n_stored == 0, "a refused command wrote to the memory"
    assert not bench.dut.mem.storage.any(), "a refused command left samples behind"
    assert bench.dut.lock.n_grants == 0, "a refused command took the lock"


def test_a_fence_is_answered_and_proves_the_load_ahead_of_it_finished():
    """``SHOT_END`` changes nothing and its response says everything before it has been processed.

    An ``hls::task`` has no loop to break, so what ``END`` is worth is what its *response* proves.  A
    testbench that ended by timing out instead could not tell a finished run from a deadlocked one.
    """
    bench = Bench()
    nsamp = NWORD * SPW
    a = dense(1000)
    bench.run([(0.0, frame(SHOT_LOOP, 0, nsamp, a)),
               (10e-6, frame(SHOT_END, 1, 0, np.zeros(0, dtype=np.uint64)))],
              until=20e-6)
    assert bench.dut.resps == [(0, SHOT_LOADED, nsamp), (1, SHOT_LOADED, 0)]
    assert np.array_equal(bench.dut.mem.storage[0:NWORD], a)


def test_a_short_transfer_is_reported_and_clobbers_what_was_playing():
    """The honest limit of one region, asserted rather than described.

    ``RfShotTx`` can refuse a short shot into a quiet memory; this design cannot, because there is
    nowhere else to put an arriving waveform.  So the transfer lands padded, the design plays the
    padded result, and :data:`~waveflow.hw.rf_shot_tx.SHOT_SHORT` going back to the host is the whole
    warning there is.  **This is the first thing S2's second region fixes**, and a test that pretended
    otherwise would hide it.
    """
    bench = Bench()
    nsamp = NWORD * SPW
    a, short = dense(1000), dense(2000, NWORD // 2)
    bench.run([(0.0, frame(SHOT_LOOP, 0, nsamp, a)),
               (20e-6, frame(SHOT_LOOP, 1, nsamp, short))],
              until=40e-6)
    assert bench.dut.resps == [(0, SHOT_LOADED, nsamp),
                               (1, SHOT_SHORT, (NWORD // 2) * SPW)]
    assert np.array_equal(bench.dut.mem.storage[:NWORD // 2], short)
    assert not bench.dut.mem.storage[NWORD // 2:NWORD].any(), "the tail was not padded with zeros"


# ---------------------------------------------------------------------------
# The dirty runs — a clean pass above means nothing without these
# ---------------------------------------------------------------------------

class _GrantsTooEarly(ShotLoopPlay):
    """A player that grants and **keeps reading** — the collision, in one missing line.

    ``run_iter`` is :meth:`ShotLoopPlay.run_iter` verbatim except that the ACQUIRE branch never
    leaves the playing state, so the next chunk reads a region the loader now owns.  That is what
    *"granting while still in PLAY_MEM"* means once a body is written as one chunk per firing: the
    hazard is not the instruction order inside the branch, it is that the state change never happens.

    Subclassed rather than monkey-patched so this is the **real** graph with one line missing: the
    lock, the memory and the loader are the shipped ones, so what fails is the ordering and nothing
    else.
    """

    def run_iter(self):
        yield from self._chunk_and_pace()
        cmd = yield from self.lock.handle_nb()
        if cmd is None:
            return
        if int(cmd.opcode) == LOCK_ACQUIRE:
            # THE DEFECT: the region goes out and this task carries on reading it.
            yield from self.lock.grant(int(cmd.start_addr), int(cmd.end_addr))
        else:
            self.rd = 0
            self.playing = True
            self.n_resumed += 1


def test_a_player_that_grants_and_keeps_reading_raises():
    """The paired dirty run for the ordering the whole protocol turns on.

    At RTL this is what ``bram_t2p.v``'s ``$error`` catches — and XSI throws ``$error`` away, so
    nothing would say a word.  The lock is the only place it is a *failure* rather than a plausible
    sample: :meth:`~waveflow.hw.locked_mem.LockedMemSlaveIF.grant` takes the region out of the
    owner's hands before the answer goes on the wire, so the very next chunk raises and names the
    cause.
    """
    with pytest.raises(RuntimeError, match="has YIELDED"):
        two_shot_run(player_cls=_GrantsTooEarly)


def test_a_loader_writing_past_its_region_raises():
    """The other dirty run: one element past the end of the grant.

    ``BramIFMaster.write`` would take it without a word — the address is in range and the access kind
    is right — which is exactly why the guard has to live on the lock and not on the memory.  The
    region here is the one at the top of the memory, so the illegal address is the first one past the
    end of the address space the design ever legitimately touches.
    """
    bench = Bench(base=DEPTH - NWORD)
    lo, hi = bench.dut.region

    def one_word_too_far():
        yield from bench.dut.load.lock.acquire(lo, hi)
        bench.dut.load.lock.write(hi, 0xDEAD)

    with pytest.raises(RuntimeError,
                       match=rf"touched element {hi} while it holds \[{lo}, {hi}\)"):
        bench.run_procs(one_word_too_far(), until=20e-6)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_the_design_is_three_tasks_and_one_lock():
    """What the lock bought, counted.

    ``RfShotTx`` moves the same samples with five tasks, seven internal channels, two hand-wired
    ``BramIF``\\ s and a shared phase object.  Here the ``rdy`` token, the ``rep`` channel, the
    ``done`` token and the whole Stage A pair are gone, and what replaced them is two lock streams
    and one ordering rule.
    """
    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.build.elaborate import elaborate

    comp = elaborate(RfShotTxLoop, {"bitwidth": WORD_BW, "samp_per_word": SPW, "depth": DEPTH,
                                    "nword": NWORD, "base": 0,
                                    "shift": int(WORD.justify_shift()), "blk_words": BLK_WORDS},
                     name="rf_shot_tx_loop")
    spec = composite_top_spec(comp, width=WORD_BW)
    assert len(spec.tasks) == 3
    assert [(p.name, p.kind) for p in spec.ports] == [
        ("s_in", "axis_in"), ("resp_out", "axis_out"),
        ("buf_w", "bram"), ("buf_r", "bram"), ("samp_out", "axis_out")]
    assert sorted(c.name for c in spec.channels) == ["lock_if_cmd", "lock_if_resp", "samp"]
    assert not comp.is_identity, (
        "shift=0 makes the last stage the identity, so the run would be measuring a pair of wires")


def test_the_loader_refuses_a_region_that_does_not_fit_at_construction():
    """A region checked once, where the reason can still be said.

    On the wire it would come back ``LOCK_BAD_RANGE`` on every single load, which is a true answer to
    the wrong question: the geometry is build-time structure, not something a host got wrong.
    """
    with pytest.raises(ValueError, match="does not fit"):
        RfShotTxLoop(sim=Simulation(), name="bad", bitwidth=WORD_BW, samp_per_word=SPW,
                     depth=DEPTH, nword=NWORD, base=DEPTH - NWORD + 1,
                     shift=int(WORD.justify_shift()), blk_words=BLK_WORDS)
