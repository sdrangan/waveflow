---
title: Credit Stream
parent: Derived interfaces
grand_parent: Interfaces
nav_order: 4
audience: python
snippets: run
api: [CreditStreamIF, CreditStreamMasterIF, CreditStreamSlaveIF, udiff, CTR_BITS, RESP_WORDS]
summary: "A forward stream plus a reverse stream carrying cumulative words consumed, so a producer that cannot be stalled can ask about room BEFORE it commits. Covers the avail = depth - resp_words - outstanding accounting, the reserved response headroom, the four rules the reverse path is built on (cumulative not incremental, non-blocking both ways, a bounded poll, and what a saturated reverse channel actually does), and the masked counter arithmetic."
---

# Credit Stream

A `CreditStreamIF` is a forward [stream](../primitive/stream.md) plus a **second stream running the
other way**, carrying the total number of words the consumer has consumed. It answers one question,
and the question arrives *before* the send:

> **May I send? Is there room?**

The producer can therefore issue a write it *knows* will not stall.

## When you need one, and when you do not

**A FIFO already implements credit.** `TREADY` *is* credit — delivered implicitly, one unit at a
time, at the moment of use. An explicit credit channel is nothing but **back-pressure moved earlier
and in bulk**.

So you only want one when "at the moment of use" is too late: when the producer commits to a
multi-word transaction it **cannot abandon partway**. A data converter is the motivating case — it
presents samples whether or not the fabric is ready, so discovering halfway through a burst that
there is no room is not a situation it can be in.

If your producer can simply block, it should: use a plain `StreamIF` and let `write` stall. Credit
buys nothing there and costs a second channel.

{: .note }
> The other reverse channel answers the opposite question. [Acked Stream](./acked_stream.md) reports
> *what became of what you sent*, which arrives **after** the send and which only the consumer can
> know. They are not two flavours of one mechanism — see the comparison on
> [Derived interfaces](./).

## Why this is a derived interface

Not a judgement call — [the tier test](../) is `boundary_kind`, and it is checkable:

```python
from waveflow.build.composite_gen import kind_of_endpoint
from waveflow.hw.reverse_stream import CreditStreamMasterIF
from waveflow.simulation.simulation import Simulation

probe = CreditStreamMasterIF(name="probe", sim=Simulation(), bitwidth=32)

print("declares boundary_kind:", hasattr(CreditStreamMasterIF, "boundary_kind"))
try:
    kind_of_endpoint(probe)
except Exception as exc:
    print("kind_of_endpoint:", type(exc).__name__)
print("decomposes into:", [(e.name, type(e).boundary_kind) for e in probe.physical_endpoints()])
```

```text
declares boundary_kind: False
kind_of_endpoint: LoweringError
decomposes into: [('probe_fwd', 'axis_out'), ('probe_crd', 'axis_in')]
```

The endpoint itself has **no** boundary kind, so it is not a primitive. What it has is
`physical_endpoints()`, which hands back the two ordinary stream endpoints it is made of — and
*those* have kinds. That is what "derived" means here, and it is also exactly how the interface
lowers: see [How it lowers](#how-it-lowers).

## Building one

The producer holds a forward **master** and a credit **slave**; the consumer holds the mirror. Both
sub-channels are built by the interface, and so is the wiring:

```python
import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.reverse_stream import CreditStreamIF, CreditStreamSlaveIF

sim = Simulation()
producer = CreditStreamMasterIF(name="producer", sim=sim, bitwidth=32)
consumer = CreditStreamSlaveIF(name="consumer", sim=sim, bitwidth=32)
chan = CreditStreamIF(name="chan", sim=sim, clk=Clock(freq=250e6),
                      bitwidth=32, depth=8, credit_depth=4)
chan.bind("master", producer)
chan.bind("slave", consumer)

print("forward depth:", producer.depth)
print("avail at rest:", producer.avail)
print("credit master is the data slave:", chan.crd_if.endpoints["master"] is consumer.crd_ep)
```

```text
forward depth: 8
avail at rest: 7
credit master is the data slave: True
```

**The reverse channel's master is the data slave**, and `bind` does that wiring rather than the
caller. Getting it backwards is the one mistake that would present as a *hang* rather than an error,
so it is not left to a call site.

`depth` may **not** be `None` on a `CreditStreamIF`, unlike a plain `StreamIF`. Credit is
`depth - outstanding`, and an unbounded queue has no depth to subtract from — a credit channel
without a depth is a credit channel without credit, and the constructor says so.

## The accounting

Three numbers, and one of them is reserved:

| | |
|---|---|
| `depth` | the forward FIFO's physical depth, read from the **interface** — there is no local copy to drift |
| `outstanding` | `written - acked`, masked: words sent but not yet known-consumed |
| `avail` | `depth - resp_words - outstanding` — room for **data** |

`avail` is conservative in the safe direction. `acked` lags the truth whenever a credit value is in
flight or was dropped, so `avail` *understates* the free room and a write it admits can never stall.
It is deliberately never clamped: a negative value would mean the accounting itself is broken, and
`max(0, ...)` is how that would survive unnoticed.

Driven to zero and back, one word at a time:

```python
seen_down, seen_up = [], []


def run_to_zero_and_back():
    for k in range(7):
        assert (yield from producer.write_nb(np.array([k], dtype=np.uint32))) is True
        seen_down.append(producer.avail)

    # Zero credit.  A refusal, not a stall: the call returns.
    admitted = yield from producer.write_nb(np.array([99], dtype=np.uint32))
    print("at zero credit, write_nb returned:", admitted)
    print("refusals counted:", producer.n_no_room)

    # ...but a response still fits, because its headroom was never data's to compete for.
    ok = yield from producer.write_resp_nb(np.array([0xDEAD], dtype=np.uint32))
    print("the response still fits:", ok, "| resp refusals:", producer.n_resp_no_room)

    for _ in range(8):
        yield from consumer.get(nwords_max=1)
        yield from producer.poll_credit(1)
        seen_up.append(producer.avail)


sim.env.process(run_to_zero_and_back())
sim.env.run()

print("avail as words were written: ", seen_down)
print("avail as words were consumed:", seen_up)
print("credit values dropped:", chan.n_credit_dropped)
```

```text
at zero credit, write_nb returned: False
refusals counted: 1
the response still fits: True | resp refusals: 0
avail as words were written:  [6, 5, 4, 3, 2, 1, 0]
avail as words were consumed: [0, 1, 2, 3, 4, 5, 6, 7]
credit values dropped: 0
```

**`write_nb` refuses; it does not stall and it does not clip.** A burst longer than the credit is
refused *whole*: the producer asked whether the transaction fits, and half of it fitting is not an
answer it can use. (A plain stream would accept the burst and spill the tail into an unbounded
container. Right for a stream, wrong here.)

### The reserved response headroom

`resp_words` (default `RESP_WORDS`, 1) is forward-channel room that data may never take. Data
competes for `depth - resp_words`; a **verdict always fits**.

The reason is asymmetric: at the consumer, a *dropped verdict is indistinguishable from a hang*. So
it gets reserved room rather than taking its chances. `write_resp_nb` draws on that reserve, and
`n_resp_no_room` exists to be **asserted zero** rather than inspected — a non-zero value means the
reservation was violated, not that a response was unlucky.

## The four rules

The whole reverse path rests on these, and each one is load-bearing.

### 1. Reverse values are cumulative, never incremental

A credit value carries *total words consumed so far*, so a lost one is **harmless**: the next one
carries the whole truth. A lost *increment* would wedge the producer against a FIFO that looks full
and is not.

Six words consumed, but only the last value ever reaches the wire:

```python
from waveflow.hw.reverse_stream import CTR_MASK

sim2 = Simulation()
p2 = CreditStreamMasterIF(name="p2", sim=sim2, bitwidth=32)
c2 = CreditStreamSlaveIF(name="c2", sim=sim2, bitwidth=32)
ch2 = CreditStreamIF(name="ch2", sim=sim2, clk=Clock(freq=250e6),
                     bitwidth=32, depth=8, credit_depth=4)
ch2.bind("master", p2)
ch2.bind("slave", c2)


def only_the_last_value_survives():
    for k in range(6):
        yield from p2.write_nb(np.array([k], dtype=np.uint32))
    print("avail after 6 writes:", p2.avail)

    for k in range(6):
        yield from c2.fwd_ep.get(nwords_max=1)
        c2.consumed = (c2.consumed + 1) & CTR_MASK
        if k == 5:
            yield from c2.offer_credit()      # only the LAST value ever reaches the wire

    took = yield from p2.poll_credit(6)       # a poll wide enough to have taken all six
    print("credit values actually taken:", took)
    print("acked now reads:", p2.acked)
    print("avail restored to:", p2.avail)


sim2.env.process(only_the_last_value_survives())
sim2.env.run()
```

```text
avail after 6 writes: 1
credit values actually taken: 1
acked now reads: 6
avail restored to: 7
```

**One value arrived and it restored everything.** The counterfactual is the point: were the reverse
value incremental, the survivor would carry `+1`, `acked` would read `1`, and the producer would sit
at `avail == 2` forever — wedged against a channel that is in fact empty, with no way to recover the
five it lost.

### 2. Both directions are non-blocking

The reverse path uses `offer` and `get_nb`, never `write` and `get`. If it could block it would
become a *second back-pressure route*, which defeats the entire point of having it.

(`CreditStreamMasterIF.write_nb` does use the blocking `write` for the forward data — deliberately.
The credit reservation already proved the room, so a stall there is impossible unless the accounting
is wrong, in which case the run deadlocks **loudly** instead of silently dropping a burst the caller
believed had been admitted.)

### 3. The reader polls a bounded number, never drain-to-empty

`poll_credit(n)` takes **up to** *n* values. *n* is a compile-time constant in the C++ twin, where
the poll unrolls into *n* `read_nb` calls and pipelines. `while (got): ...` is a data-dependent trip
count — the construct that costs the current `RfSampBuf` design its II — and **the Python shape is
what the C++ twin is written from**, so an unbounded loop here would be copied there.

### 4. A saturated reverse channel is not stale-but-safe — it is permanently wrong

With `offer` into a full FIFO, the **newest write is dropped while the reader pops the oldest**. So
*"the newest supersedes"* holds only while the reader outpaces the writer. Saturate it and the
property **inverts**: the reader receives ancient values forever and every fresh one is discarded.

That is a correctness property, not a tuning one. `credit_depth` is sized on a *rate argument* —
there is no structural guarantee on this side — so `CreditStreamIF.n_credit_dropped` is the
measurement behind the argument and a design relying on it should watch it rather than assume it. A
consumer that acks per word needs the *solicited* treatment [Acked Stream](./acked_stream.md) has,
where the depth can be sized rather than hoped at.

## The counters wrap, and that is fine

Every reverse-channel counter is free-running and **will** overflow. No absolute value is ever used —
only differences, and those are bounded by `depth`.

**The hazard is the Python model, not the hardware.** `ap_uint<N>` wraps by itself; Python ints do
not, so a twin computing `written - acked` on unbounded ints agrees with RTL everywhere *except* at
the boundary. Every counter difference goes through `udiff`:

```python
from waveflow.hw.reverse_stream import udiff

print("udiff(3, 65534, bits=16) =", udiff(3, 65534, bits=16))
print("a plain subtraction would give", 3 - 65534)
```

```text
udiff(3, 65534, bits=16) = 5
a plain subtraction would give -65531
```

`CTR_BITS` (16) is deliberately **not** the word width: widening the word must not move where a
counter wraps. `ctr_bits` is overridable per endpoint so a test can walk a counter *onto* its wrap
without simulating 65536 words, and the two sides are refused at bind if they disagree — they must
mask identically, or they agree everywhere except at the wrap.

## API

**`CreditStreamMasterIF`** — the producer, bidirectional by construction (it writes the forward
channel and reads the reverse one), so bind it `'RW'`.

| | |
|---|---|
| `poll_credit(n=1)` | take **up to** *n* credit values; returns how many were taken |
| `write_nb(words)` | write if the accounting says it fits, else refuse. Returns `True`/`False`; never blocks |
| `write_resp_nb(resp)` | write a response from the reserved headroom, so it cannot be refused for room |
| `depth` / `outstanding` / `avail` | the accounting above |
| `n_no_room` / `n_resp_no_room` | refusals counted. The first is expected non-zero in a run that tests admission; the second is expected **permanently zero** |

**`CreditStreamSlaveIF`** — the consumer.

| | |
|---|---|
| `get(schema_type=None, count=None, nwords_max=None)` | consume, then offer the new cumulative total back. Signature mirrors `StreamIFSlave.get`, so it is a drop-in |
| `offer_credit()` | offer the current total. Separate from `get` so a consumer that reads the forward channel some other way can still keep the credit channel honest |
| `consumed` | cumulative words consumed — exactly what goes on the wire |

`get` charges the words the **schema occupied**, not the number of instances — a difference that is
invisible at one word per instance and wrong everywhere else.

**`CreditStreamIF`** — `depth` (forward, required), `credit_depth` (reverse), `n_credit_dropped`.

## How it lowers

**By decomposing, and that is the whole mechanism.** `physical_interfaces()` returns the two
`StreamIF`s and `physical_endpoints()` the four stream endpoints, so every codegen walk sees two
ordinary streams. In C++ there is no credit-stream object at all: it is *literally* a pair of
`hls::stream` plus two registers in the producer, which is the main practical argument for this over
`stream_of_blocks` — nothing new has to be shown to work.

- **HLS** — [Endpoint interfaces](../../comp_codegen/interface.md#stream-endpoints--axis) and
  [Free-running composites](../../comp_codegen/freerunning_composite.md#1-add_if-became-the-channels),
  applied to each sub-channel; there is nothing credit-specific in the generator.
- **BFM / XSI** — whatever each sub-stream gets: an `AxisMaster` against the forward `axis_in`, an
  `AxisSlave` against the reverse `axis_out`. See [the XSI testbench](../../comp_codegen/xsi_tb.md#two-walks).

{: .warning }
> **No C++ twin of this endpoint exists yet.** The rules above describe the shape the twin is *to be
> written from* — bounded polls, cumulative values, `read_nb`/`write_nb` — and
> `waveflow/hw/reverse_stream.py` is explicit that the Python shape is the specification. Nothing in
> `waveflow/build/` is credit-aware today, because it does not need to be: the two streams lower on
> their own.

## See also

- [Acked Stream](./acked_stream.md) — the other reverse channel, and the question it answers instead.
- [Stream Interfaces](../primitive/stream.md) — the primitive both sub-channels are, including
  `offer` and `get_nb`.
- [The access vocabulary](../overview.md#the-access-vocabulary-three-verbs-three-meanings) — why
  `offer` is not spelled `write_nb`.
- `tests/hw/test_reverse_stream.py` — the behaviour on this page, pinned.
