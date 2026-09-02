# `LockedT2pMemIF` — a lock channel over a shared true-dual-port memory

**Status: S1 AND S2 BUILT AND RTL-GATED. Both consumers have since been renamed or absorbed —
see the note below.** Started 2026-09-01. Owns the shared-memory handover primitive and its first
consumer (infinite play on `RfShotTx`). Does **not** own `RfShotBuf` Stage C, which is
`plans/rf_shot_buf.md`'s — this plan exists to give Stage C an interface it can reach for.

> **Where S1's and S2's products live now.** `plans/rf_shot_unify.md` **Stage B** (2026-09-02)
> deleted `waveflow/hw/rf_shot_loop.py`, `examples/rf_shot_loop` and its gate: the infinite player
> was merged into `waveflow/hw/rf_shot_tx.py`'s `RfShotTx`, which now serves both opcodes on one
> lock, and `examples/rf_shot_unified` is the example that drives it. S2's receiver was **renamed**
> — `rf_pingpong_rx.py` → `waveflow/hw/rf_shot_rx.py`, `RfPingPongRx` → `RfShotRx`,
> `examples/rf_pingpong_rx` → `examples/rf_shot_rx` — with every measurement below unchanged; only
> the composite's name moved, and the leaf tasks kept theirs. **Everything below is the record of
> what S1 and S2 built and measured, and is left as written.** Follow the names in this note, not
> the ones in the sections.

---

## Next session starts here — S1

```
claude "Read plans/t2p_lock_chan.md, sections 'The interface' through 'S1', and build S1.
        Build the SINGLE-REGION case only.  The region parameter is specified so the
        contract does not change later, NOT so it can be built now -- see 'Scope discipline'."
```

---

## Why it exists, and the evidence that says build it narrow

`RfShotTx` today cannot change waveform without stopping: a load arriving mid-play is refused with
`SHOT_BUSY`. The lab flow is *load a waveform, run it a long while, then change it*, so infinite play
plus a clean handover is the missing capability.

That could be a one-off abort channel — one `StreamIF`, one `read_nb`, roughly a tenth the work.
This plan chooses the general interface instead, and the reason is that the same handover is Stage
C's: an RX capture that fills a buffer and hands a window to a reader is the identical phase
separation, viewed from the other side.

**But the repo already has one receipt for building an interface ahead of its consumer.**
`CreditStreamIF`, `CreditStreamMasterIF` and `CreditStreamSlaveIF` are fully written, documented with
three named rules, codegen-ready — and instantiated by **no design anywhere**, because the receiver
they were built for was deferred. Its sibling `AckedStreamIF` shipped in the same stage and is used
by `rf_tx_stream` / `rf_repeat_play`, because *that* consumer was built in the same arc.

The difference is not quality. It is that an un-consumed interface is **unverified**: `AckedStreamIF`
carries the annotation *"rule 1 does NOT cover the ack SEQUENCE"*, a correction found only by having
a consumer. `CreditStreamIF` carries no such correction — not because its contract is better, but
because nothing has ever tried to satisfy it.

**So the rule this plan follows: build the abstraction with its first consumer, in the same commit
arc, and build only the case that consumer needs.**

## Scope discipline

| | S1 | later |
|---|---|---|
| regions held at once | **exactly one** | many |
| requesters | **one** | many, needing an allocator |
| region bounds | `[start_addr, end_addr)`, checked | same |
| who may write | port A only | either, once the memory's assertion is symmetric |

The region bounds are in the **message from day one** even though S1 holds one region, so the wire
format does not change when S2 needs two. What S1 does not build is the allocator, the multi-holder
bookkeeping, or a second requester.

**The region parameter is an RX feature, and that is why it is specified but not built.** On TX a
handover gap is acceptable — you already accepted discontinuity. On RX a gap is *dropped samples*,
because you cannot back-pressure an ADC. So S2 is where two regions stop being an optimisation and
start being correctness.

## What it is not

**It does not replace `StreamOfBlocksIF`.** SOB is built, gated, and used by `examples/interleaver`;
it stays. The relationship is worth stating only because it locates this primitive in the space:

| | SOB | `LockedT2pMemIF` |
|---|---|---|
| buffers | fixed **2** (`hls::stream_of_blocks<T[N], 2>`, a literal in `composite_gen.py:649`) | `[start, end)`, any number, any size |
| capacity cost | 2× the working set | 1× at one region; *n*× at *n* |
| concurrency | full, always | none at one region; full at two or more |
| lock cost | RAII scope entry/exit, **no channel traffic** | request + grant + release messages |
| granularity it suits | block cadence (µs) | transaction cadence (ms) |

**The message lock is the wrong tool at block cadence.** Three transfers per swap is free when a shot
plays for milliseconds and dominates when a block turns over in microseconds. Put that sentence in
the class docstring — it is the one place SOB stays strictly better, and a reader needs to know.

---

## The interface

Three objects, the same shape as `AckedStreamIF`: two endpoints and the interface that binds them.

```python
from waveflow.hw.bram import T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.locked_mem import (LockedT2pMemIF, LockedMemMasterIF,
                                    LockedMemSlaveIF)

mem = T2pBram(sim=sim, name="mem", element_type=word_element(64), nelem=1024)

req = LockedMemMasterIF(sim=sim, name="loader", element_type=word_element(64), nelem=1024)
own = LockedMemSlaveIF(sim=sim, name="player", element_type=word_element(64), nelem=1024,
                       check_period=16)

lk = LockedT2pMemIF(sim=sim, name="buf_lock", clk=Clock(freq=250e6),
                    element_type=word_element(64), nelem=1024, memory=mem)
lk.bind("master", req)
lk.bind("slave", own)
```

**The roles are asymmetric, and the names follow the repo's convention that a master initiates.**

- **`LockedMemMasterIF` — the requester.** Holds nothing by default. Asks for a region, is granted
  it, uses it, releases it. The *bursty* side: the loader on TX, the window reader on RX.
- **`LockedMemSlaveIF` — the owner.** Holds the whole memory by default and must yield on request.
  The *continuous* side: the player on TX, the capture on RX.

That mapping is the same in both directions and is worth stating as the rule: **the side that cannot
stop is the owner; the side that arrives with a transaction is the requester.**

### Parameters

| on | parameter | meaning |
|---|---|---|
| `LockedT2pMemIF` | `element_type` | what one memory location holds — a `DataSchema` field, as `BramIF` |
| | `nelem` | memory depth in **elements**; bounds every region |
| | `memory` | the `T2pBram` this lock arbitrates. The interface wires both of its ports |
| | `cmd_depth` | lock command queue depth (1 by default — one outstanding request at S1) |
| `LockedMemMasterIF` | `element_type`, `nelem` | must match the interface; `bind` refuses a disagreement |
| `LockedMemSlaveIF` | `element_type`, `nelem` | same |
| | `check_period` | **the contract that makes a grant bounded** — the maximum elements of its own work the owner may do between polls of the lock channel. A grant therefore arrives within `check_period` element-times, and a gate can assert it |

`check_period` is the parameter that does the real work. Without it the requester's blocking wait for
a grant has no bound and "the owner is busy" is indistinguishable from a deadlock — the same reason
`SHOT_END` exists as a quiescence probe. With it, the wait is a stated number and a violated one is a
finding.

Under the hood the interface holds **four** channels — two `BramIF` ports to the memory and two
`StreamIF`s for the lock protocol. **You do not construct them and you do not bind them**; the two
`bind` calls above are the whole wiring, exactly as with `AckedStreamIF`'s `fwd_if` / `ack_if`.

### Messages

Two schemas, one word each at 64 bits. Generated from `DataList` like every other message here, so
no body ever touches a bit range.

**`MemLockCmd`** — requester → owner

| field | width | meaning |
|---|---|---|
| `opcode` | 8 | `LOCK_ACQUIRE` (0) or `LOCK_RELEASE` (1) |
| `start_addr` | `addr_bits` | first element of the region, inclusive |
| `end_addr` | `addr_bits` | one past the last element, **exclusive** |

**`MemLockResp`** — owner → requester

| field | width | meaning |
|---|---|---|
| `status` | 8 | `LOCK_GRANTED` (0), `LOCK_BAD_RANGE` (1) |
| `start_addr` | `addr_bits` | the granted region, echoed |
| `end_addr` | `addr_bits` | echoed |

Half-open `[start, end)` throughout, for the reason Python slices are: adjacent regions are written
`[0, 256)` and `[256, 512)` with no ±1 anywhere, and an empty region is `start == end` rather than a
special case.

**The region echoes back on the grant.** With one outstanding request it is redundant for
correlation, and it is there anyway for two reasons: a waveform becomes readable without cross-
referencing the command, and S2's multi-region case needs it without a format change.

**`LOCK_RELEASE` is not answered.** With one requester there is nothing to race against, and a
response would be a second thing to get wrong. The requester's obligation is a hard barrier: **after
writing `RELEASE`, do not touch the region again.** S2 revisits this if a second requester appears.

**No application data rides this channel.** `RfShotTx`'s `nsamp` and `nrepeat` stay on the existing
`rep` channel. Putting them in the lock message would be the fastest way to make a general primitive
domain-specific, and the whole argument for building this rather than an abort channel is that it is
not.

### Protocol

```
requester                                  owner
    |                                        | holds everything, working
    |-- ACQUIRE [start, end) --------------->|
    |                                        | finishes at most check_period elements
    |                                        | stops touching [start, end)
    |<------------- GRANTED [start, end) ----|
    | reads/writes [start, end)              | continues outside the region (or idles)
    |-- RELEASE ---------------------------->|
    | must not touch the region              | resumes [start, end)
```

Four rules a user must obey.

**A grant covers exactly the region asked for.** Touching one element outside it is the read-during-
write collision this interface exists to prevent, and at S1 — where the owner yields the *whole*
memory — the region is documentation rather than enforcement at RTL. **pysim enforces it anyway**,
which is the point of the next section.

**Release is a barrier, not a hint.** The owner may resume the instant it sees the release.

**One outstanding request.** A second `ACQUIRE` before the first is released is a protocol error at
S1, not a queued request.

**A bad range is refused, not clamped.** `end > nelem`, or `start > end`, answers `LOCK_BAD_RANGE`
and grants nothing — the same discipline as `SHOT_WRONG_LEN`, and for the same reason: a clamped
region is a different region, silently.

## The payoff that is not concurrency

**Region ownership is checkable in pysim; address collision is not.**

Today `BramIFSlave.read` / `write` are a plain `load` / `store` with only an access-*kind* check
(`bram.py:357-369`). The invariant the whole shot design rests on — writer and reader never overlap —
has **zero pysim enforcement**. It is caught only by scanning a traced XSI run's VCD, and
`bram_trace.py:26-29` concedes that is *"a weaker thing than the assertion firing — a second
implementation of the same predicate."*

With a lock, both endpoints know which range they hold, so every access can assert membership in both
backends:

```python
def _check_held(self, addr: int) -> None:
    if not (self._lo <= addr < self._hi):
        raise RuntimeError(
            f"{self.name}: touched element {addr} while holding [{self._lo}, {self._hi}). "
            f"This is the read-during-write collision bram_t2p.v's $error catches at RTL — "
            f"and XSI discards $error, so pysim is where it has to be caught.")
```

That converts a hazard that is currently **silent in pysim and awkward at RTL** into a loud one
everywhere. It is the strongest argument for this interface and it survives even if the concurrency
argument does not.

---

## Sketches

### Python — the requester (loader)

```python
class ShotTxLoad(FreeRunMod):
    def run_iter(self):
        hdr = yield from self.s_in.get_schema(ShotTxHdr)
        # ... decide, as today ...
        if accept:
            lo, hi = self.base, self.base + self.nword
            yield from self.lock.acquire(lo, hi)          # blocks; bounded by check_period
            # ONE burst in, ONE anchored write out.  Never a per-word loop here — see below.
            x, t0 = yield from self.s_in.get_pipelined(self.element_type, count=self.nword)
            yield from self.lock.write_pipelined(x, addr=lo, t_start=t0)
            yield from self.lock.release()
        yield from self.resp_out.write_schema(resp)
```

**`t_start` is what makes the memory write free.** `BramIFSlave.write_pipelined` elapses `count`
cycles at II=1, *"and with `t_start` in the past it elapses less — that shortening **is** the
overlap"* (`bram.py:495-500`). Passing the anchor `get_pipelined` back-calculates makes the two
phases cost `max(a, b)` instead of `a + b`, which is what the II=1 RTL actually does: a word arrives
and a word is stored in the same cycle. Dropping `t_start` would charge the design twice for one
pipeline.

The endpoint forwards `BramIF`'s access methods — `read`, `write`, `read_pipelined`,
`write_pipelined`, `array_ref` — each gated on the held region. Nothing new to learn; the lock only
decides *when* they are legal.

### Vectorized here, per-word in Stage A — and the rule that decides it

`RfShotBufLoad.run_iter` deliberately does the opposite, and its docstring says why: a pysim slave
dequeues **a whole burst per `get`** and truncation *discards* the rest, so *"a single 256-word burst
would be one pysim firing against 256 RTL firings and the two backends would be running different
designs."*

Both are right, because the granularities differ:

| | input | C++ firing consumes | pysim must |
|---|---|---|---|
| `RfShotBufLoad` | `pay`, an unframed word channel | one word | `get(nwords_max=1)` per word |
| `ShotTxLoad` | `s_in`, a **framed** boundary port | a whole shot (`take_shot` runs `NW` times inside one firing) | `get_pipelined(count=nword)` once |

**The rule: match the pysim read granularity to the C++ *task firing*, never to the word.** Vectorize
where one firing consumes a frame; loop where one firing consumes a word. Getting this backwards is
not a performance question — it makes the twins two different designs, which is what
`examples/bram_access` was written to spell out.

A consequence for the scenario bundles: `get_pipelined` requires the payload to arrive as **one
burst**, which for a framed port is exactly one `TLAST`-delimited frame. That is already how
`ShotTxLoad`'s scenarios are written, so nothing changes — but a gate that split the payload across
bursts would silently under-read.

### Python — the owner (player)

```python
class ShotTxPlay(FreeRunMod):
    def run_iter(self):
        while True:
            n = min(self.check_period, self._remaining())
            if self.state is PLAY_MEM:
                # Same discipline as the requester: one chunk per firing, anchored.
                x, t0 = yield from self.lock.read_pipelined(self.element_type, n, addr=self.rd)
                yield from self.samp_out.write_pipelined(x, t_out_start=t0)
                self.rd += n
            else:
                yield from self._play_filler(n)
            cmd = self.lock.poll_nb()                  # exactly once per check_period
            if cmd is None:
                continue
            if cmd.opcode == LOCK_ACQUIRE:
                self.state = PLAY_FILLER               # STOP TOUCHING IT, then grant
                yield from self.lock.grant(cmd.start_addr, cmd.end_addr)
            else:
                self.state = PLAY_MEM
```

**The `PLAY_FILLER` transition must precede the grant.** Granting while still in `PLAY_MEM` lets the
requester write memory the owner is reading — precisely the collision. This is the one ordering the
whole protocol turns on.

### HLS — the owner's poll

The check sits **outside** the pipelined inner loop, once per `check_period` elements, so `II=1` on
the datapath is untouched:

```c
play_set:
    for (;;) {
    play_chunk:
        for (int i = 0; i < CHECK_PERIOD; i++) {
#pragma HLS PIPELINE II=1
            samp_out.write(state == PLAY_MEM ? buf[rd++] : FILLER);
        }
        MemLockCmd c;
        streamutils::tlast_status tl;
        if (c.read_nb_axi4_stream<W>(cmd_in, tl)) {
            if (c.opcode == LOCK_ACQUIRE) {
                state = PLAY_FILLER;                 // before the grant, always
                MemLockResp r;
                r.status = LOCK_GRANTED;
                r.start_addr = c.start_addr;
                r.end_addr = c.end_addr;
                r.write_axi4_stream<W>(resp_out);
            } else {
                state = PLAY_MEM;
            }
        }
    }
```

`state` is a `static` carried across firings, and this body **reads before it writes** only if the
poll is non-blocking — which it is. If that ever changes, `reference-hls-task-reset-trap` applies:
an `hls::task` that writes before it reads advances during reset.

### HLS — the requester

```c
    MemLockCmd c;
    c.opcode = LOCK_ACQUIRE;
    c.start_addr = base;
    c.end_addr = base + NW;
    c.write_axi4_stream<W>(cmd_out);

    MemLockResp r;
    streamutils::tlast_status tl;
    r.read_axi4_stream<W>(resp_in, tl);              // BLOCKING, bounded by CHECK_PERIOD

load_shot:
    for (int i = 0; i < NW; i++) {
#pragma HLS PIPELINE II=1
        buf[base + i] = s_in.read().data;
    }

    c.opcode = LOCK_RELEASE;
    c.write_axi4_stream<W>(cmd_out);
```

---

## Traps

**Dynamic base addressing is the silent-failure class this repo has already paid for once.** The
byte-versus-word bug had every BRAM design mis-addressed, and `bram_toy` stayed green through it
because *the scaling was consistent* — a design round-trips perfectly right up to the point its
memory wraps. `base + offset` has the same shape. **The gate must include a region that wraps**, or
it is measuring nothing.

**`bram_t2p.v`'s `$error` is one-sided.** It asserts *A writes while B touches the same address*, and
`bram.py:775-781` refuses a writing port B for exactly that reason, recording the cost of lifting it:
*"one line, but it changes every example's copied `xsi/bram_t2p.v`."* S1 keeps the writer on port A
and does not touch this. S2 must decide it deliberately.

**XSI discards `$error`.** The RTL gate is a VCD scan (`find_read_during_write`), and it is only
meaningful **paired with a dirty run** that is known to collide — otherwise a scan that finds nothing
is indistinguishable from a scan that ran on the wrong nets.

**A grant is not a fence at RTL.** At S1 the owner yields the whole memory, so nothing in hardware
stops the requester from touching an address outside its region. pysim catches it; the RTL does not.
Say so on the page rather than implying the range is enforced.

**Cycle counts move.** `RfShotTx` gains channels and a state machine, so `rf_shot_play`'s gates
(292 / 76, 192 words, 2 startup blocks) are all re-measurements, not inheritances.

## Stages

### S1 — the interface and its first consumer

`waveflow/hw/locked_mem.py`, the two schemas, codegen lowering, and **infinite play on `RfShotTx`**
in the same arc. One region, one requester, writer on port A.

**Gate:** load a shot, play it indefinitely, load a second shot mid-play, and assert the output
switches waveform with filler in between and no collision on a VCD scan paired with a dirty run.
Plus a region that wraps.

### S2 — RX, where two regions become correctness

Stage C's capture as the second consumer. This is where `[start, end)` earns its generality: the
writer fills `[256, 512)` while the reader drains `[0, 256)` and **nothing is dropped**.

It also needs what TX does not: **a drop counter and a verdict.** You cannot back-pressure an ADC, so
a reader holding a lock the writer needs means lost samples — and lost samples are silent in pysim
in exactly the way sub-block loss already was. The count is the design's to produce and the gate's to
assert; the interface does not supply it.

#### Enable-gating is CLOSED, and not for the reason you would guess

**Do not re-attempt making the owner's memory port go quiet from C++.** It was tried at S1, it is in
the shipped code, and it does not work.

`shot_loop_play_task.h` already guards its read with a **register**, not an address comparison:

```c
static ap_uint<1> playing = 0;
...
samp_out.write(playing ? buf[BASE + rd + i] : (ap_uint<W>)SHOT_LOOP_FILLER);
```

`playing` changes once per handover and never per beat, so there is nothing data-dependent in the
loop's trip count and **II=1 survives** — measured, not assumed. The pipelining cost everyone expects
here is *not paid*, and the guard still fails: Vitis evaluates `buf[...]` unconditionally and muxes
the filler in, so the read port stays enabled for the whole 34-cycle overlap. The positive control
and the shipped design are identical on this net.

So the finding is stronger than *"a per-access range check would break the pipeline"*:

> **The cheapest conceivable guard — one register bit, no address arithmetic, no II cost — does not
> produce a quiet memory port.** The obstacle is not pipelining. Vitis owns the port enable and
> hoists the read out of the ternary.

Two consequences for S2:

1. **Disjoint regions are not merely the cheap option; they are the only one that does not fight the
   tool.** If the writer and reader never share addresses, both ports staying live is *irrelevant*
   rather than tolerated, and no enable needs gating.
2. The two alternatives stay open but cost more than they look. **Gating `EN` in the wrapper** is the
   natural place (the wrapper already owns the memory-port wiring — it is where the `>> 3` lives),
   but the kernel must then *export* the holding bit as a port, which HLS makes awkward; it is a
   `wrapper_gen` + codegen change, bounded but real. **A sticky `collision` output on the memory**
   prevents nothing — it makes a violation *visible in both backends*, replacing a VCD scan that has
   now twice failed to separate a clean design from a deliberately dirty one. That one is worth doing
   on its own merits and belongs to `plans/rtl_module.md`, not here.

#### One thing S2 must decide before it builds anything

`plans/rf_shot_buf.md` Stage C wants **pre-trigger history**: fill circularly, stop on a trigger, read
out a window that begins *before* the trigger fired. A circular fill spans the whole memory, which is
in tension with a disjoint-region split, and the resolution changes what S2 is:

* **Triggered one-shot** — write circularly, trigger, *stop writing*, hand the reader the whole
  memory. Writer and reader are never live together, so this is S1's degenerate case (`region = all`)
  and needs no second region at all. Pre-trigger history works because the writer already wrote it.
  Samples arriving during read-out are lost, which for a one-shot capture is correct behaviour, not a
  defect — but the drop counter must say so.
* **Continuous capture** — writer fills `[256, 512)` while the reader drains `[0, 256)`, swap, repeat.
  Nothing is dropped, and this is what the region parameter was built for. But a window is then at
  most one region long and **cannot start before the region boundary**, so pre-trigger history is
  bounded by the region rather than by the memory.

These are different products. Pick one deliberately and say which on the page; do not discover the
tension mid-build.

**DECIDED 2026-09-01: continuous capture.** S2 builds the two-region variant, because it is the one
that exercises `[start, end)` at all — the triggered one-shot is `region = all` and would leave the
interface exactly where S1 left it, unverified in the only dimension S2 exists to verify. The cost is
accepted: a capture window is bounded by the region, so pre-trigger history cannot reach past the
region boundary. Full-depth history stays available later as the `region = all` case with a trigger
and a stop, reusing this stage's drop counter.

**Check this before building anything.** S1's requester already acquires an arbitrary
`[start, end)` while the owner holds the complement — which *is* two disjoint regions. It is
plausible that `locked_mem.py` needs **no change at all** and S2 is a consumer plus its gates. Verify
that first; if true, say so and do not manufacture interface work to fill the stage.

### S3 — many regions, many requesters

Only if something demands it. Needs an allocator and a deadlock argument, and at that point this
stops being an interface and starts being arbitration.

## Not in scope

- Replacing or modifying `StreamOfBlocksIF`.
- Making `bram_t2p.v`'s assertion symmetric (S2's decision).
- A central arbiter. The lock is peer-to-peer between one owner and one requester.
- `RfStreamBuf`. `CreditStreamIF` is still waiting for that consumer, and this plan does not touch it.

---

## S1 as built — decisions taken without the plan, and where they landed

Written during the S1 build (2026-09-01).  Everything here was a question the sections above do not
answer; each was decided from what their reasoning implies, and each is recorded rather than left in
the code for someone to re-derive.

### Checkpoint 1 — `waveflow/hw/locked_mem.py`

**`addr_bits` is 28, and the messages are 64 bits exactly.**  The plan writes the field width as
`addr_bits` without a number.  Taking it from the *memory* would mean the wire format changes when a
memory does, which is the coupling `IDX_BW` exists to prevent next door — so it is a constant, and
28 is the constant that makes `8 + 28 + 28` exactly **64**.  That is the width every design in this
arc already speaks, so *"two schemas, one word each at 64 bits"* becomes structural rather than a
coincidence that happens to hold.  `lock_bitwidth()` refuses anything that does not pack to one beat,
because the owner's poll is a **non-blocking read** and half a command is not a command.

**The lock channels are built at the schema's own width, not the memory's word width.**
`AckedStreamIF`'s precedent (`status_bitwidth`): a channel whose width can disagree with what travels
on it is a disagreement waiting to be found at the wrap.  A consequence worth stating: a 32-bit
design still gets 64-bit lock FIFOs, which is two beats of storage and no beats of traffic.

**A seam-spanning interface declares its wrapper wires, and `add_if` files them.**  This was the one
structural problem the plan does not mention.  `LockedT2pMemIF` holds four channels and they do *not*
lower the same way: the two `StreamIF`s are internal edges, and the two `BramIF`s are **wrapper
wires** whose kernel-side ends must stay boundary ports.  `derive_internal_edges` walks
`physical_interfaces()` and `derive_boundary` derives "internal" from the same walk — so a `BramIF`
returned there would make the memory ports vanish into a FIFO that does not exist.

The answer is a second hook, `Interface.rtl_interfaces()` (default `[]`), which
`HwModule.add_if` sweeps into the `add_rtl_if` registry.  That keeps the plan's promise that *"the two
`bind` calls are the whole wiring"* — a composite registers the lock once and both halves land where
they belong — and nothing that was already lowering changes, because the default is empty.

**`check_period` is asserted in SECONDS, not cycles.**  `check_period` counts the owner's *own work*,
and what one element of that work costs is the owner's business: a player paced by a DAC spends a
converter word-time per element, not a fabric cycle.  The interface refuses to invent that rate, so
`assert_grant_bounded(max_seconds)` takes the product from the gate, which knows it.

**`handle_nb()` exists beside `poll_nb()`.**  A `RELEASE` needs no decision and no answer, so handling
it inside the endpoint keeps a design's state machine down to the case that *does* need one.  An
`ACQUIRE` is returned untouched on purpose: granting it is the design's call, and it must come after
the design has switched away from the region.

**"A region that wraps" means a region whose last element is the memory's last element.**  A region
that literally wraps modulo the depth is `start > end`, which the plan already answers with
`LOCK_BAD_RANGE` — so it cannot be what the trap is about.  What the byte-versus-word bug actually
was is `base + offset` staying *consistent* right up to the top of the address space, so the gate is
`[nelem - n, nelem)`: the base is non-zero, the last element is the memory's last, and a wrongly
scaled base runs off the end instead of aliasing quietly.  `test_a_region_at_the_TOP_of_the_memory_round_trips`.

**Both dirty runs are in the pysim gate, by name.**  `test_the_requester_touching_ONE_element_outside_its_region_raises`
and `test_the_owner_that_grants_BEFORE_it_stops_reading_raises`.  The second is the ordering
everything turns on, and it fails because `grant()` takes the region out of the owner's hands *before*
the response goes on the wire — so the owner's very next read is the failure rather than a plausible
number.

### Checkpoint 2 — the lowering

**`derive_boundary` had to expand `physical_interfaces()` too, not only `physical_endpoints()`.**
This was a real defect the plan does not predict, and it is worth stating because it is the general
form of the S1 structural problem.  `derive_boundary` builds its *internal* set by walking each
registered interface's own `endpoints` and expanding those — which is right for `AckedStreamIF`,
where every channel lowers the same way, and wrong the moment an interface's channels do **not**.
`LockedT2pMemIF`'s endpoints expand to `(mem, cmd, resp)`, so all three landed in *internal* and the
memory ports vanished from the boundary: a kernel with no way to reach its memory, and no error until
the wrapper had nothing to join.  The fix is one line — expand the interface first, then read the
sub-interfaces' endpoints — and it is exactly what `derive_internal_edges` already did.

**`mem_lock.h` is framework and the toy's two bodies are not.**  The three moves a lock-aware body
needs (request, await, poll+grant) ship from `waveflow/build/` through `MemLockStep`, so no body
hand-rolls a beat and both ends read the layout through the generated schema headers.  A step of its
own rather than a line in `RfShotBufStep`: the lock is a primitive and S2's RX consumer will reach for
the same header, and filing a general mechanism under its first user is the shape `CreditStreamIF` is
still paying for.

**`MEM_LOCK_W` is a `#define`, not a template parameter.**  The channel width is the schema's and the
schema is fixed, so a body that took it as a parameter would be advertising a freedom that does not
exist — and the two ends could then be instantiated at different widths.

**The minimal consumer is a test fixture (`tests/hw/lock_toy/`), not an example.**  What is on trial
is the lowering; an example teaches a design, and this one teaches nothing a user wants.  Building a
teaching example ahead of the first real consumer would be the un-consumed-abstraction mistake this
plan opens by refusing.

**The owner inherits the reset trap and the requester does not.**  `reference-hls-task-reset-trap`
says a task that WRITES before it READS advances during reset.  An owner *cannot* avoid that shape —
writing without being asked is what "the side that cannot stop" means — so every owner needs
`#pragma HLS reset` on its statics **and** `config_rtl -reset state` in the solution tcl, which is
what actually closed it under Vitis 2025.1 in `rf_repeat_play`.  A requester opens with a blocking
read and is on the safe side.  This is an obligation the interface imposes on one of its two users
and not the other, and it belongs in the docs page when one is written.

**Measured (Vitis HLS 2025.1, xczu48dr, 4 ns):** both pipelined loops reach **II=1** — the
requester's `store_shot` (payload beat in, memory beat out, one pipeline, no local copy) and the
owner's `play_chunk` (`buf[rd + i]` at a running static base).  The loop names are *discovered* from
the report by label rather than spelled, because a spelled name stops matching on a comment edit and
a gate that skipped on a miss would read as a pass.

### Checkpoint 3 — infinite play, as a SIBLING of `RfShotTx` rather than a mode of it

**`waveflow/hw/rf_shot_loop.py`: `ShotLoopLoad`, `ShotLoopPlay`, `RfShotTxLoop`.**  The plan says
*"infinite play on `RfShotTx`"* and this does not modify `RfShotTx`.  The reason is that the two
designs differ in what the player **is**: a finite player consumes a token, plays, and reports done,
so the loader owning the memory is safe by construction; an infinite player never stops, so the
memory has to be handed over.  That is a different task, a different loader and a different channel
set — and `examples/rf_shot_play`'s measured gates (292 / 76, 192 words, 2 startup blocks) belong to
the design that produced them.  A sibling keeps them valid and makes the loop design's numbers its
own, exactly as `rf_shot_play` and `rf_repeat_play` are two designs answering one user story.

**The flag is an opcode, `SHOT_LOOP = 2`, not a bit on `nrepeat`.**  `nrepeat == 0` already means
something — it is what the loader sends for a shot it refused to call playable — so overloading it
would put *play forever* and *never play* on the same value.  A `SHOT_LOAD` arriving at the loop
design is **refused** (`SHOT_WRONG_LEN`), never reinterpreted: a command answered as something other
than what it asked for is invisible, because the samples look perfect.

**`SHOT_BUSY` is unreachable, and that is the whole capability.**  Under infinite play a design that
answered `BUSY` would refuse every load forever.

**Five tasks became three.**  No `rdy` token, no `rep` channel, no `done` token, no `ShotPhase`, and
no `RfShotBufLoad` / `RfShotBufRead` pair: the loader writes the memory and the player reads it.
What replaced all of it is two lock streams and one ordering rule.

**The re-layout is LAST, and that moved a modelling field with it.**  The memory holds *dense* words,
so the conversion to converter slots happens after the player — which makes the re-layout the stage
the converter back-pressures.  pysim's quantum on that edge is a block, so `_RelayoutTask` gained
`blk_words`, the same sim-only field `ShotTxPlay` already carries and for the same reason.  **The
accommodation follows the port, not the class.**  Default 1: nothing that was already running moves.

**A known twin divergence, recorded rather than hidden.**  The C++ loader acquires and *then* reads
the payload beat by beat, because `buf[lo + i] = s_in.read()` at II=1 is one pipeline and there is no
way to have it without owning the region first.  The pysim twin cannot: a pysim slave takes a whole
burst per `get`, so the frame is in hand before there is anything to decide.  **The RTL holds the
region for the whole transfer and pysim holds it for the write**, so the handover gap differs between
the backends.  Both are measured; neither is inherited.

**A short load CLOBBERS, and that is the first thing S2's second region fixes.**  With one region
there is nowhere else to put an arriving waveform, so a transfer that ends early overwrites what was
playing and the design plays the padded result.  `SHOT_SHORT` going back to the host is the whole
warning there is.  `RfShotTx` can do better only because it has a phase in which the memory is idle.

**MEASURED (pysim, 40 us at a 4 Mword/s DAC, `nword=16`, `blk_words=4`, `depth=64`):**

| | value |
|---|---|
| blocks to the converter | **40** (4 words each; **no short burst, ever**) |
| filler blocks | **3** — two at startup, **one** for the handover |
| words out of the memory | **148** |
| grants / releases | **2 / 2** |
| grant wait | **238** and **221** fabric cycles at 250 MHz = **0.95 / 0.88 us** |
| verdicts | `[(0, SHOT_LOADED, 64), (1, SHOT_LOADED, 64)]` |

Identical for `base = 0` and `base = 48` (the region whose last element is the memory's last).

**The grant wait confirms checkpoint 1's "seconds, not cycles" decision.**  `check_period` is 4
*elements*, and the wait is ~238 fabric cycles — because this owner is paced by a DAC, so one element
of its own work is a converter word-time and not a fabric cycle.  A bound expressed in cycles would
have been wrong by two orders of magnitude, in the direction that passes.

**Both dirty runs are in the gate by name.**  `test_a_player_that_grants_and_keeps_reading_raises`
and `test_a_loader_writing_past_its_region_raises`.  The first one taught something the plan's
wording does not quite say: once a body is one chunk per firing, *"granting while still in
PLAY_MEM"* is not about instruction order **inside** the branch — a grant followed immediately by
the state change is harmless, because no read happens in between.  The hazard is the state change
**never happening**, so the next chunk reads a region the requester now owns.  The dirty player is
the shipped one with that line missing.

### Checkpoint 4 — the RTL gate, and the defect it found before it could run

**A REAL DEADLOCK, MEASURED AT RTL, AND IT IS THE INTERFACE'S AND NOT THE DESIGN'S.**

The first XSI run of `rf_shot_tx_loop` produced 359 words of filler, consumed **two** words of the
command stream and answered **no** header at all.  The full-depth VCD says why: the loader's
`ap_CS_fsm` sits in state 1 for the whole run, `lock_if_resp_blk_n` goes low at cycle 30, and
`lock_if_cmd_write` **never asserts once**.

The cause is the shape of the requester's round trip, written the obvious way:

```c
    mem_lock_request(cmd_out, LOCK_ACQUIRE, BASE, BASE + NW);   // a write
    ap_uint<8> st = mem_lock_await(resp_in, lo, hi);            // a BLOCKING read
```

Two operations, two **different** streams, and **no data dependency between them**.  Vitis is
therefore free to schedule both in one state — and it does.  That state then stalls on the empty
response FIFO, and a stalled state performs none of its writes, so the request is never sent and
nothing will ever fill the FIFO it is waiting on.

The fix is in `mem_lock.h` and it is one loop: `mem_lock_await` polls with `read_nb` instead of
blocking.  A loop is a scheduling barrier — the request is committed in the state before it, and the
poll cannot be hoisted above a write it is control-dependent on.

**The owner has no such hazard and must not copy the fix.**  Its write is guarded by its read's
result (`if (mem_lock_poll(...)) { ... grant ... }`), so the two really are data-dependent and Vitis
cannot reorder them.  The asymmetry is worth stating: *a request/response round trip across two
streams needs an explicit ordering barrier; a poll-then-answer does not.*

**This is exactly the receipt the plan opened by asking for.**  `CreditStreamIF` is fully written and
documented and has never had a consumer, so nothing has ever tried to satisfy its contract.  This
defect is invisible in pysim (SimPy does not reorder), invisible in `check()`, invisible in `csynth`
(it synthesizes cleanly and reports II=1 on every loop), and invisible in the C++ read: it appears
only when real RTL runs.  An interface built ahead of its consumer would have shipped with it.

#### The gate itself — `examples/rf_shot_loop` and `tests/examples/test_rf_shot_loop_xsi.py`

**One scenario, not two, and that is the capability made structural.**  `rf_shot_play` needs two
because once a shot is accepted its buffer is busy and at most one load per stream can succeed.  That
constraint is exactly what the lock removes, so a single file-driven stream reaches every verdict
**and** both loads:

| tid | frame | verdict |
|---|---|---|
| 0 | a whole shot | `SHOT_LOADED` -> plays |
| 1 | `nsamp` the design was not built for | `SHOT_WRONG_LEN` |
| 2 | `nsamp == 0`, no payload | `SHOT_ZERO_LEN` |
| 3 | a `SHOT_LOAD` — a *finite* play | `SHOT_WRONG_LEN` |
| 4 | a second whole shot, mid-play | `SHOT_LOADED` -> **the waveform switches** |
| 5 | `SHOT_END` — the fence | `SHOT_LOADED` |

`tid` 1 and 2 sit between the two loads deliberately: their payloads have to be drained, which buys
the first waveform airtime on the converter before the second arrives.

**The region is `[192, 256)` — the top of the memory**, which is *"a region that wraps"* in the only
sense the plan can mean (see checkpoint 1).  The gate reads the write port's own addresses off the
waveform and asserts the writer touched exactly `192..255`: `base + offset` is the shape of the
byte-versus-word bug, and a design that round-trips correctly can still be addressing the wrong
elements.

### MEASURED at RTL (Vitis HLS 2025.1, xczu48dr, 4 ns; xsim 2025.1; 1400 cycles)

| | value |
|---|---|
| verdicts | all six, in order, **identical to the pysim golden** |
| playout | `[filler 3 blk][A 2 blk][filler 2 blk][B 15 blk]` — **the handover is two blocks** |
| bit-exactness | waveform A and B both exact against the loaded ramp, in converter codes |
| words to the DAC | **359** at 0.256 words/cycle |
| `DAC_BLOCKS_ZERO_FILLED` | **0** — the handover's silence is real beats, so the DAC is never starved |
| `DAC_UNDERRUN` | **1**, at cycle **4** — before the pipeline has produced anything |
| command stream | **262 / 262** words consumed |
| last verdict | cycle **374** |
| write addresses touched | exactly `192..255` |
| achieved II | **1** on `take_shot`, `drain_tail`, `await_grant`, `play_chunk`, and the re-layout |
| estimated Fmax | 367.34 MHz |

The pysim run of the same scenario gives the same segments (`3 / 2 / 2 / rest`) and the same six
verdicts, which is the twin comparison this arc exists to make.

### The positive control, and the third thing it taught

`RfShotTxLoopDirty` is the shipped composite reached through a new `player_cls` ClassVar, with the
player's `playing = 0` removed — **one line**, so a difference is attributable to that line.  It gets
its own top, wrapper, `$dumpvars` module, hazard manifest, Vitis project **and its own generated
harness**, the last because a harness hardcodes `DESIGN_DLL`: a control driven by the shipped design's
harness runs the shipped design, reports its numbers and finds no hazard.  That was hit on the way,
and it is the single most convincing way for this gate to lie.

**Then the cycle-exact predicate refused to distinguish them, and the reason is the plan's own.**
MEASURED: the control puts both memory ports live on the region for **34 cycles** and
`find_read_during_write` returns nothing — the reader is DAC-paced at one word in four while the
writer runs at II=1, so the sweeps cross inside the reader's idle window (the diff steps
`-1, -1, 5, 5, 11, 11, …` past zero).  That is `examples/bram_access`'s finding restated: *address
overlap alone is not a collision*.  Aligning the two on purpose was tried and did not help; the phase
is deterministic.

**And the shipped design shows the same 34 cycles.**  Vitis reads the BRAM *unconditionally* at II=1
and muxes in the filler, so the read port stays enabled while the player is yielded.  So:

> **At S1 the region is enforced in pysim and NOT at RTL.**  What the lock buys in hardware is that
> the data is not **used**; the memory still sees both ports live.

The plan says exactly this under *A grant is not a fence at RTL* — this is its concrete, measured
form, and it is worth reading before S2 makes the region load-bearing.

So the pairing is made **on the samples**, which is where the difference is real and visible:

| | shipped | control |
|---|---|---|
| non-filler runs in the playout | 4 segments, 2 gaps | **1** — it never goes quiet |
| first sample | filler | **-1**, the uninitialised memory |
| `check_switched` | passes | **raises** — the old waveform is spliced into the new |
| both ports live on the region | 34 cycles | 34 cycles (*the same*, and that is the finding) |
| `find_read_during_write` | 0 | 0 (*the predicate cannot see it*) |

The gate asserts every row.  `find_read_during_write` stays, as the memory's own predicate and
nothing stronger.

**S2 has to decide this deliberately.**  If the region is to be correctness rather than
documentation, either the player's read must be *conditional* in the RTL (which costs the
speculative read the II=1 datapath is built on) or `bram_t2p.v`'s assertion has to become the
enforcement — and that is the same one-sided-`$error` edit S2 already owns.

---

## S2 as built — decisions taken without the plan, and where they landed

Written during the S2 build (2026-09-01), on branch `t2p-lock-chan-s2` off `plans-s2-scope`.

### Checkpoint 0 — the answer to "does the interface need to change at all?"

**Almost not. One change, and it is one line of routing plus its refusal.**

The plan's guess was that `locked_mem.py` might need nothing, because S1's requester already acquires
an arbitrary `[start, end)` while the owner holds the complement — which *is* two disjoint regions.
**That half of the guess is exactly right, and it is verified rather than asserted**: a full
ping-pong swap cycle runs on `acquire` / `grant` / `release` / `may_touch` **as S1 shipped them**, with
no change to the protocol, the messages, the region bookkeeping or the complement guard.
`test_two_disjoint_regions_swap_on_the_UNCHANGED_S1_protocol` is that run: three grants, the reader
alternating `[0,32) → [32,64) → [0,32)`, and the middle window coming back as the ramp the capture
wrote **while the reader held the other half**.

What S1 did get wrong is a different axis, and it was invisible until RX asked for it:

> **Role and direction are independent, and S1 coupled them.**
>
> `bind` routed *master → port A* and *slave → port B*. That is right for TX and **only** for TX,
> where the requester is the loader (writes) and the owner is the player (reads). RX inverts the
> **directions** while keeping the **roles**: the capture cannot stop, so it is still the owner — and
> it *writes*; the window reader still arrives with a transaction, so it is still the requester — and
> it *reads*. The RX pairing was therefore refused at bind, with a message about a memory port rather
> than about the design.

The fix is `_mem_if_for`: a side gets port A if it writes and port B if it reads, whichever role it
is. **Port A stays the writer's in both directions**, which is not a preference — `bram_t2p.v`'s
`$error` is one-sided (*A writes while B touches the same address*), so a writing port B would be
invisible to the memory's only real RTL check. TX's routing is bit-for-bit what it was; the S1
consumer's tests and gates are untouched.

It comes with a refusal it did not have: **two ends that want the same memory port**. Two writers is
the collision the one-sided `$error` cannot see; two readers is a lock arbitrating a memory nothing
ever fills — a design that looks correct and moves no data. Both are wiring mistakes, and bind is the
last place the message can still name the two declarations.

**So S2 is a consumer plus its gates, and one four-line routing fix.** The plan asked not to
manufacture interface work to fill the stage, and there was none to do.

### Checkpoint 1 — the RX pair, and the channel the lock does not provide

**`waveflow/hw/rf_pingpong_rx.py`: `PingPongCapture`, `PingPongWindow`, `RfPingPongRx`.**  The
capture is the owner and it **writes**; the window reader is the requester and it **reads** — the
inversion checkpoint 0 made bindable.

**There is a `rdy` channel, and its existence is a finding rather than an oversight.**

> **The lock arbitrates; it does not synchronise.**  It answers *may I touch these addresses* and has
> no way to say *there is something there worth touching*.

A reader that alternated blindly would acquire a half the capture had not filled and drain zeros —
the plausible-samples failure this arc keeps meeting.  So the capture announces each region as it
completes it, exactly as `RfShotBufLoad` announces a shot, and the reader blocks on the announcement
before it asks for anything.  One word, and the word is the region's **base address**: an index would
be a second encoding of a geometry the lock already speaks in addresses.

**Its depth is `N_REGION`, and that is an invariant rather than a guess.**  At most `N_REGION`
regions can be full at once, so at most that many announcements can be outstanding — which is what
lets the capture write the channel *blocking* and still never stall.  A shallower channel would make
the capture block on a reader, which is back-pressuring an ADC.

**A region is free when it is RELEASED, not when it is taken.**  The capture keeps a `full` flag per
region, set when it finishes filling one and cleared by the *release*.  If taking a region freed it,
the capture could refill the half a reader is still draining — the collision, arrived at from the
bookkeeping rather than from the lock.  This is also what makes the drop condition reachable at all:
when neither region is free, the block has nowhere to go.

**The capture takes its input first and unconditionally.**  A body that read only when it had room
would be back-pressuring an ADC, and it would make the drop counter read *zero* for a design that was
quietly losing everything upstream instead.  `test_the_capture_never_stalls_its_input` asserts every
presented block was taken, and that `written + dropped == presented`.

**On RX the ordering rule that TX turns on costs nothing — and the guard still has to be there.**
The reader only ever asks for a region the capture has already announced and moved off, so no state
change precedes the grant.  A design that drifted into granting the half it is filling would corrupt
a window silently, so `test_the_capture_never_grants_a_region_it_is_filling` drives that case
directly and the interface's own guard catches it.

#### MEASURED (pysim, 40 blocks of 4 words at 4 Mword/s, `depth=64`, two regions of 32)

| | clean | dirty (`stall_blocks=10`) |
|---|---|---|
| windows drained | **4** | 3 |
| window bases | `[0, 32, 0, 32]` | `[0, 32, 0]` |
| blocks taken off the input | **40** | **40** |
| words written | **160** | 128 |
| **words dropped** | **0** | **32** (8 blocks) |
| grants | 4 | 3 |
| gap visible between drained windows | **0** | **16** |

**The counter and the samples do not have to be the same number, and asserting that they were was
wrong.**  The first version of the dirty gate asserted equality and failed: 32 counted against 16
visible.  The samples can only show loss that fell *between two windows the reader actually drained*;
the counter also holds whatever was dropped after the last one, because the run ends with the reader
still holding a window and the capture goes on losing blocks it will never announce.  So the relation
is a **bound** — `0 < visible <= counted` — and every visible gap must be a whole number of blocks,
because the capture drops a block at a time.  Asserting equality would have been asserting that the
run stopped at a convenient moment.

### Checkpoint 2 — the count, and the verdict, on the wire

The plan is explicit that *"the count is the design's to produce and the gate's to assert; the
interface does not supply it"*, and that lost samples are silent in pysim in exactly the way sub-block
loss was.  Checkpoint 1 produced the count as a Python attribute, which is **invisible to a host and
invisible to the RTL** — the same shape the silence had.  This checkpoint puts it on the wire.

**Every window is now a frame: one `CaptureWindowHdr`, then the samples.**  8 + 28 + 28 = exactly 64,
one beat, the width everything in this arc speaks.  It is written **once**, by the capture, onto the
`rdy` channel, and the reader forwards it verbatim as the frame's header — one schema rather than
two, because it is one statement (*here is a region, here is what was lost before it*) and a second
schema would be a second place for the two to disagree.

**Two fields, because there are two different questions.**

* `n_dropped` — words lost **since reset**, cumulative.  `reverse_stream`'s rule 1: a lost cumulative
  value is harmless because the next carries the whole truth; a lost *increment* is wrong forever.
* `status` — `CAP_OK` / `CAP_LOST`, *was anything lost immediately before this window?*  Not
  derivable from one cumulative reading — a host would have to remember the last one and subtract —
  and the design already knows.  The same split `ShotTxResp` makes between a status and a count.

**The verdict marks the window AFTER the gap**, and that is asserted rather than assumed: the flagged
window is the one whose first sample does not follow the previous window's last.  Marking the window
*before* would point a host at data that is entirely fine.

**One frame, not two writes.**  The first version wrote the header and then the payload, which in
pysim is two bursts and therefore two `TLAST`s — measured as frames of `[1, 32, 1, 32, …]` at the
sink.  The C++ twin writes the header beat and the payload beats into a single `axi4s` frame, so a
split here would make the two backends disagree about the *boundary* rather than about a value, which
is the harder kind of divergence to see.  The header is serialized through the schema's own
serializer and concatenated; `split_windows()` is the one place the layout is read back, so a gate
never slices it by hand.

#### MEASURED (pysim, same run as checkpoint 1)

| | clean | dirty (`stall_blocks=10`) |
|---|---|---|
| frames at the host | `[33, 33, 33, 33]` | `[33, 33, 33]` |
| headers `(status, base, n_dropped)` | `(OK,0,0) (OK,32,0) (OK,0,0) (OK,32,0)` | `(OK,0,0) (OK,32,0) (**LOST**,0,16)` |
| internal counter | 0 | 32 |

**The published total is a lower bound on the counter, for checkpoint 1's reason.**  16 published
against 32 counted: the last window carries what was known at the last *announcement*, and the
capture goes on dropping after it, because the run ends with the reader still holding a window.  This
is the second time in one stage that an equality assertion had to become a bound, and both times the
cause was the same — **a counter records the whole run; the wire records what the design had a chance
to say.**

The one place equality *is* asserted is the clean run: every window reports `CAP_OK` with zero, and
the last published total must then equal the counter.  That is not the same check wearing a different
hat — it catches loss the host would **never be told about**, because it fell after the final
announcement.

### Checkpoint 3 — the lowering, and two asymmetries with TX that are worth the words

**Nothing in the lowering path needed changing.**  Checkpoint 0's routing fix was the whole of it:
`check()` is clean, the four boundary ports come out `samp_in / buf_w / buf_r / w_out`, the four
internal channels are `dense / rdy / lock_if_cmd / lock_if_resp`, and one `add_if(lock)` still files
the two `BramIF`s as wrapper wires.  The hazard manifest now names the **capture's** port as the
writer, which is the port map RX needs and the one S1 could not produce.

**The RX owner is on the SAFE side of the reset trap, and the TX owner is not.**
`reference-hls-task-reset-trap` says a task that WRITES before it READS advances during reset.
`shot_loop_play_task.h` cannot avoid that shape — a TX owner *produces* samples nobody asked for,
which is what "the side that cannot stop" means there.  An RX owner **consumes** them, so its first
act is a blocking stream read and it stalls at reset like any requester.  The statics still carry
`#pragma HLS reset`; what this design does not *depend* on is the solution-level
`config_rtl -reset state` that TX needed.  (It is set anyway, so the two builds differ in nothing a
reader would have to explain.)

**The input is read unconditionally and the memory write is what the guard covers.**

```c
    ap_uint<W> x = samp_in.read();      // UNCONDITIONAL
    if (have) buf[dst + i] = x;
```

That is the shape a drop needs: a body that read only when it had room would be back-pressuring an
ADC, and it would make `dropped` read **zero** for a design quietly losing everything one stage
upstream.  The destination is decided *before* the block arrives — it depends only on statics the
previous firing left behind — so there is no local copy and the store is one pipeline.

**The C++ tracks `held_r[]` itself; pysim asks the lock endpoint.**  The pysim twin uses
`may_touch()`, because there the endpoint is also the **guard** that raises on a violation.  There is
no guard at RTL, so the body keeps the same fact in its own register.  The two are twins in behaviour
and not in mechanism, which is written down rather than left to be noticed.

**The free-region scan runs downward on purpose.**  `find_region` iterates `NR-1 → 0` with `UNROLL`,
so the last assignment wins and the result is the *lowest* free region — which is what the pysim
twin's upward first-match returns.  An upward unrolled loop would take the *highest*, and at
`N_REGION == 2` that difference is invisible; on a three-region design the two backends would
ping-pong in opposite orders.  It costs nothing to agree, which is exactly why it is recorded.

#### MEASURED (Vitis HLS 2025.1, xczu48dr, 4 ns target)

| module | loop | achieved II |
|---|---|---|
| `pingpong_capture_task_64_512_2_16` | `store_block` | **1** |
| `pingpong_window_task_64_512_2_16` | `drain_window` | **1** |
| `pingpong_window_task_64_512_2_16` | `await_grant` | **1** |
| `rf_relayout_to_dense_task_64_4_2_s` | *(Stage A's, unlabelled)* | **1** |

Estimated period **2.722 ns**, Fmax **367.4 MHz** — the same number the TX loop design measured, which
is what one expects when the datapath is a BRAM port and a FIFO either way.

**`store_block` is the interesting one.**  It reads unconditionally and writes conditionally, which
is the shape that makes a drop possible without stalling an ADC — and it is exactly the shape one
might expect to cost a cycle.  It does not.

### Checkpoint 4 — the RTL gate, and the claim two regions can make that one cannot

`examples/rf_pingpong_rx` and `tests/examples/test_rf_pingpong_rx_xsi.py`.  A real ADC filling the
memory, the ping-pong receiver, one sink taking windows.  **There is no command stream**, and that is
the design rather than an omission: a capture is asked nothing — it is *told* when a region is ready
by the design itself, and it answers on every window with a header a host can act on.  Compare
`examples/rf_samp_buf_rx`, whose whole middle is a command layer because *its* reader has to say which
window it wants.

#### MEASURED at RTL (Vitis HLS 2025.1, xczu48dr, 4 ns; xsim 2025.1; 2800 cycles)

| | value |
|---|---|
| windows to the host | **4 frames of 129 words** — one header, 128 samples |
| headers | `(CAP_OK, 0, 0) (CAP_OK, 128, 0) (CAP_OK, 0, 0) (CAP_OK, 128, 0)` |
| samples | **2048, contiguous**, codes `1000 … 3047` |
| words the converter handed over | **640**, in 40 blocks |
| words the converter could **not** hand over | **0** |
| host words / last window | **516** / cycle **2205** |
| achieved II | **1** on `store_block`, `drain_window`, `await_grant`, and the re-layout |

**Bit-identical to the pysim golden** — same four frames, same four headers, same 2048 contiguous
codes.  That is the twin comparison this arc exists to make, and on RX it is unusually strong,
because contiguity is a property of the *whole run* rather than of any one window.

#### S2's RTL claim is a POSITIVE one, and S1's could not be

S1 could only report an absence — the cycle-exact scan found nothing — and **its own positive control
found nothing either**: with one region the shipped design and the deliberately broken one both had
34 cycles of both-ports-live, and `find_read_during_write` separated neither.  Two disjoint regions
change the shape of the evidence entirely.  MEASURED on this run's VCD:

| | value |
|---|---|
| cycles with **both** memory ports live (`en && we` / `en`) | **140** |
| of those, cycles with the writer and reader in the **same region** | **0** |
| of those, cycles at the **same address** | **0** |
| regions each port visited over those cycles | writer `[70, 70]`, reader `[70, 70]` |
| `find_read_during_write` | 0 |

The 140 is what makes the 0 mean something: a run where the two were never live together would prove
nothing at all.  And both ports visit *both* regions in equal measure, so it is not one side sitting
still either.

> **This is the consequence the plan predicted under *Enable-gating is CLOSED*, now measured:** *"If
> the writer and reader never share addresses, both ports staying live is irrelevant rather than
> tolerated, and no enable needs gating."*  Vitis still owns the port enable and still reads
> speculatively.  With two regions that stops mattering, and the region becomes **enforced at RTL by
> construction** rather than by an assertion nobody can hear.

**S2 therefore answers the question S1 left open** — *"if the region is to be correctness rather than
documentation, either the player's read must be conditional or `bram_t2p.v`'s assertion has to become
the enforcement"* — with a third option the plan itself named and did not cost out: **neither, if the
regions are disjoint.**

#### No dirty RTL build, and why that is not the S1 situation

The fault-injection knob is `stall_blocks`, and it is a **pysim modelling field**: it reaches no
template argument, because *a reader that dawdles* is not something the RTL can be asked to do.  A
control would need a second design, as S1's did.

It is not needed here, and the reason is the one above: S1 needed a control because its claim was an
*absence*, and an absence can be produced by a scan bound to the wrong nets.  S2's claim is a
positive, self-witnessing measurement — 140 cycles of simultaneous liveness, 0 of them shared — which
cannot be produced by a scan that is looking at nothing.  The clean/dirty pairing lives in pysim,
where the knob does (`tests/hw/test_rf_pingpong_rx.py`), and the RTL gate says so on its own page.

**`WANT_XSI_GATES` 86 → 95.**
