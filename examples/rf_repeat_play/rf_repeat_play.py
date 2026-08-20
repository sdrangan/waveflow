"""rf_repeat_play.py — Stage 1 of ``plans/rf_samp_new.md``: the repeat player, TX path only.

A host loads a waveform of ``NSAMP`` samples and replays it forever on a fixed period, through
:class:`~waveflow.hw.rf_tx_stream.RfTxStream` into a real converter::

    RepeatPlayHost --TxCmd--> loader                         player --> Rfdc.tx_stream
                   --samples-> loader --AckedStreamIF--> player
                   <--TxResp-- loader <---- TxStatus ---- player
                                                  Rfdc.tx_rf --RFSampIF--> RfDataSink

**TX only, and that is the point of doing it first.**  No RX means nothing in the other half can
confuse a diagnosis: every underrun, every late window and every counter in this example belongs to
the transmitter.

How the host learns "now"
-------------------------

``start_now`` on the **first** play, and nothing else.  The player assigns the slots; the status
reports where the window's *last* sample landed; ``TxResp.samp_start`` comes back as
``status.slot - (nsamp - 1)``.  Every repeat after that is scheduled **absolutely** at
``samp_start + k * PERIOD`` — never relative to the previous one, because a schedule computed from
the last thing that happened cannot recover from a gap, and recovering from a gap is what the third
assertion measures.

**There is no zero-length probe command**, deliberately.  A zero-length frame has no last sample, so
``request_status`` is never set, so no status returns and the pending slot never pops — a few of
those and the loader refuses everything with ``TX_NO_SLOT``, for reasons that look nothing like the
cause.  :class:`~waveflow.hw.rf_tx_stream.TxLoader` refuses ``nsamp == 0`` outright
(:data:`~waveflow.hw.rf_tx_stream.TX_ZERO_LEN`), which is the plan's open question closed by the
first of the two answers it offers.

The geometry, and why each number is what it is
------------------------------------------------

``PERIOD == NSAMP == BLK_SAMP == blksize``.  Contiguous repeats, one window per block period, which
is what makes :meth:`~waveflow.hw.rf_sample_if.RFSampIF.assert_clean` the right gate: a converter fed
through a pipeline **must** underrun for its first blocks and must never underrun afterwards.  A
duty-cycled schedule would underrun by design and make ``assert_clean`` inapplicable — and
``underrun == 0`` is not a substitute, because it passes designs that recover by accident.

The waveform is genuinely **fixed** — the same ``NSAMP`` samples every time, which is what a repeat
player is.  That leaves one thing the played data alone cannot see: a slip of a *whole* period, since
a tiled repeat of one waveform is self-similar at exactly that shift.  It is caught instead by the
pair of checks around it — an exact ``samp_start`` per ``tid``, and ``assert_clean``, because a slipped
period is a block in which nothing was due and therefore one extra underrun.  Sub-period drift
misaligns the tiling and shows up directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.hw_module import HwModule  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave  # noqa: E402
from waveflow.hw.rf_samp_buf import IDX_BW, pack_samples  # noqa: E402
from waveflow.hw.rf_sample_if import RFSampIF  # noqa: E402
from waveflow.hw.rf_tx_stream import (  # noqa: E402
    SAMP_BW,
    TX_MISALIGNED,
    TX_NO_SLOT,
    TX_STREAM_SCHEMA_CLASSES,
    TX_TOO_LATE,
    TX_TRANSMITTED,
    TX_ZERO_LEN,
    RfTxStream,
    TxCmd,
    TxResp,
)
from waveflow.simulation.rf_tb import RfDataSink  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402

__all__ = [
    "BLK_SAMP", "MAX_IN_FLIGHT", "NREPEAT", "NSAMP", "N_BLK", "PERIOD", "SAMP_BASE", "SAMP_BW",
    "START_LEAD", "FIRST_PLAY_BLK", "first_play_offset",
    "SAMP_RATE", "TX_MISALIGNED", "TX_NO_SLOT", "TX_STREAM_SCHEMA_CLASSES", "TX_TOO_LATE",
    "TX_TRANSMITTED", "TX_ZERO_LEN", "RepeatPlayHost", "RfRepeatPlayTB", "RfTxStream", "TxCmd",
    "TxResp", "played_samples", "responses", "run_pysim", "waveform",
]

# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

#: Samples in one play.  Equal to the converter's ``blksize`` so a window is exactly one block: the
#: player's pysim quantum is a block, so a window that straddled two would be judged by its first and
#: the arithmetic would stop being checkable by inspection.
NSAMP = 64

#: Slots between the starts of consecutive plays.  Equal to :data:`NSAMP` — contiguous playout, which
#: is what makes ``assert_clean`` applicable.  See the module docstring.
PERIOD = 64

#: Samples per pysim block on the converter edge.  One number, three roles, and they must agree.
BLK_SAMP = 64

#: Plays in the gate scenario.  **Enough that drift shows rather than only a jump**: the absolute-grid
#: defect this guards against adds the body's own elapsed time to every period, so its error grows
#: linearly with the firing index.  Forty plays against a 64-slot period means an error of a single
#: fabric cycle per firing (4 ns against a 15.6 ns slot) would have accumulated past a whole slot by
#: play 4 and past half a period by play 40 — visible as a misaligned tiling long before the end.
NREPEAT = 40

#: Sample rate.  15.625 ns per slot, 1 us per block, against a 250 MHz fabric — so the loader has
#: about 250 fabric cycles per block to read a command, drain 64 payload words and write 64 tagged
#: beats.  It needs roughly 140, which is the headroom this design is supposed to have and the
#: existing BRAM loader does not.
SAMP_RATE = 64e6

#: Windows the loader may have unresolved at once — the ``pending`` FIFO depth **and** the ack
#: channel's depth.  Four block periods of lookahead: enough that the host is never the bottleneck,
#: small enough that :data:`TX_NO_SLOT` is reachable by a host that ignores the admission condition.
MAX_IN_FLIGHT = 4

#: Periods of lead the host gives its **first absolutely-scheduled** play, and the only host-side
#: constant in this example.
#:
#: It exists because a ``TxResp`` is *deferred*: the verdict for the ``start_now`` window is not sent
#: until that window has finished playing, so by the time the host knows where "now" was, "now" is a
#: period in the past.  Measured here: the window plays in block 1, its response reaches the host at
#: the start of block 2, and the command for the next slot needs about 0.6 of a block period to
#: cross the loader — so ``base + 1*PERIOD`` is already gone and ``base + 2*PERIOD`` is a coin toss.
#:
#: Four, not three, and it is **derived rather than padded**: the host may have
#: :data:`MAX_IN_FLIGHT` windows outstanding, so in steady state it is already issuing exactly that
#: many periods ahead.  Starting the train any closer would mean the first few plays ran at a
#: tighter lead than every play after them.
#:
#: **It is not a tuning knob, because the design checks it.**  If the lead is too small the player
#: says so — ``BEFORE`` -> ``MISSED`` -> :data:`TX_TOO_LATE` on that ``tid`` — which is exactly the
#: mechanism ``plans/rf_samp_new.md`` refuses to duplicate with a loader-side pre-check.
START_LEAD = 4

#: Sample values: ``SAMP_BASE + i``.  A ramp rather than a constant, so a window played at the wrong
#: phase is visible in the data rather than only in a counter.
SAMP_BASE = 1000

#: Converter block the ``start_now`` window lands in, counting the grid from 0.  **Measured, then
#: pinned** — it is what the loader's own latency comes to, and a change in it is a finding needing
#: an explanation rather than a constant to re-tune.  Block 0 is gone before the first command has
#: crossed the loader, so it underruns; block 1 is the first the player can fill.
FIRST_PLAY_BLK = 1

#: Block periods the converter's metronome runs.  Exactly enough to cover every play and **no tail**:
#: play ``NREPEAT-1`` lands in block ``FIRST_PLAY_BLK + NREPEAT - 1``, so the grid is that many
#: blocks plus one.
#:
#: The absence of a tail is deliberate.  Trailing blocks with nothing scheduled are underruns like
#: any other, and they would put :attr:`~waveflow.hw.rf_sample_if.RFSampIF.last_underrun_idx` past
#: the startup transient — turning "the run ended" into what reads as a steady-state fault.  The
#: metronome stops where the schedule does; ``run_until`` gives the last verdict two further block
#: periods to reach the host, which costs no grid.
N_BLK = FIRST_PLAY_BLK + NREPEAT


def waveform(nsamp: int = NSAMP, base: int = SAMP_BASE) -> np.ndarray:
    """The one waveform, replayed every period.  ``base + i`` at position *i* within the play."""
    return ((np.arange(int(nsamp), dtype=np.int64) + int(base)) % (1 << SAMP_BW)).astype(np.uint64)


# ---------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------


@dataclass
class RepeatPlayHost(HwModule):
    """Loads the waveform once, then schedules it forever — and **reads its own responses**.

    A file-driven :class:`~waveflow.simulation.stream_tb.StreamDriver` cannot play this part: the
    schedule depends on ``TxResp.samp_start`` for ``tid`` 0, which does not exist until the design has
    run.  That is not a wart, it is the mechanism — *the only way to learn where "now" is, is to ask
    the thing that knows*.  See ``run_rtl`` in the PR body for what it costs the RTL flow.

    **It honours the admission condition rather than testing it.**  At most :attr:`max_in_flight`
    windows are outstanding, so a refusal in this host would be a defect.  ``TX_NO_SLOT`` is driven
    off zero deliberately, by :attr:`overrun_in_flight`, which is the only way a counter becomes
    evidence.
    """

    nsamp: int = NSAMP
    period: int = PERIOD
    nrepeat: int = NREPEAT
    max_in_flight: int = MAX_IN_FLIGHT
    start_lead: int = START_LEAD
    bitwidth: int = SAMP_BW
    samp_per_word: int = 1

    #: Leading windows sent with ``start_now``.  **One is the design under test**: the rule is
    #: ``start_now`` on the first play and nothing else, and the price is a hole in the playout
    #: between that window and the first absolutely-scheduled one — the host cannot schedule what it
    #: cannot yet locate.
    #:
    #: Raising it to :data:`START_LEAD` primes that hole with further ``now`` windows, which the plan
    #: says land on consecutive slots for free, and the playout becomes contiguous from the first
    #: played block.  That difference is measured rather than argued — see the test named for it —
    #: because it is the difference between ``assert_clean`` applying and not.
    prime_now: int = 1

    #: **Fault injection.**  Ignore :attr:`max_in_flight` and keep sending, so the loader's
    #: ``pending`` FIFO fills and :data:`~waveflow.hw.rf_tx_stream.TX_NO_SLOT` fires.
    overrun_in_flight: bool = False
    #: **Fault injection.**  Schedule this ``tid`` at a slot already in the past, so the *player*
    #: reports ``MISSED`` and the loader turns it into ``TX_TOO_LATE``.  ``None`` disables.
    late_tid: int | None = None
    #: Slots into the past the late window is aimed.  One period is enough — the player judges a
    #: window by whether its slot has gone out, and there is no tolerance band by design.
    late_by: int = 2 * PERIOD
    #: **Fault injection.**  Send nothing for this many plays starting at :attr:`starve_from`, then
    #: resume on the ORIGINAL grid.  This is the assertion the example exists for.
    starve_from: int | None = None
    starve_plays: int = 4
    #: **Fault injection.**  Send one ``nsamp == 0`` command before the schedule starts.
    probe_zero_len: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.cmd_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_cmd_out", bitwidth=w,
                                      has_tlast=True)
        self.samp_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_samp_out", bitwidth=w,
                                       has_tlast=True)
        self.resp_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_resp_in", bitwidth=w,
                                     has_tlast=True)
        for ep in (self.cmd_out, self.samp_out, self.resp_in):
            self.add_endpoint(ep)
        #: ``(tid, status, samp_start)`` for every response, in arrival order.  The gate reads this.
        self.resps: list[tuple[int, int, int]] = []
        #: Where the first ``start_now`` window actually went out — the origin of every later slot.
        self.base: int | None = None
        self.wave = waveform(int(self.nsamp))

    # -- primitives -------------------------------------------------------------------------------

    def send(self, tid: int, samp_start: int, nsamp: int, now: bool) -> ProcessGen[None]:
        """One command, and its payload immediately behind it on the sample port."""
        c = TxCmd()
        c.tid, c.samp_start = int(tid), int(samp_start) % (1 << IDX_BW)
        c.start_now, c.nsamp = int(bool(now)), int(nsamp)
        yield from self.cmd_out.write(c)
        if int(nsamp):
            yield from self.samp_out.write(
                pack_samples(self.wave[:int(nsamp)], int(self.bitwidth), int(self.samp_per_word)))

    def take_resp(self) -> ProcessGen[tuple[int, int, int]]:
        r = yield from self.resp_in.get(TxResp)
        out = (int(r.tid), int(r.status), int(r.samp_start))
        self.resps.append(out)
        return out

    def poll_resps(self) -> ProcessGen[None]:
        """Drain whatever has arrived, without waiting for anything that has not."""
        while True:
            got = yield from self.resp_in.get_nb(TxResp)
            if got is None:
                return
            self.resps.append((int(got.tid), int(got.status), int(got.samp_start)))

    @property
    def outstanding(self) -> int:
        """Commands sent and not yet answered — what :attr:`max_in_flight` bounds."""
        return self._sent - len(self.resps)

    # -- the schedule -----------------------------------------------------------------------------

    def run_proc(self) -> ProcessGen[None]:
        self._sent = 0

        if self.probe_zero_len:
            # Kept as fault injection ONLY.  The plan warns what a zero-length probe does to a design
            # that admits one; here the loader refuses it, and this is how that refusal is witnessed.
            yield from self.send(tid=900, samp_start=0, nsamp=0, now=False)
            self._sent += 1
            yield from self.take_resp()

        # --- the priming windows: start_now, and the only way to learn "now" --------------------
        nprime = max(1, int(self.prime_now))
        for j in range(nprime):
            yield from self.send(tid=j, samp_start=0, nsamp=int(self.nsamp), now=True)
            self._sent += 1

        tid, status, start = yield from self.take_resp()
        if tid != 0 or status != TX_TRANSMITTED:
            raise RuntimeError(
                f"{self.name}: the first start_now window came back tid={tid} status={status}. A "
                f"start_now window CANNOT be late — it plays when it plays — so this is a design "
                f"fault, not a scheduling one.")
        self.base = int(start)

        # --- every repeat after that is ABSOLUTE, on the original grid --------------------------
        first_k = max(nprime, int(self.start_lead))
        for k in range(first_k, int(self.nrepeat)):
            if self.starve_from is not None and (
                    self.starve_from <= k < self.starve_from + int(self.starve_plays)):
                # Send NOTHING.  The player has nothing due, the converter underruns, and the
                # counters say so.  The SCHEDULE IS NOT TOUCHED: k still means base + k*period, so a
                # design that re-based on the gap is caught by where the playout resumes.
                yield from self.poll_resps()
                continue

            if not self.overrun_in_flight:
                while self.outstanding >= int(self.max_in_flight):
                    yield from self.take_resp()          # CHECK, then send

            if self.late_tid is not None and k == int(self.late_tid):
                slot = self.base + k * int(self.period) - int(self.late_by)
            else:
                slot = self.base + k * int(self.period)
            yield from self.send(tid=k, samp_start=slot, nsamp=int(self.nsamp), now=False)
            self._sent += 1

        while len(self.resps) < self._sent:
            yield from self.take_resp()


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


@dataclass
class RfRepeatPlayTB(FreeRunMod):
    """Host + :class:`~waveflow.hw.rf_tx_stream.RfTxStream` + a real converter + an RF sink.

    **The converter is really here.**  The one thing this design exists to satisfy is that a DAC
    cannot be told to wait, and only a metronome can hold it to that.  The tile is DAC-only
    (``n_rx=0``): a playout design has no receiver, and wiring a fake ADC in would add a metronome
    nothing drains.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset()

    nsamp: int = NSAMP
    period: int = PERIOD
    nrepeat: int = NREPEAT
    blk_samp: int = BLK_SAMP
    n_blk: int = N_BLK
    samp_rate: float = SAMP_RATE
    axis_freq: float = RFSOC4X2_CLK_HZ
    nbits: int = SAMP_BW
    samp_per_word: int = 1
    max_in_flight: int = MAX_IN_FLIGHT
    start_lead: int = START_LEAD
    prime_now: int = 1
    # Fault-injection passthroughs — see RepeatPlayHost.
    overrun_in_flight: bool = False
    late_tid: int | None = None
    starve_from: int | None = None
    starve_plays: int = 4
    probe_zero_len: bool = False
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        from examples.rf_loopback.rfdc import Rfdc

        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))
        self.slot_period = 1.0 / float(self.samp_rate)

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=0, n_tx=1,
                         nbits=int(self.nbits), samp_per_word=int(self.samp_per_word))
        w = self.rfdc.axis_bitwidth
        self.dut = RfTxStream(name=f"{self.name}_dut", sim=self.sim, bitwidth=w,
                              samp_per_word=int(self.samp_per_word),
                              blk_samp=int(self.blk_samp), max_in_flight=int(self.max_in_flight),
                              slot_period=self.slot_period, clk=self.axis_clk)
        self.host = RepeatPlayHost(
            name=f"{self.name}_host", sim=self.sim, nsamp=int(self.nsamp),
            period=int(self.period), nrepeat=int(self.nrepeat),
            max_in_flight=int(self.max_in_flight), start_lead=int(self.start_lead),
            prime_now=int(self.prime_now), bitwidth=w, samp_per_word=int(self.samp_per_word),
            overrun_in_flight=bool(self.overrun_in_flight), late_tid=self.late_tid,
            starve_from=self.starve_from, starve_plays=int(self.starve_plays),
            probe_zero_len=bool(self.probe_zero_len))
        self.sink = RfDataSink(name=f"{self.name}_sink", sim=self.sim)
        for c in (self.dut, self.rfdc, self.host, self.sink):
            self.add_comp(c)

        # --- the RF domain: one interface, one metronome (there is no ADC) ---------------------
        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blk_samp), n_blk=int(self.n_blk))
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.sink.rf_ep)
        self.add_if(self.dac_if)
        # ONE SOURCE OF TRUTH PER BACKEND: the player reports the underrun the EDGE counted, because
        # the edge is the object that owns the sample grid here.  See TxPlayer.edge_underrun.
        self.dut.player.tx_edge = self.dac_if

        # --- the PL domain ----------------------------------------------------------------------
        for nm, master, slave in (
                ("cmd", self.host.cmd_out, self.dut.cmd_in),
                ("samp", self.host.samp_out, self.dut.samp_in),
                ("resp", self.dut.resp_out, self.host.resp_in)):
            ifc = StreamIF(name=f"{self.name}_{nm}_axis", sim=self.sim, clk=self.axis_clk,
                           bitwidth=w, depth=4 * int(self.blk_samp))
            ifc.bind("master", master)
            ifc.bind("slave", slave)
            self.add_if(ifc)
            setattr(self, f"{nm}_axis", ifc)

        # The converter's AXIS input.  Deep enough for a block plus slack: the player hands a whole
        # block over at once and the Rfdc consumes one per event, so a shallower queue would make
        # the handover itself the pacing rather than the metronome.
        self.dac_axis = StreamIF(name=f"{self.name}_dac_axis", sim=self.sim, clk=self.axis_clk,
                                 bitwidth=w, depth=4 * int(self.blk_samp))
        self.dac_axis.bind("master", self.dut.samp_out)
        self.dac_axis.bind("slave", self.rfdc.tx_stream)
        self.add_if(self.dac_axis)

    @property
    def blk_period(self) -> float:
        """Seconds per converter block — the metronome's own period."""
        return int(self.blk_samp) / float(self.samp_rate)

    @property
    def run_until(self) -> float:
        """Simulated horizon.  ``n_blk`` block periods plus a two-block tail.

        A testbench constant, not a latency: the metronome already decides how long the converter
        runs, and this only gives the last verdict somewhere to land before the lights go out.
        """
        return (int(self.n_blk) + 2) * self.blk_period


# ---------------------------------------------------------------------------
# Running it, and reading what came out
# ---------------------------------------------------------------------------


def run_pysim(**kw) -> RfRepeatPlayTB:
    """Build the graph, run it to the metronome's horizon, return the testbench.

    **Bounded explicitly, and the bound is a testbench constant rather than a latency.**  The player
    is free-running: when nothing is due it still visits its slots, so it is an event source that
    never exhausts, and ``env.run()`` with no bound would never return.  That is not a modelling
    error — it is what a DAC does — so the run is bounded the way the converter is, by the
    metronome's :attr:`~waveflow.hw.rf_sample_if.RFSampIF.n_blk`, with a small margin so the last
    window's status has somewhere to land.

    The lifecycle is spelled out rather than delegated to
    :meth:`~waveflow.simulation.simulation.Simulation.run_sim`, which takes no bound.
    """
    tb = RfRepeatPlayTB(name="tb", sim=Simulation(), **kw)
    sim = tb.sim
    for obj in sim._sim_objs:
        obj.pre_sim()
    for obj in sim._sim_objs:
        proc = obj.run_proc()
        if proc is not None:
            sim.env.process(proc)
    try:
        sim.env.run(until=tb.run_until)
    except Exception:
        for obj in sim._sim_objs:
            obj.error_cleanup()
        raise
    for obj in sim._sim_objs:
        obj.post_sim()
    return tb


def played_samples(tb: RfRepeatPlayTB) -> np.ndarray:
    """What the DAC actually played, block by block, as unsigned 16-bit words.

    Read off the **RF sink** — the far side of the converter — so the comparison covers packing,
    playout and the converter's own unpack rather than only the design's output port.  Block ``j``
    covers edge slots ``[j*blk_samp, (j+1)*blk_samp)``, underruns included: the edge zero-fills a
    block it had nothing for, so a gap is present in the array rather than absent from it, and the
    slot grid stays readable straight off the index.
    """
    if not tb.sink.blocks:
        return np.zeros(0, dtype=np.uint64)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in tb.sink.blocks])
    ints = np.rint(flat * float(1 << (SAMP_BW - 1))).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


def responses(tb: RfRepeatPlayTB) -> list[tuple[int, int, int]]:
    """``(tid, status, samp_start)`` in arrival order, as the host saw them."""
    return list(tb.host.resps)


def first_play_offset(played: np.ndarray, wave: np.ndarray | None = None) -> int:
    """Index in *played* where the waveform first appears — the origin of the played grid.

    Neither backend may assume played sample *i* is slot *i*: the edge's grid and the player's start
    at different instants, and the offset between them is a property of the startup transient.  What
    is fixed is that it is a **constant**, so it is measured once here and every later position is
    absolute against it.
    """
    w = waveform() if wave is None else wave
    n = min(16, w.size)
    for i in range(max(0, played.size - n + 1)):
        if np.array_equal(played[i:i + n], w[:n]):
            return i
    return -1
