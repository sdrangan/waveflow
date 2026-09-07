"""One shot transmitter, both play modes — ``plans/rf_shot_unify.md`` Stage A.

The merge of a finite transmitter (``ShotPhase`` + ``rdy`` + ``done``, five tasks) and an infinite
one (the lock, three tasks), both retired by ``plans/rf_shot_unify.md`` Stage B.  What is on trial
is that one
design does what both did, so the four gates are named for the four things the pair could do between
them:

1. **finite play** — ``SHOT_LOAD``, *n* passes, then quiet;
2. **infinite play** — ``SHOT_LOOP``, the waveform switched mid-play with filler in between;
3. **``SHOT_BUSY``** — a load arriving while a *finite* shot is running, refused rather than
   preempted;
4. **all five verdicts**, in one stream.

Gate 3 is the one the merge could most easily get wrong in a way nothing notices: preempting a finite
shot produces a perfectly good *shorter* signal, and every counter downstream still adds up.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.rf_shot_tx import (
    SHOT_BUSY,
    SHOT_END,
    SHOT_LOAD,
    SHOT_LOADED,
    SHOT_LOOP,
    SHOT_BAD_OPCODE,
    SHOT_SHORT,
    SHOT_STATUS_NAMES,
    ShotTxHdr,
)
from waveflow.hw.rf_relayout import to_slots
from waveflow.hw.rf_shot_tx import FILLER, RfShotTx, ShotPlayCmd
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.simulation.simulation import Simulation

#: The converter's word: four 14-in-16 samples in 64 bits.  ``justify_shift() == 2``, so the last
#: stage is a **real** conversion — a bench with ``shift == 0`` would be measuring a pair of wires,
#: and the comparison below would then be trivially true for the wrong reason.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)

WORD_BW = int(WORD.bitwidth)
SPW = int(WORD.samp_per_word)
#: Words the memory holds, which **is** the length of a shot (``plans/rf_shot_geometry.md``).
#:
#: **16, not 64**: it is what ``nword`` was before the shot became the buffer, so every played
#: length, pass count and scenario timing in this file is unchanged by the geometry change and the
#: assertions below stay comparable across it.
DEPTH = 16
BLK_WORDS = 4
NSAMP = DEPTH * SPW

#: An opcode this design does not know.  ``OPCODE_BW`` is 2 bits and the legal values are 0, 1 and 2,
#: so 3 is the only illegal one the wire can carry.
BAD_OPCODE = 3
#: One word every 250 ns, so a pass is 4 us and a run is a handful of firings.
#:
#: **It is the SINK's rate, and since ``plans/lt_transient.md`` S2 that is the only place it can
#: be.**  It used to be handed to the player as a ``dac_word_rate`` metronome; the player has no
#: such field any more, because at RTL nothing tells it a rate — ``TREADY`` does.  So the bench
#: models the converter's appetite where a converter's appetite lives, on the consumer, and the
#: player is paced by back-pressure flowing back up the channel.  The scenario timings below are
#: unchanged by the move: throughput is still one 4-word chunk per microsecond.
DAC_WORD_RATE = 4e6


def ramp(base: int, n: int = DEPTH) -> np.ndarray:
    """*n* distinguishable words.  Distinguishable matters: a zeroed memory reads as zeros, so a
    constant payload cannot tell a write that landed from one that never happened — and the whole
    claim of gate 2 is that the output *switches*."""
    return np.arange(int(base), int(base) + int(n), dtype=np.uint64)


def slots(dense: np.ndarray) -> np.ndarray:
    """What the converter sees after the re-layout — the dense words a host loaded, re-packed.

    The memory holds **dense** words (the logic-side format a host can write without knowing anything
    about justification) and the last stage converts them to the converter's slots.  So a gate that
    compared the played words against the loaded ones directly would fail on a *correct* design, and
    one that used ``shift == 0`` to make them equal would be measuring a pair of wires.  The
    expectation goes through the same single source the design does.
    """
    return np.asarray(to_slots(WORD, np.asarray(dense, dtype=np.uint64)), dtype=np.uint64).ravel()


def frame(opcode: int, tid: int, nrepeat: int, payload: np.ndarray) -> np.ndarray:
    """One ``TLAST``-delimited frame: the header, then the samples.

    Header **and** payload on one port, which is what makes the frame boundary the mechanism rather
    than a convenience: a payload word and a header word are the same 64 bits.
    """
    h = ShotTxHdr()
    h.opcode, h.tid, h.nrepeat = int(opcode), int(tid), int(nrepeat)
    return np.concatenate([np.asarray(h.serialize(word_bw=WORD_BW), dtype=np.uint64).ravel(),
                           np.asarray(payload, dtype=np.uint64).ravel()])


class Bench:
    """The design, a frame source, and a sink — no converter, and that is deliberate.

    ``examples/rf_shot_tx`` puts a real ``Rfdc`` on the end because the property *that* graph
    claims is that it keeps a DAC fed.  What is on trial here is the **merge**, and a converter would
    add a second thing that can fail while proving nothing extra about it.  What the bench does need
    from a converter is its *rate*, and :meth:`_drain` is that and nothing else: one chunk per
    :data:`DAC_WORD_RATE`-worth of words, on an absolute grid, so the player is paced by
    back-pressure exactly as it is at RTL.
    """

    def __init__(self, *, shift: int = 2) -> None:
        self.sim = Simulation()
        self.clk = Clock(name="clk", freq=250e6)
        self.dut = RfShotTx(sim=self.sim, name="dut", bitwidth=WORD_BW, samp_per_word=SPW,
                                   depth=DEPTH, shift=int(shift),
                                   blk_words=BLK_WORDS, clk=self.clk)
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
        """The converter's appetite: one chunk per chunk period, on an **absolute** grid.

        Absolute rather than ``yield timeout(period)`` in a loop, for the reason
        :meth:`~waveflow.hw.rf_sample_if.RFSampIF.run_proc` states: anything the body yields for
        would otherwise be added to the period and never given back, and the grid would slip
        cumulatively and silently.
        """
        period = BLK_WORDS / DAC_WORD_RATE
        k = 0
        while True:
            self.played.append(np.asarray((yield from self.samp_snk.get())).ravel())
            k += 1
            dly = k * period - self.samp_snk.now
            if dly > 0:
                yield self.samp_snk.timeout(dly)

    def _resp(self):
        while True:
            yield from self.resp_snk.get()

    def run(self, schedule, until: float) -> None:
        """*schedule* is ``[(t, frame), ...]`` — when the host pushes each frame.

        ``until`` is a testbench constant, not a latency: the player is a free-running source that
        never exhausts, so an unbounded ``env.run()`` would not return.
        """
        def drive():
            t = 0.0
            for when, f in schedule:
                if when > t:
                    yield self.src.timeout(when - t)
                    t = when
                yield from self.src.write(np.asarray(f, dtype=np.uint64))

        sim = self.sim
        for obj in sim._sim_objs:
            obj.pre_sim()
        for obj in sim._sim_objs:
            p = obj.run_proc()
            if p is not None:
                sim.env.process(p)
        sim.env.process(drive())
        sim.env.process(self._drain())
        sim.env.process(self._resp())
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

        Splitting on filler is how the *shape* of a run is read: a gap between two waveforms, or a
        tail after a finite shot, rather than one waveform that happens to contain some zeros.  The
        payloads are non-zero precisely so this is unambiguous.
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

    @property
    def resps(self) -> list[tuple[int, int, int]]:
        return self.dut.resps


def named(rs):
    """Verdicts as names, so a failure says what happened rather than a number."""
    return [(t, SHOT_STATUS_NAMES.get(s, s), n) for t, s, n in rs]


# ---------------------------------------------------------------------------
# Gate 1 — finite play
# ---------------------------------------------------------------------------

def test_a_finite_shot_plays_n_passes_and_then_goes_quiet():
    """**Gate 1.**  ``SHOT_LOAD`` with ``nrepeat = 3``: three passes, bit-exact, then filler forever.

    Three claims, and the third is the one the infinite predecessor could not make at all: it
    *stops*.  A
    player that never stopped would produce a longer perfectly good signal; one that stopped early, a
    shorter one.  Only the pass count and the trailing filler separate them.
    """
    a = ramp(1000)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOAD, 0, 3, a))], until=60e-6)
    dut = b.dut

    assert named(b.resps) == [(0, "SHOT_LOADED", NSAMP)], named(b.resps)
    dut.assert_handover(1)
    dut.assert_finite_completed(n_shots=1, n_plays=3)

    segs = b.segments()
    runs = [s for is_filler, s in segs if not is_filler]
    assert len(runs) == 1, (
        f"the playout has {len(runs)} non-filler run(s); a finite shot is one continuous run of "
        f"passes between the startup filler and the tail")
    assert runs[0].size == 3 * DEPTH, (
        f"the run is {runs[0].size} words, expected {3 * DEPTH} — three whole passes")
    assert np.array_equal(runs[0].reshape(3, DEPTH), np.tile(slots(a), (3, 1))), (
        "the three passes are not three copies of the loaded waveform")
    assert segs[-1][0], "the run did not end in filler — the player never went quiet"


def test_the_shot_fills_the_whole_memory_and_the_region_is_all_of_it():
    """**The shot IS the buffer** — the region is ``[0, depth)`` and the load fills every word.

    This gate used to place the region at the top of the memory and check the words either side of
    it, because ``base + offset`` was the shape of the byte-versus-word bug ``bram_toy`` stayed green
    through.  ``plans/rf_shot_geometry.md`` removed ``base``, so there is no placement to get wrong
    and nothing outside the region to check — the region is the memory.

    What is left to assert is the half that still has content: the counted load pass writes **every**
    element, so a short frame is padded rather than leaving the tail holding the previous waveform.
    The wrap that replaced the addition is gated at RTL, where it is visible on the read port.
    """
    a = ramp(1000)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOAD, 0, 1, a))], until=40e-6)
    assert b.dut.region == (0, DEPTH), (
        f"the region is {b.dut.region}; the shot is the buffer, so it can only be [0, {DEPTH}).")
    assert np.array_equal(b.dut.mem.storage[0:DEPTH], a)


# ---------------------------------------------------------------------------
# Gate 2 — infinite play
# ---------------------------------------------------------------------------

def test_an_infinite_shot_switches_waveform_mid_play_with_filler_between():
    """**Gate 2.**  ``SHOT_LOOP`` twice: waveform A, a gap, waveform B — and it never stops.

    This is the capability ``RfShotTx`` does not have: its answer to the second frame is
    ``SHOT_BUSY``.  Here the load preempts through the lock, which is the only way an infinite play
    can ever end.
    """
    a, bb = ramp(1000), ramp(5000)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOOP, 0, 1, a)),
           (20e-6, frame(SHOT_LOOP, 1, 1, bb))],
          until=40e-6)
    dut = b.dut

    assert named(b.resps) == [(0, "SHOT_LOADED", NSAMP), (1, "SHOT_LOADED", NSAMP)], named(b.resps)
    dut.assert_handover(2)
    assert np.array_equal(dut.mem.storage[0:DEPTH], bb)
    assert int(dut.play.n_done) == 0, (
        "the infinite path sent a done token; a spurious one clears a busy that a LATER finite shot "
        "set, and the next load would preempt it")
    assert dut.play.playing, "an infinite shot stopped playing"

    runs = [s for f, s in b.segments() if not f]
    assert len(runs) == 2, f"expected waveform A, a gap, then waveform B; got {len(runs)} runs"
    for want, got, which in ((slots(a), runs[0], "A"), (slots(bb), runs[1], "B")):
        n = min(int(got.size), int(want.size))
        assert np.array_equal(got[:n], want[:n]), f"waveform {which} is not what was loaded"
        whole = got.size - (got.size % want.size)
        assert np.array_equal(got[:whole].reshape(-1, want.size),
                              np.tile(want, (whole // want.size, 1))), (
            f"waveform {which} does not repeat from its own start; the read pointer is not wrapping")


# ---------------------------------------------------------------------------
# Gate 3 — SHOT_BUSY
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("second_opcode", [SHOT_LOAD, SHOT_LOOP],
                         ids=["a_second_LOAD", "a_LOOP_arriving"])
def test_a_load_arriving_while_a_finite_shot_plays_is_refused(second_opcode):
    """**Gate 3.**  A finite shot in flight refuses *any* load — and the first one plays out whole.

    The objection is not to what the arriving shot is; it is that truncating the running one would be
    **invisible**.  A preempted three-pass shot produces two perfectly good passes, and every counter
    downstream still adds up — which is why the refusal has to be a verdict the host can see, and why
    ``busy`` covers ``SHOT_LOOP`` as well.

    Both opcodes are parametrized because getting this right for one and not the other is precisely
    the merge bug this gate exists to catch.
    """
    a, bb = ramp(1000), ramp(5000)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOAD, 0, 3, a)),
           (5e-6, frame(second_opcode, 1, 1, bb))],
          until=60e-6)
    dut = b.dut

    assert named(b.resps) == [(0, "SHOT_LOADED", NSAMP), (1, "SHOT_BUSY", 0)], named(b.resps)
    # The refusal did NOT take the lock, and did NOT touch the memory.
    dut.assert_handover(1)
    assert np.array_equal(dut.mem.storage[0:DEPTH], a), (
        "the refused load wrote to the memory anyway — SHOT_BUSY must refuse before it requests")
    # ... and the first shot played out in full.
    dut.assert_finite_completed(n_shots=1, n_plays=3)
    runs = [s for f, s in b.segments() if not f]
    assert len(runs) == 1 and runs[0].size == 3 * DEPTH, (
        f"the running shot was truncated: {[int(r.size) for r in runs]} words against "
        f"{3 * DEPTH} expected. That is a perfectly good shorter signal, which is why it needs a "
        f"count and not a comparison.")


def test_a_load_arriving_while_an_INFINITE_shot_plays_is_accepted():
    """The other half of the asymmetry, stated as its own claim.

    ``busy`` must be set by a finite shot and **not** by an infinite one.  A design that set it for
    both would answer ``SHOT_BUSY`` forever to every load after the first loop — which is exactly the
    defect the infinite predecessor was written to avoid.
    """
    a, bb = ramp(1000), ramp(5000)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOOP, 0, 1, a)),
           (10e-6, frame(SHOT_LOAD, 1, 2, bb))],
          until=60e-6)
    assert named(b.resps) == [(0, "SHOT_LOADED", NSAMP), (1, "SHOT_LOADED", NSAMP)], named(b.resps)
    b.dut.assert_handover(2)
    # The finite shot that preempted the loop ran to completion and stopped.  `n_plays` is TOTAL
    # passes and the loop's depend on when the preemption landed, so what is pinned is the finite
    # half: exactly one done, and the player quiet at the end.
    b.dut.assert_finite_completed(n_shots=1)
    runs = [s for f, s in b.segments() if not f]
    assert np.array_equal(runs[-1].reshape(-1, DEPTH), np.tile(slots(bb), (runs[-1].size // DEPTH, 1)))
    assert runs[-1].size == 2 * DEPTH, (
        f"the preempting finite shot played {runs[-1].size // DEPTH} pass(es), expected 2")


def test_the_busy_flag_clears_when_the_finite_shot_finishes():
    """``done`` is what makes ``SHOT_BUSY`` transient rather than permanent.

    A player that never sent one leaves the loader busy forever: every later load is refused, and the
    design looks like it is working right up until a host tries to change waveform.  So the third
    frame — arriving *after* the shot has finished — must be accepted.
    """
    a, bb, c = ramp(1000), ramp(5000), ramp(9000)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOAD, 0, 1, a)),
           (2e-6, frame(SHOT_LOAD, 1, 1, bb)),      # mid-play -> refused
           (30e-6, frame(SHOT_LOAD, 2, 1, c))],     # after it finished -> accepted
          until=80e-6)
    assert named(b.resps) == [(0, "SHOT_LOADED", NSAMP),
                              (1, "SHOT_BUSY", 0),
                              (2, "SHOT_LOADED", NSAMP)], named(b.resps)
    b.dut.assert_finite_completed(n_shots=2, n_plays=2)
    # `busy` is cleared by a HARVEST, which happens on a firing -- so it is still set at the end of a
    # run whose last shot finished with no frame behind it.  That is correct, and it is why the proof
    # that it clears is the third frame being ACCEPTED rather than the flag being read directly.
    assert int(b.dut.play.n_done) == 2


# ---------------------------------------------------------------------------
# Gate 4 — all four verdicts
# ---------------------------------------------------------------------------

def test_all_four_verdicts_plus_the_fence_in_one_stream():
    """**Gate 4.**  Every status this design can produce, in one run, in an order that is not a race.

    ``tid`` 0 is the only load that can succeed at first; the two behind it arrive while a *finite*
    shot is playing, so they exercise the busy path — and **malformed is tested before transient**,
    which is what makes ``tid`` 2 distinguishable from ``SHOT_BUSY``.  A build that reordered the two
    tests would return ``SHOT_BUSY`` for it and this scenario would say so.

    **Four, where it was five.**  ``plans/rf_shot_geometry.md`` retired ``SHOT_ZERO_LEN``
    (``nsamp == 0``) and the length half of ``SHOT_WRONG_LEN`` (``nsamp != nword * spw``) with the
    header field both read.  The verdict survives as ``SHOT_BAD_OPCODE`` — same wire value, named for
    the fault it reports — and it is what ``tid`` 2 now provokes.
    """
    a = ramp(1000)
    empty = np.zeros(0, dtype=np.uint64)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOAD, 0, 3, a)),                     # LOADED
           (2e-6, frame(SHOT_LOAD, 1, 1, a)),                    # BUSY  (transient)
           (3e-6, frame(BAD_OPCODE, 2, 1, a)),                   # BAD_OPCODE (malformed, and it
           #                                                       beats the busy it arrived during)
           (30e-6, frame(SHOT_LOAD, 3, 1, a[:DEPTH // 2])),      # SHORT
           (50e-6, frame(SHOT_END, 4, 0, empty))],               # the fence
          until=90e-6)

    assert named(b.resps) == [
        (0, "SHOT_LOADED", NSAMP),
        (1, "SHOT_BUSY", 0),
        (2, "SHOT_BAD_OPCODE", 0),
        (3, "SHOT_SHORT", (DEPTH // 2) * SPW),
        (4, "SHOT_LOADED", 0),
    ], named(b.resps)
    assert {s for _t, s, _n in b.resps} == {SHOT_LOADED, SHOT_BUSY, SHOT_BAD_OPCODE,
                                           SHOT_SHORT}, (
        "the run did not reach all four verdicts")


def test_a_short_shot_is_loaded_and_then_never_played():
    """Half a waveform must not reach the converter — on **either** path.

    This achieves it by handing the player a repeat count of zero; the infinite predecessor could
    not,
    and says so: it plays the padded result because it has no way to go quiet.  The merged design
    does have one, so the stricter rule wins and both paths get it.
    """
    a, short = ramp(1000), ramp(5000, DEPTH // 2)
    b = Bench()
    b.run([(0.0, frame(SHOT_LOOP, 0, 1, a)),
           (20e-6, frame(SHOT_LOOP, 1, 1, short))],
          until=40e-6)
    assert named(b.resps)[1] == (1, "SHOT_SHORT", (DEPTH // 2) * SPW)
    # It landed in the memory, padded ...
    assert np.array_equal(b.dut.mem.storage[0:DEPTH // 2], short)
    assert not b.dut.mem.storage[DEPTH // 2:DEPTH].any(), "the tail was not padded with zeros"
    # ... and the run ends in filler rather than playing it.
    assert b.segments()[-1][0], "a short shot reached the converter"
    assert not b.dut.play.playing


def test_a_frame_whose_opcode_is_neither_LOAD_nor_LOOP_is_refused(monkeypatch=None):
    """An unknown opcode is refused, never reinterpreted.

    A command answered as something other than what it asked for is invisible: the samples would look
    perfect.  ``SHOT_END`` is the one other legal value and it is a fence, handled before the verdict.
    """
    b = Bench()
    b.run([(0.0, frame(BAD_OPCODE, 0, 1, ramp(1000)))], until=20e-6)
    assert named(b.resps) == [(0, "SHOT_BAD_OPCODE", 0)]
    assert b.dut.lock.n_grants == 0, "an unknown opcode took the lock"


# ---------------------------------------------------------------------------
# The play command, and the ordering everything turns on
# ---------------------------------------------------------------------------

def test_a_buffer_too_large_for_the_old_16_bit_field_builds_and_round_trips():
    """**The witness for `plans/rf_shot_wire_format.md` Part A**, retargeted but not weakened.

    Before Part A, the length field was 16 bits wide because ``IDX_BW`` said so — a constant imported
    from :mod:`waveflow.hw.rf_samp_buf`, the superseded family — and ``RfShotTx.__post_init__``
    *refused* any geometry whose shot did not fit it:

    ``if nw * spw >= (1 << IDX_BW): raise ValueError("... does not fit the 16-bit nsamp field")``

    A constant from another module bounded the design. Now the design sizes the field, so this test
    is the check becoming **unnecessary** rather than deleted on faith: it builds the geometry the
    old code refused, and shows the length survives the wire.

    **The field it watches moved**, because ``plans/rf_shot_geometry.md`` removed the header's
    ``nsamp``: the only length on the wire now is the response's ``nsamp_loaded``, and it is still
    derived, still has to grow with the geometry, and still has to round-trip.  The failure it
    guards is unchanged in kind — a width that wrapped would report a *partial* load as a full one,
    which is the one thing ``SHOT_SHORT``'s diagnosis exists to say.

    Without this the change is unfalsifiable — everything else only confirms nothing broke.
    """
    from waveflow.hw.rf_shot_tx import nsamp_bw_for, shot_tx_schemas

    big_depth = 1 << 16                       # 65536 words x 4 = 262144 samples
    nsamp = big_depth * SPW
    assert nsamp >= (1 << 16), "the point is a buffer the OLD 16-bit field could not describe"

    # 1. the width follows the geometry rather than a constant
    nb = nsamp_bw_for(big_depth, SPW)
    assert nb > 16, f"nsamp_loaded is still {nb} bits; it has to grow with the buffer"

    # 2. the design CONSTRUCTS at that geometry -- this is the line the old check refused
    dut = RfShotTx(sim=Simulation(), name="big", bitwidth=WORD_BW, samp_per_word=SPW,
                   depth=big_depth, shift=2,
                   blk_words=BLK_WORDS, clk=Clock(name="c", freq=250e6))
    assert dut.nsamp_shot == nsamp

    # 3. and the length round-trips on the wire, which is what the field is for
    hdr_cls, resp_cls = shot_tx_schemas(big_depth, SPW)
    r = resp_cls()
    r.tid, r.status, r.nsamp_loaded, r._rsvd = 7, SHOT_SHORT, nsamp, 0
    back = resp_cls().deserialize(r.serialize(word_bw=WORD_BW), word_bw=WORD_BW)
    assert int(back.nsamp_loaded) == nsamp, (
        f"nsamp_loaded came back {int(back.nsamp_loaded)} instead of {nsamp} — the field wrapped, "
        f"which reports a partial load as a correct one. That is the failure the old check existed "
        f"to prevent, arrived at from the response's side.")
    assert (int(back.tid), int(back.status)) == (7, SHOT_SHORT)

    # 4. and both are still ONE 64-bit word, which is the decision the plan made explicitly
    assert hdr_cls.get_bitwidth() == 64 and hdr_cls.nwords_per_inst(WORD_BW) == 1
    assert resp_cls.get_bitwidth() == 64 and resp_cls.nwords_per_inst(WORD_BW) == 1


def test_both_messages_are_one_word_and_the_padding_is_declared():
    """One 64-bit word at every geometry, with the slack **named** rather than incidental.

    ``plans/rf_shot_wire_format.md`` Part A: the point of deriving the widths is that a field cannot
    silently overflow, *not* that the message gets smaller — a stable wire size is what a DMA wants.
    So whatever ``nsamp_loaded`` does not use is a declared ``_rsvd`` field, and the total is
    invariant — and since the header carries no length at all, its padding is simply fixed.
    """
    from waveflow.hw.rf_shot_tx import MSG_BW, shot_tx_schemas

    for depth, spw in ((16, 4), (64, 4), (1 << 16, 4), (1 << 20, 2)):
        hdr, resp = shot_tx_schemas(depth, spw)
        for cls in (hdr, resp):
            assert cls.get_bitwidth() == MSG_BW, (
                f"{cls.__name__} is {cls.get_bitwidth()} bits at depth={depth}, spw={spw}; the "
                f"wire size is supposed to be {MSG_BW} whatever the geometry.")
            assert "_rsvd" in cls.elements, f"{cls.__name__}'s padding is not declared"
            assert cls.nwords_per_inst(64) == 1


def test_the_play_command_is_one_beat_and_carries_the_hosts_own_opcode():
    """``8 + 16`` in one 64-bit beat, and the opcode is the host's rather than a parallel vocabulary.

    The player needs exactly two things the lock has no opinion about — how many passes, and whether
    a ``done`` is owed — and both are already in the header the host sent.
    """
    assert int(ShotPlayCmd.nwords_per_inst(WORD_BW)) == 1
    c = ShotPlayCmd()
    c.opcode, c.nrepeat = SHOT_LOOP, 3
    got = ShotPlayCmd().deserialize(c.serialize(word_bw=WORD_BW), word_bw=WORD_BW)
    assert (int(got.opcode), int(got.nrepeat)) == (SHOT_LOOP, 3)


class _GrantsWhilePlaying(RfShotTx.player_cls):
    """The shipped player with the ``playing = False`` before the grant removed — **one line**."""

    def run_iter(self):
        yield from self._chunk()
        cmd = yield from self.lock.handle_nb()
        if cmd is None:
            return
        if int(cmd.opcode) == 1:                     # LOCK_RELEASE
            play = yield from self.rep_in.get_schema(ShotPlayCmd)
            self.rd = 0
            self.loop = int(play.opcode) == SHOT_LOOP
            self.nrep_left = int(play.nrepeat)
            self.playing = self.nrep_left > 0
            self.n_resumed += 1
            if not self.playing and not self.loop:
                yield from self._send_done()
            return
        # THE DEFECT: the region goes out and this task carries on reading it.
        yield from self.lock.grant(int(cmd.start_addr), int(cmd.end_addr))


def test_a_player_that_grants_and_keeps_reading_raises():
    """**The ordering the whole protocol turns on**, as a paired dirty run.

    At RTL this is what ``bram_t2p.v``'s ``$error`` catches — and XSI discards ``$error``, so nothing
    would say a word.  The pysim guard is the only place it is a *failure* rather than a plausible
    sample, and it fails on the very next chunk because
    :meth:`~waveflow.hw.locked_mem.LockedMemSlaveIF.grant` takes the region out of the owner's hands
    before the answer goes on the wire.
    """
    class Dirty(RfShotTx):
        player_cls = _GrantsWhilePlaying

    b = Bench.__new__(Bench)
    b.sim = Simulation()
    b.clk = Clock(name="clk", freq=250e6)
    b.dut = Dirty(sim=b.sim, name="dut", bitwidth=WORD_BW, samp_per_word=SPW, depth=DEPTH,
                  shift=2, blk_words=BLK_WORDS, clk=b.clk)
    b.src = StreamIFMaster(sim=b.sim, name="src", bitwidth=WORD_BW, has_tlast=True)
    b.resp_snk = StreamIFSlave(sim=b.sim, name="resp_snk", bitwidth=WORD_BW, has_tlast=True)
    b.samp_snk = StreamIFSlave(sim=b.sim, name="samp_snk", bitwidth=WORD_BW, has_tlast=True)
    for nm, m, s in (("cmd", b.src, b.dut.s_in), ("resp", b.dut.resp_out, b.resp_snk),
                     ("samp", b.dut.samp_out, b.samp_snk)):
        ifc = StreamIF(name=f"tb_{nm}", sim=b.sim, clk=b.clk, bitwidth=WORD_BW, depth=2)
        ifc.bind("master", m)
        ifc.bind("slave", s)
    b.played = []

    a, bb = ramp(1000), ramp(5000)
    with pytest.raises(RuntimeError, match="has YIELDED"):
        b.run([(0.0, frame(SHOT_LOOP, 0, 1, a)),
               (20e-6, frame(SHOT_LOOP, 1, 1, bb))],
              until=40e-6)


# ---------------------------------------------------------------------------
# Structure — what the merge removed
# ---------------------------------------------------------------------------

def test_the_design_is_three_tasks_and_three_channels_plus_the_lock():
    """What the lock bought, counted against ``RfShotTx``.

    That design wires **seven** internal channels and **two** ``BramIF``\\ s by hand and instantiates
    **five** tasks.  This wires three — ``rep``, ``done``, ``samp`` — plus one ``add_if(lock)``, and
    instantiates three.  ``rep`` and ``done`` survive because the lock has no opinion about them.
    """
    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.build.elaborate import elaborate

    comp = elaborate(RfShotTx,
                     {"bitwidth": WORD_BW, "samp_per_word": SPW, "depth": DEPTH,
                      "shift": 2, "blk_words": BLK_WORDS},
                     name="rf_shot_tx")
    spec = composite_top_spec(comp, width=WORD_BW)
    assert len(spec.tasks) == 3
    assert [(p.name, p.kind) for p in spec.ports] == [
        ("s_in", "axis_in"), ("resp_out", "axis_out"),
        ("buf_w", "bram"), ("buf_r", "bram"), ("samp_out", "axis_out")]
    assert sorted(c.name for c in spec.channels) == [
        "done", "lock_if_cmd", "lock_if_resp", "rep", "samp"]
    assert not comp.is_identity, (
        "shift=0 makes the last stage the identity, so the run would be measuring a pair of wires")


def test_the_loader_writes_port_A_and_the_player_reads_port_B():
    """``bram_t2p.v``'s ``$error`` is one-sided, so the writer must be on port A.

    The lock routes by declared direction rather than by role, which is what lets TX and RX share it;
    on TX the requester writes, so it lands on A.
    """
    b = Bench()
    assert b.dut.load.lock.access == "write" and b.dut.play.lock.access == "read"
    assert b.dut.load.lock.mem_ep.interface is b.dut.lock.wr_if
    assert b.dut.play.lock.mem_ep.interface is b.dut.lock.rd_if
    assert b.dut.lock.wr_if.endpoints["slave"] is b.dut.mem.wr_port


def test_the_player_polls_once_per_block():
    """``check_period`` is the block, which is what makes the loader's wait for a grant a stated
    number rather than a hope."""
    b = Bench()
    assert b.dut.play.lock.check_period == BLK_WORDS
