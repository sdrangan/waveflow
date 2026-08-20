# Re-design of the RF sample interface

**Status: DESIGN, not started.** 2026-08-19. Replaces the BRAM + progress-channel structure of
`waveflow/hw/rf_samp_buf.py` / `rf_samp_buf_tx.py` **for the streaming and scheduled-capture cases**.
See *What this does not do* before assuming it replaces triggered capture with pre-history.

## Why — three defects, and a measurement for each

**1. The polling wait costs 2× throughput on both halves.** `RfSampBufCapture` and
`RfSampBufLoader` each wrap a data-dependent `while` spin around a progress channel *inside* their
per-word loop. Vitis cannot pipeline an outer loop whose body contains an unbounded inner one
(`HLS 200-878`, `HLS 200-960`), so both sit at **2 cycles/word** while the converter-facing bodies
reach 1. The loop ceiling is the `max` over stages, so pattern B is stuck at 500 MSa/s on a part
whose port capacity is 1 GSa/s. Hoisting the wait was tried (PR #166): csynth reached II=1 and the
**RTL played 0xFFFF for 9984 samples** while every counter said success. Reverted, undiagnosed.

**2. The wait exists only because a BRAM has no handshake.** That is why it was chosen — it cannot
refuse a write, so the ingress satisfies never-stall structurally. The price is that the reader has
no back-pressure to learn from, so the position travels out-of-band, stale, needing `MARGIN` to
bound the staleness. Everything above is downstream of one choice: **a memory instead of a channel.**

**3. `stream_of_blocks` collapses under any interruption in supply** — the risk called out in the
previous draft of this file, now measured. `scratchpad/chain/`, three tasks
(`cmd_gen → capture → SOB(2) → samp_proc`), four variants differing *only* in commands-in-flight:

| variant       | command flow                           | result                                                                       |
| ------------- | -------------------------------------- | ---------------------------------------------------------------------------- |
| `chain_c2`  | unconditional, one per returned credit | **works — 69 cyc / 64-sample block (~1.08 cyc/sample), data correct** |
| `chain_c1`  | prime 1, then unconditional            | wedges,**0 blocks out**                                                |
| `chain_seq` | issue, block for credit, repeat        | wedges,**0 blocks out**                                                |
| `chain_fin` | prime 2, then *conditional*           | delivers the 2 primed blocks, then wedges                                    |

Three of four wedge. The survivor is the one where the writer runs unconditionally ahead — which is
exactly how `examples/interleaver` uses SOB, and why the interleaver is unaffected.

**The sequential consumer is the fatal case**, and the previous draft predicted it: *"if consumer
sends a command for new data before dropping the read_lock, system will backpressure and whole flow
will collapse."* It is worse than back-pressure — it produces **nothing at all**.

**Not diagnosed.** Four variants in one sitting could share one bug of mine. What is established is
that the structure is fragile under interrupted supply, and a triggered capture is inherently
bursty. Diagnosing SOB is worth doing on its own account; it is not on this plan's path.

---

## Reverse channels: credit and ack

Both halves of this design add a **reverse stream** alongside the forward one. They are *not* two
flavours of the same mechanism, and conflating them is the mistake the first draft of this file made
— it called the receiver's channel an "ack" when the receiver has no acks at all.

| | **credit** | **ack** |
|---|---|---|
| answers | *"May I send? Is there room?"* | *"What became of what I sent?"* |
| arrives | **before** the send | **after** the send |
| who could possibly know | the **channel** | only the **consumer** |
| used by | the receiver's capture (`CreditStreamIF`) | the transmitter's loader (`AckedStreamIF`) |

> **Credit answers a question about the channel. An ack answers a question about the consumer's
> semantics.**

The consequence that makes the split real: **a FIFO already implements credit — `TREADY` *is*
credit.** Back-pressure is credit delivered implicitly, one unit at a time, at the moment of use. An
explicit credit channel is nothing more than **back-pressure moved earlier and in bulk**, which is
why you only ever need one when "at the moment of use" is too late.

No FIFO can implement an ack, because what is being reported is not a property of the FIFO. On the
TX side a dropped sample is not a buffer-full drop at all — it is a **missed deadline**. The channel
delivered it perfectly; it simply arrived after its slot. No amount of flow control detects that,
which is exactly why the reverse path has to carry it.

### When each is needed

- **Credit** — when the sender commits to a **multi-item transaction it cannot abandon partway**. A
  sender writing one item at a time needs nothing but `write_nb`: a refusal costs it that item and
  nothing else. This is why the RX ingress uses a plain stream and only `ADMIT` uses credit.
- **Ack** — when success is **not determined by the transport**: timeliness, admission, any
  application-level acceptance the consumer decides. The forward path cannot report it because the
  forward path does not know it.

Nothing forbids an edge needing both — a sender that must reserve a whole block *and* learn whether
it played on time would carry two reverse channels answering two different questions. Neither of ours
does today, which is why they are described as two interfaces below; if a third case wants both, they
should become optional reverse endpoints on one `StreamIF` rather than a third class.

### Why this shape at all — the failure mode it replaces

| consumer style | with `stream_of_blocks` | with a reverse channel |
|---|---|---|
| sequential (issue → wait → process → issue) | **wedges, 0 output** | correct, serialised |
| pipelined (issue ahead, then process) | fast | correct, overlapped |

SOB makes the natural way to write a consumer **fatal**. Either reverse channel makes it merely
*slow*, leaving overlap as an optimisation the consumer opts into. That is the difference worth
paying for, and it is why both halves of this plan are built on streams rather than blocks.

---

### Rules shared by both channels

These four hold for the credit channel and the ack channel alike. They are the reason either is safe
to build on.

**1. Reverse values are cumulative, never incremental.** A credit ack carries *total words consumed
so far*; a status carries *totals played and underrun*. A lost increment destroys state permanently —
the producer wedges against a FIFO that looks full and is not — whereas a cumulative total is
idempotent, because the next one carries the whole truth. This is the convention the existing
progress channels already use: *"only the newest position counts."*

**2. Both directions are non-blocking.** Because a lost reverse value is harmless (rule 1), the
reverse path uses `write_nb` too. Neither end can block the other, so the reverse path cannot become
the new back-pressure route — which would defeat the entire point.

**3. The reader polls a BOUNDED number, never drain-to-empty.** `poll<N>()` with **N a compile-time
constant** unrolls into N `read_nb` calls and pipelines; `while (got) ...` is a data-dependent trip
count, which is the exact construct that costs the current design its II
(`HLS 200-878`, `HLS 200-960`). One `read_nb` is measured at II=1 (`plans/witness/task_loop/`).

**4. A saturated reverse channel is not stale-but-safe — it is permanently wrong.** With
`hls::stream` + `write_nb`, a full FIFO **drops the newest write while the reader pops the oldest**.
So "the newest supersedes" holds only while the reader outpaces the writer. Saturate it and it
inverts: the reader receives ancient values forever and every fresh one is discarded.

That is not a tuning problem, it is a correctness one, and it drives a rate rule on each channel:

| channel | write rate | why it is safe |
|---|---|---|
| RX credit | one per consumer `get` (≈ per command) | capture polls 2–3 times per command — reader outpaces writer |
| TX ack | one per **requested** item (≈ per command) | statuses are solicited, never broadcast; FIFO sized to `MAX_IN_FLIGHT` |

RX is safe today by a **rate argument, not a structural guarantee**. If a future consumer ever acks
per word, that channel needs the same solicited treatment the TX side already has.

### The counters wrap, and that is fine — but only in modular arithmetic

Every reverse-channel counter is free-running and **will** overflow. It does not matter, because no
absolute value is ever used — only differences, and those are bounded:

```
outstanding = written - acked        # always >= 0 (you cannot consume what was not written)
                                     # always <= depth
```

A modular subtraction at the counter's width is **exact** whenever the true difference is below
`2^BITS`. Bounded by `depth`, that is a margin of thousands at 16 bits and millions at 32. Note this
is a *weaker* constraint than the sample index's half-wrap contract: `time_compare` needs values
within `2^(WR_BITS-1)` because it is a signed three-way compare, whereas here the sign is known.

**The hazard is the Python model, not the hardware.** `ap_uint<N>` wraps on its own; Python integers
do not. A pysim twin computing `self.written - self.acked` on unbounded ints never exercises the wrap,
so the two backends agree everywhere except at the boundary — the exact shape of every fidelity
defect this arc has found. **Mask explicitly in Python** (`& MASK`) and put a test *at* the wrap, not
near it.

---

### `CreditStreamIF` — used by the receiver

A forward stream plus a reverse stream carrying **cumulative words consumed**. The producer tracks
the room it *knows* it has, so it can offer a write guaranteed not to stall.

The inversion is the point: the repo's existing progress channels tell the *consumer* where the
producer is; this tells the *producer* where the consumer is — and the producer is the one carrying
the never-stall obligation, so that is the direction the information should flow.

```python
@dataclass
class CreditStreamIF:
    fwd_if: StreamIF      # forward: data
    crd_if: StreamIF      # reverse: CUMULATIVE words consumed


class CreditStreamSlaveIF(...):
    def get(self, ...):
        data = yield from self.fwd_ep.get(...)
        self.consumed += nwords_of(data)                 # monotone total
        yield from self.crd_ep.offer(self.consumed)      # non-blocking; dropping is safe


class CreditStreamMasterIF(...):
    def __post_init__(self):
        self.written = 0
        self.acked   = 0
        self.depth   = self.fwd_ep.interface.queue_size

    def poll_credit(self, n=1):
        """Up to *n* values.  BOUNDED — n is a compile-time constant in the C++ twin."""
        for _ in range(n):                               # unrolls; never data-dependent
            got = yield from self.crd_ep.get_nb()
            if got is None:
                break
            self.acked = int(got) & MASK                 # cumulative: the newest wins

    @property
    def avail(self):
        """Room for DATA.  RESP_WORDS stays reserved so a verdict always fits."""
        return self.depth - RESP_WORDS - ((self.written - self.acked) & MASK)

    def write_nb(self, words):
        n = nwords_of(words)
        if n > self.avail:
            return False                                 # caller counts it; nothing stalls
        yield from self.fwd_ep.write(words)
        self.written = (self.written + n) & MASK
        return True

    def write_resp_nb(self, resp):
        """A verdict.  Draws on the RESERVED headroom, so it cannot be refused for room."""
        if self.depth - ((self.written - self.acked) & MASK) < RESP_WORDS:
            return False                                 # should be unreachable; count it if not
        yield from self.fwd_ep.write(resp)
        self.written = (self.written + RESP_WORDS) & MASK
        return True
```

**`RESP_WORDS` is permanently reserved headroom** — enough for one response. It means a verdict can
never be refused for lack of room, which matters because a *dropped* verdict is indistinguishable
from a hang at the consumer. Data competes for `depth - RESP_WORDS`; the verdict always fits.

### `AckedStreamIF` — used by the transmitter

A forward stream plus a reverse stream carrying **per-item outcomes**. Items are marked; the consumer
reports the fate of each marked item, and the producer learns something the transport could not have
told it.

```python
@dataclass
class AckedStreamIF:
    fwd_if: StreamIF      # forward: items, each carrying a `request_status` bit
    ack_if: StreamIF      # reverse: one outcome per MARKED item
```

#### The endpoint API

**Master** — frames in, resolved tokens out. The pending FIFO lives here, not in the app, because
every user would otherwise hand-roll the same one and the failure when they get it wrong is silent.

```python
class AckedStreamMasterIF(...):
    def write_frame(self, words, token):   # write words, MARK THE LAST, remember token
    def can_write_frame(self) -> bool:     # is a pending slot free?  AN ADMISSION CONDITION
    def harvest(self, n=N):                # BOUNDED; yields (token, status) for resolved frames
```

`can_write_frame()` is not a convenience. Without it the app either blocks on a full pending FIFO —
coupling the producer to the consumer's progress, and deadlocking if the consumer never resolves — or
accepts a frame it cannot remember, which breaks the correspondence silently. **The contract is
"check, then write"**, and the pysim twin should assert it rather than trust it.

**Slave** — two readers, and only one of them is synthesizable:

```python
class AckedStreamSlaveIF(...):
    def read_nb(self):        -> (item, mark) | None   # ONE item.  THE HLS TWIN.
    def send_status(self, payload)                     # non-blocking; one per marked item

    @sim_only
    def read_frame_nb(self):  -> (items, token) | None # a whole frame.  PYSIM ONLY.
```

`read_nb` is per-item because the hardware consumer is metronome-paced: it takes one sample per slot
and decides on each, so it can never consume a frame in one go. `read_frame_nb` is the **LT
approximation** — one SimPy event per frame instead of one per sample, which is what makes a
millisecond of signal simulable.

**The approximation is smaller than it looks.** The status is emitted only for the *marked* item, so
the RTL verdict already answers "did the last sample make it?" — not "did the whole frame?". A
per-frame read answers the same question, and the verdict does not diverge at all. What diverges is
`n_underrun`, which counts every filled slot; that is the already-declared block-granularity limit
(*Fidelity*, below), inherited rather than introduced.

**The one thing that must be right is status timing.** A frame read that reports immediately hands
the producer a verdict *before those samples would have played*, so the producer runs ahead of what
the hardware allows and every rate conclusion drawn from the model is optimistic. The frame reader
must **charge the playout** before reporting:

```python
@sim_only
def read_frame_nb(self):
    fr = self._take_frame()                       # non-blocking
    if fr is None:
        return None
    yield self.timeout(len(fr.items) * self.slot_period)   # CHARGE IT, then report
    return fr
```

That is the same correction that made the RX ingress twin honest (PR #160): a twin that consumes a
burst per firing and charges nothing is rate-blind, and rate-blind twins report zero loss where the
hardware loses samples.

Two properties make it different from credit, and both follow from *"only the consumer knows"*:

- **Solicited, not broadcast.** The producer marks the items it wants reported — normally the last of
  each transaction. That bounds the reverse rate by construction, which is what keeps rule 4 satisfied
  without a rate argument.
- **The outcome is semantic, not transport.** `PLAYED` / `MISSED` on the TX side is about a deadline,
  not about room. A successful forward delivery says nothing about it.

The producer needs no credit here because **it is allowed to block**: back-pressure costs it time and
nothing else. That asymmetry — the RX producer cannot be stalled (physics), the TX producer can (it
is only logic) — is what selects a different reverse channel on each side.

### HLS lowering

Every primitive in both interfaces already ships in synthesized, XSI-gated bodies: `read_nb`,
`write_nb`, plain `hls::stream`. **Nothing new has to be shown to work** — the main practical argument
for either of these over `stream_of_blocks`. In C++ each is a pair of streams plus two or three
registers in the producer, and the poll is a bounded unrolled `read_nb`.

## Receiver — uses `CreditStreamIF`

```
samp_in ─▶ ingress ─▶ stream⟨TaggedSamp⟩ ─▶ capture ─▶ CreditStream⟨RxResp+samples⟩ ─▶ consumer
                        (plain, write_nb)                        ▲
                                                                 └──────── ack ────────┘
```

### Tagged samples

```c
struct TaggedSamp {
    ap_uint<WR_BITS> wr;      // free-running sample index
    ap_uint<W>       samp;    // one wide sample word
};
```

The tag travels **with** the sample instead of on a side channel. That deletes staleness, the
safe/unsafe direction analysis, and `MARGIN` — all three exist only because the progress channel is
a stale lower bound.

### Ingress

```c
while (1) {
#pragma HLS PIPELINE II=1
    t.samp = samp_in.read();
    t.wr   = wr;
    if (!rx_fifo.write_nb(t)) n_dropped++;   // NEVER blocks; loss is counted
    wr++;                                    // implicit wrap
}
```

**A PLAIN `hls::stream` — no reverse channel at all** — which is the *when each is needed* rule from
the machinery section applied here:

> A **credit** channel is needed only where a producer must reserve capacity for a multi-item
> transaction it cannot abandon partway. A producer writing one item at a time needs nothing but
> `write_nb`.

The ingress writes a single word per iteration and has no future to plan for: `write_nb` returning
false *is* the never-stall property, and credit accounting would add a `read_nb`, two registers and
a reverse FIFO to the one loop in the design that must stay at II=1. The capture→consumer edge is
different — `ADMIT` has to know a whole window fits before it starts, because a capture cannot be
abandoned halfway — so that edge, and only that edge, is acked.

**This resolves the previous draft's first CRITICAL RISK** (*"we need to make sure `rx_fifo_out` has
sufficient buffer size that it never backpressures"*). It is no longer a sizing argument: the ingress
cannot block because `write_nb` cannot block. Depth still governs *how much* is lost under a slow
consumer, but not *whether the converter is stalled* — and the loss is counted rather than silent.

Measured at II=1 (`plans/witness/task_loop/`): a `while (1)` task body with a `read_nb` and a stream
write sustains one word per cycle indefinitely.

### Capture

Outer `while (1)` **unpipelined**; each inner loop pipelined; the loops are **siblings, not nested**.
That distinction is the whole scheduling story — nesting is what killed the loader.

```c
void rx_capture_task(hls::stream<TaggedSamp>& rx, hls::stream<RxCmdW>& cmd_in,
                     AckedMaster<W>& out, ...) {
    static CaptureState  state   = NO_CMD;
    static RxCmd         cmd;
    static TaggedSamp    first;
    static CompareState  compare;

    while (1) {
        if (state == NO_CMD) {
            out.poll_ack<NO_CMD_ACKS>();                    // 2-3: the producer is otherwise IDLE
                                                            // here, so acks would pile up unread
            if (cmd_in.read_nb(cmd)) { state = ADMIT; }     // do NOT drain first
            else {
                for (int i = 0; i < CMD_PERIOD; i++) {
#pragma HLS PIPELINE II=1
                    (void)rx.read();                        // discard; amortises the cmd check
                }
            }
        }

        else if (state == ADMIT) {
            out.poll_ack<ADMIT_ACKS>();                     // freshest credit before deciding
            if (misaligned(cmd)) {
                refuse(cmd.tid, RF_MISALIGNED, 0);  n_misaligned++;  state = NO_CMD;
            } else if (out.avail() < cmd.nsamp) {           // avail() already excludes RESP_WORDS
                refuse(cmd.tid, RF_NO_ROOM, 0);     n_no_room++;     state = NO_CMD;
            } else if (cmd.start_now) {
                state = CAPTURE;                            // no trigger: take the next nsamp
            } else {
                state = WAIT_FOR_TRIG;
            }
        }

        else if (state == WAIT_FOR_TRIG) {
            while (1) {
#pragma HLS PIPELINE II=1
                first   = rx.read();
                compare = time_compare(first.wr, cmd.samp_start);
                if (compare != BEFORE) break;
            }
            state = (compare == AFTER) ? REFUSE_TOO_OLD : CAPTURE;
        }

        else if (state == CAPTURE) {
            // start_now took no sample yet, so take one here and let it define the timestamp.
            if (cmd.start_now) first = rx.read();

            RxResp r = { cmd.tid, RF_CAPTURED, first.wr };   // wr = WHEN this capture began
            if (!out.write_resp_nb(r)) { n_resp_dropped++; state = NO_CMD; continue; }
            out.write_word(first.samp);                      // the sample already taken
            for (int i = 1; i < cmd.nsamp; i++) {
#pragma HLS PIPELINE II=1
                out.write_word(rx.read().samp);              // no per-word checks: room was reserved
            }
            n_captured++;  state = NO_CMD;
        }

        else /* REFUSE_TOO_OLD */ {
            refuse(cmd.tid, RF_CMD_TOO_LATE, first.wr);  n_too_old++;  state = NO_CMD;
        }
    }
}
```

**`CMD_PERIOD` is kept** — amortising the command check out of the per-sample path is the right idea.
One correction from the previous draft: on finding a command the transition must **skip the drain**,
or up to `CMD_PERIOD` samples are discarded and the trigger point can be thrown away with them.

**Polling acks in `NO_CMD` is not optional.** In every other state the producer is writing, so it
polls as it goes. In `NO_CMD` it writes nothing and could sit there for a long time — long enough
for the ack FIFO to fill, the consumer's `write_nb` to start failing, and `avail` to go stale-low.
The next command would then be refused for lack of room that had in fact been freed. `NO_CMD_ACKS`
of 2–3 costs a few cycles per `CMD_PERIOD` samples and removes that entirely.

**Every response is non-blocking.** `refuse()` and `write_resp_nb()` both draw on the reserved
`RESP_WORDS`, so they cannot be refused for room — but they still return a verdict, and a `False`
there is counted (`n_resp_dropped`) rather than ignored. The reservation is what makes that counter
expected-zero: a **dropped verdict is indistinguishable from a hang at the consumer**, so the design
must make it impossible rather than merely rare.

**`start_now`** — a flag on `RxCmd` meaning *"the next `nsamp` samples, whenever they are"*. It skips
`WAIT_FOR_TRIG` entirely and goes straight to `CAPTURE`. The response's `wr` field then reports
**when the capture actually began**, which is the point: it is how a host gets an initial timestamp
to schedule subsequent triggered commands against. (Named `start_now` rather than `no_samp_start` —
the flag reads at its use site, `if (cmd.start_now)`, without a double negative. Rename freely.)

**The response header is kept too**, and it is a good design: the consumer receives one object
carrying a verdict, followed by data only when the verdict is `RF_CAPTURED`. It just travels in the
forward stream rather than a block.

### `time_compare`, and the two rules that fall out

Three-way (`BEFORE` / `AT` / `AFTER`) over a **signed circular difference**, breaking on `!= BEFORE`.

- **Break on `>=`, never `==`.** A missed sample then costs a one-sample-late start instead of
  waiting a full counter wrap — at 32 bits and 1 GSa/s, a wrap is 4.3 s, delivered with a
  valid-looking response. That is worse than a hang because a hang is obvious.
- **`AFTER` is binary: refuse.** No "reasonable lateness" tolerance — any such threshold is `MARGIN`
  in a new costume and silently changes what the command meant.

**This resolves the previous draft's second CRITICAL RISK** (*"not clear if this pipelines since we
have a break"*). Measured 2026-08-19, `scratchpad/trig/`, achieved `PipelineII` from `csynth.xml`:

| variant    | shape                                                                    | II              |
| ---------- | ------------------------------------------------------------------------ | --------------- |
| `trig_a` | `while(1)` + `time_compare` + break                                  | **1**     |
| `trig_b` | same, no break (control)                                                 | 1               |
| `trig_c` | break on plain `==`                                                     | **1**     |
| `trig_d` | `SEARCH` then counted `CAPTURE`, siblings under an unpipelined outer | **1 / 1** |

`HLS 200-878` fired on the loader because an *unbounded inner loop* made the body unschedulable, not
because an exit test is inherently hard. `trig_a` schedules at depth 1 where the no-break control
gets depth 3 — Vitis cannot speculate past an exit test, which costs nothing in throughput and
slightly *helps* trigger latency.

### The admission decision — what closes the loop

The `ADMIT` state asks the output stream for room for the **whole** window before committing. If it
fits, the capture then runs with no possibility of stalling, because the credit was reserved. If not,
the command is refused with a status and a counter and the caller reissues.

That converts a run-time stall into an admission-time refusal, and closes the chain structurally:

> capture never blocks on its output → capture always drains the ingress stream → the ingress's
> `write_nb` always succeeds → the ADC never loses samples for a fabric reason.

Every link is a property of the mechanism, not a sizing argument or a usage contract. **This is what
resolves the third CRITICAL RISK** — the one the previous draft called exactly right, that a
consumer issuing a command before releasing its buffer would collapse the flow. Here it cannot: the
consumer's outstanding data is *visible as credit*, so capture declines rather than blocking.

### The half-wrap contract

`time_compare` is meaningful only while the two values are within `2^(WR_BITS-1)`. At 32 bits and
1 GSa/s that is **2.1 s** — comfortably beyond any plausible command latency, but a real bound that
belongs written beside `WR_BITS`, because the whole trigger scheme rests on it.

### Consumer

Unchanged in shape from the previous draft, and now safe either way:

```c
cmd.write_stream(cmd_out);            // issue
r = in.read_resp();                   // header carries the verdict
if (r.status == RF_CAPTURED) {
    process(in, cmd.nsamp);           // acks flow back as words are consumed
}
```

Sequential is **correct but serialised**. To overlap, issue command *k+1* before processing block
*k* — safe, because `ADMIT` will refuse rather than stall if the credit is not there. The measured
overlap on the SOB equivalent was 69 cycles per 64-sample block; there is no reason a credit stream
should do worse, but it is **not yet measured** and stage 2 must show it.

### Counters

`n_dropped` (ingress `write_nb` refused), `n_no_room`, `n_too_old`, `n_misaligned`, `n_captured`,
and `n_resp_dropped` — the last **expected permanently zero**, because `RESP_WORDS` is reserved. A
non-zero value means the reservation arithmetic is wrong, which is worth knowing loudly since the
symptom otherwise is a consumer that hangs.

Drive each of the others off zero at least once: a counter that has never counted is not evidence.

---

## Transmitter — uses `AckedStreamIF`

**Uses `AckedStreamIF`, not `CreditStreamIF`** — and the reason is the asymmetry that selects a
reverse channel, from *Reverse channels: credit and ack* above:

| | RX | TX |
|---|---|---|
| the producer is | an ADC — **cannot** be stalled (physics) | your logic — **can** be stalled |
| so back-pressure is | forbidden | **legitimate**, costing only the producer's time |
| so it needs to know | *may I write?* → **credit** | *did it go out on time?* → **ack** |

A blocking write from the loader is therefore correct: if it stalls, that is the producer's problem
and nothing is lost. What the producer cannot learn from back-pressure is whether its samples reached
the converter **at the slot they were meant for** — a deadline, not a resource — and only the player
observes that.

```
DUT ─(TxCmd + in-band samples)─▶ loader ─▶ stream⟨TaggedSamp⟩ ─▶ player ─▶ samp_out (AXIS → Rfdc)
 ▲                                  │                                │
 │                                  └──(TxResp: admitted / refused)───┘
 └───────────────────(TxStatus: cumulative played_through, n_underrun)─┘
```

**Two questions, two paths, deliberately separate:**

- *"Was my command accepted, and at which slot?"* — `TxResp`, emitted by the loader **immediately**,
  so it never waits and can take the next command.
- *"Did it actually play without gaps?"* — `TxStatus`, cumulative counters from the player, which the
  producer differences across its own window.

Folding the second into the first would make the loader wait for the window to play before
responding, serialising loading behind playout for no benefit.

### What the ack carries

The four shared rules apply unchanged — cumulative, non-blocking, bounded poll, and the saturation
hazard of rule 4, which here is answered structurally rather than by a rate argument: statuses are
**solicited**, so the reverse rate is one per command by construction.

| field | meaning |
|---|---|
| `slot` | the slot this status is about |
| `verdict` | `PLAYED` (real data went out) or `MISSED` (it arrived too late and was discarded) |
| `played_through` | highest slot emitted from **real** data |
| `n_underrun` | running total of slots filled because nothing was ready |

The producer differences the cumulative pair across its window: if `n_underrun` did not move while
slots `[start, start+nsamp)` played, every slot was fed. The player therefore holds **no per-block
state** — the producer does the arithmetic, which is what keeps its body a flat II=1 loop.

#### How the marks work

`TaggedSamp` carries a **`request_status`** bit, and the player emits a status **exactly when a
marked sample leaves the holding register** — written (`AT` → `PLAYED`) or discarded (`BEFORE` →
`MISSED`). Never for `AFTER`: that sample has not resolved yet and is still held.

The loader chooses the marks — normally the last sample of each window, which is precisely the "did
my block go out on time?" question. That is what makes the reverse rate one per command instead of
one per sample word, and it is how this channel satisfies rule 4 **structurally** where the RX credit
channel satisfies it only by a rate argument.

**No heartbeat, and no unsolicited traffic at all** — an earlier draft had one, and it turned out to
be solving a problem the design had created. See *Why there is no heartbeat, and no pre-check* below.

**Sizing rather than hoping:** exactly one status per accepted command means the FIFO needs
`depth >= MAX_IN_FLIGHT`, which is the same constant that bounds the `pending` FIFO. One number
governs both. `n_status_dropped` is **expected permanently zero**; non-zero means the sizing rule was
violated, which is worth hearing loudly rather than debugging as a lost verdict.

### Commands

```c
struct TxCmd    { tid; samp_start; start_now; nsamp; };
struct TxResp   { tid; status; samp_start; };        // ADMITTED | TX_TOO_LATE | TX_MISALIGNED
struct TxStatus { slot; verdict; played_through; n_underrun; };   // PLAYED | MISSED

struct TaggedSamp {
    ap_uint<WR_BITS> wr;              // the slot this sample is for (ignored when `now`)
    ap_uint<1>       now;             // play at the next available slot; the PLAYER assigns it
    ap_uint<1>       request_status;  // emit a TxStatus when this one resolves
    ap_uint<W>       samp;
};
```

`start_now` mirrors the RX flag: transmit immediately rather than at an absolute `samp_start`. It is
resolved by the **player**, not the loader — the loader sets the `now` bit on the window's samples and
the player places them at the next available slots, because it is the only thing that knows where
`slot` is. `TxResp.samp_start` then reports **where they actually went out**, recovered from
`TxStatus.slot`, which is how a producer learns where "now" was.

### Loader — HLS

A blocking write is correct here, so the body is plain: no credit, no `avail()`, no spin.

```c
void tx_loader_task(hls::stream<TxCmdW>& cmd_in, hls::stream<ap_uint<W> >& samp_in,
                    hls::stream<TaggedSamp>& to_player, hls::stream<TxRespW>& resp_out,
                    hls::stream<TxStatusW>& status_in) {
    static LoadState      state = NO_CMD;
    static TxCmd          cmd;
    static ap_uint<IDX_W> slot;
    static TxStatus       st;                         // freshest view of the player

    while (1) {
        if (state == NO_CMD) {
            harvest<STATUS_POLLS>(status_in, st, pending, resp_out);   // BOUNDED; never a drain loop
            if (cmd_in.read_nb(cmd)) state = ADMIT;
        }

        else if (state == ADMIT) {
            poll_status<STATUS_POLLS>(status_in, st);
            slot = cmd.samp_start;                 // ignored when start_now: the player assigns

            // NO too-late pre-check here.  The player already detects lateness (BEFORE -> MISSED),
            // and a second detector fed by a stale view would be a second source of truth for one
            // condition.  A doomed window costs stream bandwidth and nothing at the DAC.
            if      (misaligned(cmd)) { state = REFUSE_MISALIGNED; }
            else if (pending.full())  { state = REFUSE_NO_SLOT;    }
            else {
                // ACCEPTED -> do NOT respond yet.  Remember it; the player's verdict answers it.
                pending.push(Pending{cmd.tid, slot, cmd.nsamp});
                state = LOAD;
            }
        }

        else if (state == LOAD) {
            for (int i = 0; i < cmd.nsamp; i++) {
#pragma HLS PIPELINE II=1
                TaggedSamp t;
                t.samp           = samp_in.read();     // in-band payload, behind the command
                t.now            = cmd.start_now;      // if set, the PLAYER assigns the slot
                t.wr             = slot + i;           // ignored when `now`
                t.request_status = (i == cmd.nsamp - 1);   // the LAST sample answers the question
                to_player.write(t);                    // BLOCKING is correct: stalling is our problem
            }
            state = NO_CMD;
        }

        else /* REFUSE_* */ {
            respond(cmd.tid, status_of(state), slot);   // refusals answer IMMEDIATELY; nothing pends
            for (int i = 0; i < cmd.nsamp; i++) {      // DRAIN THE FRAME ANYWAY
#pragma HLS PIPELINE II=1
                (void)samp_in.read();
            }
            state = NO_CMD;
        }
    }
}
```

#### Responses are deferred: one `TxResp` per command, answered by the player

A refusal is answered **immediately** — nothing has been loaded, so there is nothing to wait for. An
**accepted** command is not answered at acceptance. It is pushed onto a small `pending` FIFO as
`{tid, slot, nsamp}`, and the loader answers it when the player's verdict for that window comes back:

```c
template <int N>
void harvest(hls::stream<TxStatusW>& status_in, TxStatus& st,
             Fifo<Pending>& pending, hls::stream<TxRespW>& resp_out) {
    for (int i = 0; i < N; i++) {              // BOUNDED; N is compile-time
        TxStatus s;
        if (!status_in.read_nb(s)) break;
        st = s;                                 // cumulative fields refresh
        Pending pd = pending.pop();             // EVERY status is a reply; in load order
        // start_now windows had no slot until the player assigned one -- recover it from the
        // status, which reports where the LAST sample of the window actually went out.
        ap_uint<IDX_W> start = pd.start_now ? (s.slot - (pd.nsamp - 1)) : pd.slot;
        respond(pd.tid, (s.verdict == PLAYED) ? TRANSMITTED : TX_TOO_LATE, start);
    }
}
```

**Why this is better than answering at acceptance.** There is exactly one `request_status` per
accepted command, so exactly one status comes back, so exactly **one `TxResp` per command** — and the
producer gets a definitive verdict per `tid` without differencing any counters. The cumulative fields
remain available for a producer that wants them, but nothing depends on that arithmetic any more.

**Ordering needs no matching.** The player processes samples in slot order and windows are loaded in
order, so statuses return in the order commands were pushed. `pending.pop()` in order is correct;
matching by `tid` would be redundant machinery.

**Every status is a reply**, so `pending.pop()` is unconditional and there is no "is this for me?"
test. That is a consequence of dropping the heartbeat — see below.

**`pending` full is an admission condition, not an assertion.** If a command were accepted with
nowhere to record its `tid`, the correspondence would break *silently* — so a full FIFO is a
refusal (`TX_NO_SLOT`), counted like any other. That also makes one constant govern both channels:
`MAX_IN_FLIGHT` is the `pending` depth **and** the bound the status FIFO is sized against.

#### Why there is no heartbeat, and no pre-check

An earlier draft had the player emit an unsolicited status every `STATUS_PERIOD` slots, to keep the
loader's view of the play position fresh. **It was solving a problem the design created.**

`played_through` is *highest slot emitted from real data*, so when nothing is loaded it does not
advance — the player is underrunning every slot. A heartbeat would have reported the same value
repeatedly and refreshed nothing. What advances while idle is `slot`, which the loader was never
sent.

The loader wanted a fresh position for two things, and neither survives inspection:

- **A `TX_TOO_LATE` pre-check in `ADMIT`** — a *second* lateness detector, fed by a staler view than
  the player's. Two sources of truth for one condition is the failure mode this whole plan exists to
  remove. Dropped: the player's `BEFORE → MISSED` is the answer, and it reaches the producer as
  `TX_TOO_LATE` on the `TxResp`.
- **`start_now`** — the only real need, and better answered by the player, which is the only thing
  that knows where `slot` is. The `now` bit says *"the next available slot"*; consecutive `now`
  samples land on consecutive slots for free, because one sample is consumed per slot.

Removing both deletes the heartbeat, the three-way verdict, `STATUS_PERIOD`, and the sizing
interaction between heartbeat rate and `nsamp`. The status FIFO now needs only
`depth >= MAX_IN_FLIGHT`, because statuses are exactly one per accepted command.

A `start_now` window **cannot be late** — it plays when it plays — so its verdict is always
`PLAYED`, and `TxStatus.slot` is how the producer learns *where* it landed.

**A refused command must still drain its payload.** Otherwise the next command's samples are read as
this one's, and every command after it is misaligned. This is the trap the old loader documented in
prose — *"a refused command whose payload is left in the stream desynchronises every command after
it"* — and it is why the refuse path has a counted loop rather than an early exit.

**There is no `START_NOW_LEAD`, and that is the point.** An earlier draft had the loader place a
`start_now` window at `played_through + LEAD` — a tunable nobody could derive, guarding against a
staleness the loader should not have had to reason about. Setting the `now` bit and letting the
player place the samples removes the constant entirely. Worth noting because the reflex is to add a
margin: two constants in `rf_samp_buf_tx.py` were introduced that way, inherited by symmetry, and
both were wrong (`plans/adc_model.md`). **A constant you cannot derive is usually a design smell,
not a parameter.**

### Loader — Python

Structurally identical, and because the write is blocking it needs no rate model at all:

```python
class TxLoader(FreeRunMod):
    def run_iter(self):
        yield from self._harvest()                     # answer whatever resolved since last time
        cmd  = yield from self.cmd_in.get(TxCmd)
        slot = int(cmd.samp_start)                     # ignored when start_now: the player assigns

        if misaligned(cmd):
            refuse = TX_MISALIGNED; self.count_misaligned()
        elif not self.to_player.can_write_frame():            # CHECK, then write
            refuse = TX_NO_SLOT;    self.count_no_slot()
        else:
            refuse = None;          self.count_admitted()

        if refuse is not None:
            yield from self._respond(cmd.tid, refuse, slot)   # refusals answer IMMEDIATELY

        samples = yield from self.samp_in.get(count=int(cmd.nsamp))   # drain EITHER WAY
        if refuse is None:
            yield from self.to_player.write_frame(
                tag(samples, slot, now=cmd.start_now),        # blocking write: correct on TX
                token=Pending(cmd.tid, slot, cmd.nsamp, cmd.start_now))

    def _harvest(self):
        """Answer whatever the player resolved.  The FIFO and the ordering are the INTERFACE's;
        only the slot recovery and the status-to-TxResp mapping are this module's."""
        for tok, st in self.to_player.harvest():
            start = (st.slot - (tok.nsamp - 1)) if tok.start_now else tok.slot
            yield from self._respond(tok.tid,
                                     TRANSMITTED if st.verdict == PLAYED else TX_TOO_LATE, start)
```

Note the drain is unconditional in both models, and for the same reason. Writing it as a single
`get` outside the branch is what makes the two twins obviously the same shape.

### Player — HLS

```c
void tx_player_task(hls::stream<TaggedSamp>& fwd, hls::stream<ap_uint<W> >& samp_out,
                    hls::stream<TxStatusW>& status_out) {
    static ap_uint<IDX_W> slot           = 0;
    static ap_uint<IDX_W> played_through = 0;
    static ap_uint<IDX_W> n_underrun     = 0;
    static TaggedSamp     h;
    static bool           held           = false;
    static ap_uint<W>     last           = 0;         // for a repeat-last filler

    while (1) {
#pragma HLS PIPELINE II=1
        if (!held && fwd.read_nb(h)) held = true;     // one-element peek: hls::stream has none

        bool     resolved = false;                    // a MARKED sample left the register
        ap_uint<2> verdict  = 0;

        // `now` means "the next available slot" -- the player assigns it, because the player is
        // the only thing that knows where `slot` is.  Consecutive `now` samples land on consecutive
        // slots for free: one sample is consumed per slot, so no base register is needed.
        CompareState c = held ? (h.now ? AT : time_compare(h.wr, slot)) : AT;

        if (!held) { samp_out.write(FILLER(last)); n_underrun++; }
        else switch (c) {
            case AT:     samp_out.write(h.samp); last = h.samp;
                         played_through = slot; held = false;
                         resolved = h.request_status; verdict = PLAYED;        break;
            case AFTER:  samp_out.write(FILLER(last)); n_underrun++;           break;  // still held
            case BEFORE: held = false;
                         resolved = h.request_status; verdict = MISSED;        continue; // stale
        }

        // REQUESTED, not broadcast.  A per-cycle write saturates the FIFO, after which write_nb
        // drops the NEWEST while the reader pops the OLDEST -- the loader would read ancient
        // numbers forever, and "the newest supersedes" would quietly stop being true.
        if (resolved) {
            TxStatus s = { slot, verdict, played_through, n_underrun };
            if (!status_out.write_nb(s)) n_status_dropped++;   // expected zero; see the sizing rule
        }
        slot++;
    }
}
```

Nothing in that body blocks — the TX obligation, exactly as never-stall is the RX one. Every loop in
the design is now flat-`while(1)` or counted: no nesting, no spin, no data-dependent trip count.
Measured shapes: `while(1)` with `read_nb` at II=1, and a data-dependent `switch`/`break` at II=1
(`plans/witness/task_loop/`, `scratchpad/trig/`).

**The `BEFORE` case deliberately does not count.** A stale sample was already accounted for by the
`n_underrun` that fired at the slot it missed; counting it again double-reports one event. The old
design's separate `too_late` counter is redundant under cumulative status.

### Player — Python: derived, not duplicated

**This is the part genuinely harder than the C, and the reason is structural.** The underrun is
detected by a *different object* in each backend:

- in RTL by the **player** — holding register empty at a slot boundary;
- in pysim by the **edge** — `RFSampIF._drain_one` finds `_buf` empty on the metronome, zero-fills,
  `underrun += 1`.

A pysim player that *also* counted underruns would give one backend two counters for one phenomenon,
computed at different granularities — fabric rate versus the sample grid — and they would disagree.

**So the pysim player does not model the decision at all.** Its only job is to keep the converter fed;
the existing metronome already counts what happens when it fails. It needs no slot counter, no
holding register and no `time_compare` — all three exist in the C to solve a problem pysim does not
have, because pysim's back-pressure chain already paces it: `put` blocks on a bounded `_buf` →
`_dac_proc` stalls → the AXIS write stalls.

```python
class TxPlayer(FreeRunMod):
    """Pysim twin: strip the tag, feed the converter, and REPORT WHAT THE EDGE SAW."""

    def run_iter(self):
        fr = yield from self.fwd.read_frame_nb()           # LT: one event per FRAME, not per sample
        if fr is None:                                     # (and it CHARGES the playout internally)
            yield self.timeout(self.slot_period)           # idle a slot; the edge counts the underrun
            return
        yield from self.samp_out.write(strip_tags(fr.items))   # blocking; the edge paces us
        yield from self.fwd.send_status(self.status_word(fr))  # one per marked item

    @sim_only
    def status_word(self):
        """ONE SOURCE OF TRUTH PER BACKEND.

        In RTL these come from the player; here from the edge that actually owns the sample grid.
        Same numbers, different observer — and the cross-backend gate is what proves it, rather than
        two independent implementations that agree by luck.
        """
        edge = self.tx_edge()                              # the bound RFSampIF
        return TxStatus(played_through=edge.blocks_delivered * self.blksize,
                        n_underrun=edge.underrun)
```

`@sim_only` is doing real work here: reading another module's counters is meaningless in hardware,
and the extractor's implicit-capture rule exists precisely to force that to be said out loud rather
than baked silently into a body.

### Fidelity — two limits to declare, not discover

**1. pysim cannot see a partially-late block.** The edge either has a block or it does not; the RTL
player fills slot by slot. A block that is 90% on time reads as **fully** underrun in pysim and as
10% underrun in RTL. This is the same sub-block blindness as `rf_loopback`'s 72-of-512
(`docs/guide/rf/python/fidelity.md`) — not new, but it lands on a *different counter* here and will
look like a gate failure if it is not stated first.

**2. The filler differs from the hardware.** The model emits zeros; the real RFDC **repeats the last
frame** and ignores `TVALID` entirely (`plans/adc_model.md` § *What the real RFDC does that this
model does not*). The counter means the same thing either way; a scope trace will not match a
simulation. Choose deliberately and write the choice beside `FILLER`.

### The gate: `assert_clean`, not `underrun == 0`

`RFSampIF.assert_clean(n)` already exists and is strictly stronger: it asserts `underrun == n`
**exactly** *and* `last_underrun_idx <= n`. A converter fed through a pipeline **must** underrun for
its first `n` blocks — the data has not arrived yet — and must never underrun afterwards. `== 0`
passes designs that recover by accident; `assert_clean` fails every one of them.

The cross-backend check is then one comparison: RTL's `player.n_underrun` against pysim's
`RFSampIF.underrun` for a single scenario. That is `plans/behavioral_edges.md` S4, and this is its
first real user.

### Counters

`n_underrun` (player: slots filled), `n_misaligned` and `n_no_slot` (loader: commands refused
up front), `n_too_late` (loader: windows the **player** reported `MISSED`), `n_admitted`, and
`n_status_dropped` — the last **expected permanently zero**, because the status
rate is bounded by design (one per command plus a heartbeat) and the FIFO is sized against commands
in flight. Non-zero means the sizing rule was violated, and the symptom otherwise is a `start_now`
placed against a stale play position — which looks like a design bug, not a sizing one.

Drive each of the others off zero at least once — a counter that has never counted is not evidence.

## What this does not do

**It deletes pre-trigger history.** Samples flow through a FIFO and are discarded in `NO_CMD`, so a
window starting in the past can never be served — the tag has gone by. Only *future* windows are
admitted, which the `AFTER` refusal makes explicit rather than silent.

That covers scheduled capture — "start at sample N", MTS-coordinated measurement — which is a large
share of real SDR use. It does **not** cover trigger-on-detection with pre-trigger context, the
oscilloscope case, which is what the BRAM's absolute addressing exists for.

**So this is a second module, not a replacement.** `RfSampBuf` keeps the history case. Decide in
writing which examples move.

---

## Measured, and not

| claim                                                              | status                                                     |
| ------------------------------------------------------------------ | ---------------------------------------------------------- |
| `while (1)` in a task body reaches II=1                          | **measured** — `plans/witness/task_loop/`         |
| a pipelined loop with a data-dependent `break` reaches II=1       | **measured** — `trig_a`, `trig_d`               |
| the three-way `time_compare` chain costs nothing vs `==`        | **measured** — `trig_c`                           |
| sibling pipelined loops under an unpipelined outer both reach II=1 | **measured** — `trig_d`                           |
| `read_nb` / `write_nb` synthesize and pipeline                 | **measured** — four shipped bodies, XSI-gated       |
| SOB wedges under interrupted supply                                | **measured, not diagnosed** — `scratchpad/chain/` |
| `CreditStreamIF`'s accounting is correct at RTL                       | **NOT measured**                                     |
| the admission decision prevents every capture-side stall           | **NOT measured**                                     |
| the ingress `write_nb` never blocks at RTL                        | **NOT measured**                                     |
| a pipelined consumer overlaps as well as the SOB's 69 cyc/block    | **NOT measured**                                     |

The first five are why this is believable. The last four are what the stages have to prove.

---

## Staging

Five stages, each with a cycle gate. **A stage without a gate is not done.** The three examples are
ordered so a failure can always be attributed: TX alone, then TX+RX on one grid, then the full loop.

### Stage 0 — both reverse channels in pysim

`CreditStreamIF` and `AckedStreamIF`, the four shared rules, and the endpoint API. No hardware.

Four tests, and the last two exist because they cover failures nothing currently does:

| test | what it proves |
|---|---|
| credit driven to zero and back | the accounting debits and restores |
| reverse values deliberately dropped | the cumulative form self-heals (rule 1) |
| counters walked **across the wrap** | the Python twin masks; unbounded ints stop matching RTL exactly there |
| **saturation** — writer outpacing reader | the reader receives the *oldest* values, which is the failure rule 4 names |

Plus the `can_write_frame()` → `write_frame()` contract asserted rather than trusted: calling
`write_frame` on a full pending FIFO is the silent-correspondence bug, and the twin should refuse it.

### Stage 1 — Example 1: the repeat player (TX only)

**`examples/rf_repeat_play`.** The host loads a waveform of `nsamp` samples and replays it forever on
a fixed period. TX path only, so nothing in the RX half can confuse a diagnosis.

**How the host learns "now": `start_now` on the first play, and nothing else.** The player assigns
the slots; the status reports where the last sample landed; `TxResp.samp_start` comes back as
`status.slot - (nsamp - 1)`. Every repeat after that is scheduled **absolutely** at
`samp_start + k * period`.

> **Do NOT use a zero-length probe command to discover the current slot.** A zero-length frame has no
> last sample, so `request_status` is never set, so no status returns and the pending token pushed at
> `ADMIT` never pops. A few of those and the loader refuses everything with `TX_NO_SLOT`, for reasons
> that will look nothing like the cause. The loader cannot answer such a command itself either — it
> does not know the current slot, which was the whole point of asking.
>
> **Decide `nsamp == 0` explicitly**: either refuse it in `ADMIT` beside `misaligned`, or give the
> marking rule a special case. Leaving it undefined is what produces the leak.

**The gate is not just "it played".** Three assertions:

1. **The schedule holds.** Consecutive plays land exactly `period` apart, over enough repeats to
   catch slot arithmetic drifting rather than jumping.
2. **`TX_TOO_LATE` is driven off zero** — schedule one play in the past and see the player's `MISSED`
   arrive as `TX_TOO_LATE` on that `tid`, with every other `tid` unaffected.
3. **`n_underrun` is driven off zero and recovers** — starve the host briefly, then confirm the
   periodic schedule resumes on the original grid rather than re-based on the gap.

Assertion 3 is what the example is really for: an all-green repeat test proves far less than one that
survives a gap.

### Stage 2 — Example 2: timed capture (TX + RX on one grid)

**`examples/rf_timed_capture`.** Play the repeating waveform, loop it back through the RF
environment, and issue a *timed* capture. Because the waveform is known and its absolute start is
known, the samples at any index are known — so a capture at index `N` must contain a specific slice.

**This is the strongest of the three**, because it tests the thing nothing else does: that the RX
sample counter and the TX slot counter are on the **same grid**. That is what `t0` exists to
guarantee, and a misalignment shows up as a *shifted slice* rather than as noise.

> **Assert a measured, CONSTANT delay — not zero.** A zero-sample loopback may not be achievable; the
> ADC and DAC metronomes are separate grids and a fixed offset is likely. That is an improvement, not
> an obstacle: asserting zero is **weaker**, because a model that ignores timing altogether also
> produces zero. A constant non-zero offset holding across every capture is positive evidence the two
> grids are locked.
>
> Measure it once, pin it as a gate constant, and treat a change as a finding needing an explanation —
> the same discipline as the cycle gates.

Also here, because this is the first example with a real RX consumer:

- **`n_no_room` driven off zero** — a consumer slow enough that `ADMIT` refuses, proving the
  admission decision refuses rather than stalling (which is the whole claim of the credit channel).
- **`start_now` on the RX side** — the initial-timestamp case, which is how a host bootstraps before
  it can schedule anything at all.

### Stage 3 — Example 3: `BlkDelay` on the new modules

The pattern-B loop rebuilt on `CreditStreamIF` / `AckedStreamIF`, with the ceiling recomputed as
`max` over stages. **It should now be 1 cycle/word everywhere** — i.e. port capacity, 1 GSa/s at
`samp_per_word = 4` and 250 MHz — where the BRAM design is stuck at 500 MSa/s because capture and
loader both sit at 2.

That number *is* the gate: if the ceiling is not 1 everywhere, one of the new stages has an inner
loop that should not be there, and the witness harness (`plans/witness/task_loop/`) is how to find it.

### Stage 4 — retire or keep

Decide in writing what moves off `RfSampBuf` and what stays. The BRAM design keeps triggered capture
with pre-trigger history; this one takes streaming and scheduled capture. See *What this does not do*.

### What no stage covers, deliberately

- **The SOB wedge** (`scratchpad/chain/`) — worth diagnosing on its own account, since
  `examples/interleaver` depends on SOB and currently satisfies the one working configuration by
  construction rather than by design. Not on this plan's path.
- **Partial-block lateness.** pysim cannot see it (see *Fidelity*), so no pysim gate can assert it;
  only an RTL run can, and only through `n_underrun`.

## Open questions

- **Is `nsamp == 0` legal?** Stage 1 forces the decision: a zero-length frame has no sample to mark,
  so it never resolves and leaks a pending slot. Refuse it, or special-case the marking rule.
- **Does the consumer need whole blocks?** If it streams, this design is complete. If it needs a
  block in hand (FFT, sort), a block boundary must come from somewhere and a credit stream does not
  supply one.
- **Ack granularity vs depth.** If the consumer acks per block, credit returns in chunks and `depth`
  must comfortably exceed one chunk, or the producer waits for a credit that only arrives at a block
  boundary. Wants a declared rule in the shape of `check_rate`.
- **What moves off `RfSampBuf` and what stays** — see *What this does not do*.
- **The SOB wedge.** Worth diagnosing on its own account: the interleaver depends on SOB and
  currently satisfies the one working configuration by construction rather than by design, so nobody
  knows how close to the edge it sits.
