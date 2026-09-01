# `LockedT2pMemIF` — a lock channel over a shared true-dual-port memory

**Status: SPECIFIED HERE, NOTHING BUILT.** Started 2026-09-01. Owns the shared-memory handover
primitive and its first consumer (infinite play on `RfShotTx`). Does **not** own `RfShotBuf` Stage C,
which is `plans/rf_shot_buf.md`'s — this plan exists to give Stage C an interface it can reach for.

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
