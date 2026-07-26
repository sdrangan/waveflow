---
title: Polling Overhead
parent: Interfaces
nav_order: 4
audience: python
api: [MMIFMaster, AXIMMCrossBarIF, DirectMMIF, MMIFSlave, PollCond, Eq, Ne, AXIMMQueue]
summary: "The loosely-timed polling-overhead model — MMIFMaster.poll_until with a restricted PollCond, the per-bus bandwidth-steal (ov) derating and the deterministic discovery-latency delay, modeled in O(transactions) rather than by stepping every poll cycle."
---

# Polling Overhead (`poll_until`)

A master that **polls** a memory word — a queue consumer watching a ring tail, a core spinning on a doorbell, a driver waiting on a status flag — is not free. It imposes two distinct costs on the shared bus:

1. **Bandwidth steal.** Every poll read consumes bus beats. A master polling every *N* cycles steals a `1/N` fraction of the bus's throughput from everyone else on it, whether or not the watched event has happened yet.
2. **Discovery latency.** After the watched event becomes true, the poller does not see it until its *next* poll. On average that is half a poll interval of delay between the event and its discovery.

Waveflow models **both** costs with [`MMIFMaster.poll_until`](./aximm.md), in **O(transactions)** — it never actually loops the simulation every poll cycle. This page describes the model and the design rationale behind it.

> This is the loosely-timed (LT) twin of a real hardware poll loop (the C++ ring-poll `while (head == tail) tail = gmem[...]`). `poll_until` is also `@synthesizable`: the same call lowers to that C++ poll loop — see [The synthesizable twin](#the-synthesizable-twin) below. Most of this page documents the **simulation** model; the timing parameters (`poll_interval`, `poll_beat_cost`, `discovery`) are loosely-timed concerns and have no hardware meaning.

---

## The primitive

```python
from waveflow.hw.memif import Eq, Ne

value = yield from m_mem.poll_until(addr, cond, poll_interval)   # poll_interval in CYCLES
```

`poll_until` blocks until `cond` holds on a poll of `addr`, then returns the satisfying word value. Conceptually the master reads `addr` every `poll_interval` cycles and checks `cond`; the simulation does **not** issue a timed read every interval — it applies the aggregate model below.

| Parameter | Meaning |
|---|---|
| `addr` | Global byte address of the watched word. |
| `cond` | A restricted [`PollCond`](#the-condition-pollcond) — `Eq(rhs)` or `Ne(rhs)`. |
| `poll_interval` | Poll period in **cycles**. |
| `poll_beat_cost` | (keyword, default `1`) bus beats charged per poll read. |
| `discovery` | (keyword, default `"mean"`) discovery-delay model; only `"mean"` in v1. |

### The condition (`PollCond`)

`cond` is a small declarative value, not an arbitrary Python lambda. It is restricted to equality against a single word — which is exactly what real polls are (`tail != head`, `status == DONE`):

```python
Eq(1)        # read(addr) == 1
Ne(head)     # read(addr) != head   (head is a value read earlier)
```

The `rhs` may be a literal **or** a value captured from a prior runtime read. The restriction is deliberate: the *same* `PollCond` object is the single source of truth that a future extractor will lower to the C++ poll loop, so the sim and the hardware poll can never drift apart (design decision D4). `cond.eval(value)` is the simulation form.

---

## The model

### 1. Bandwidth steal — the per-bus occupancy `ov`

For the duration of a `poll_until` wait, the master registers as an **active poller** on the bus (the slave) it polls. Each active poller *i* contributes a fraction of the bus's beats:

```
ov = Σ  poll_beat_cost[i] / poll_interval[i]      (summed per shared slave/bus)
```

Real (non-poll) transactions on that bus have their **per-word occupancy** stretched by `1/(1-ov)` — the effective bandwidth left for everyone else is `1-ov`. Crucially, **only the per-word term is derated**: the fixed init / address / return-wire latency is untouched. Concretely, in `AXIMMCrossBarIF` a FULL read costs

```
latency_init  +  slave access  +  latency_read_return  +  nwords / (1 - ov)
└─ not derated ──────────────────────────────────────┘   └─ derated ─┘
```

`ov` is **per-bus and time-varying**: it is summed per slave (so independent ports never derate each other) and evaluated at *each transaction's* time, because the active-poller set changes as `poll_until` calls begin and end. The crossbar owns this registry because it has the global, per-bus topology view the derating needs. `DirectMMIF` supports pollers (so a master wired point-to-point can still `poll_until`) but does not derate — there is no contention on a point-to-point link.

### 2. Discovery latency — the event-to-next-poll gap

Once `cond` becomes true, `poll_until` adds the average gap between the event and the next poll:

```
discovery delay = (poll_interval - 1) / 2   cycles      (the deterministic mean)
```

This is **deterministic by default** (decision D1). Waveflow's verification spine — byte-identical `sim_timeline.json` baselines, the "metrics hold" gate, the calibration regression — depends on reproducible timelines, so the default is the mean, never bare randomness. A seeded-stochastic mode (`U[0, poll_interval-1]`, seeded from the `Simulation`) is reserved but not exposed in v1.

### Saturation: `ov ≥ 1` is clamped and warned

When polling alone would consume the whole bus, `1/(1-ov)` diverges. A 1-cycle poll is already `ov = 1`. Rather than producing garbage timing, the model **clamps `ov` to 0.99** (a visible ~100× per-word stretch that unmistakably reads as "polling-bound") and emits **one loud warning per bus per saturation onset** (decision D2):

```
UserWarning: AXIMMCrossBarIF '...': bus 'slave_0' is polling-bound (ov=1.000 >= 1);
clamping to 0.99 (per-word occupancy stretched ~100x). A poll interval this aggressive
saturates the bus merely checking the condition.
```

This is a feature: the model flags the exact 1-cycle-poll mistake that a coarse fixed poll period would silently hide.

---

## Making a slave pollable

`poll_until` needs an **untimed** snapshot of the watched word while it waits — its bus cost is already charged aggregately through `ov`, so the peek itself must take zero sim time. A slave advertises this by setting `peek_read`:

```python
self.slave_ep = MMIFSlave(
    sim=self.sim, bitwidth=32,
    rx_write_proc=self.rx_write,
    rx_read_proc=self.rx_read,
    peek_read=self.peek,           # (nwords, local_addr) -> Words, untimed snapshot
)
```

[`MemModel`](../flows/modules.md) populates `peek_read` automatically. A slave without one cannot be polled (`poll_until` raises). The blocked poller does not spin: it waits on a per-slave write-notify the interconnect fires after every write, then re-peeks — so there is no lost wakeup and no per-cycle stepping.

---

## Worked example

`examples/interface/poll_demo.py` isolates each cost in its own scenario over one shared FULL slave:

```
  Producer  (master_0) ──┐
  Poller    (master_1) ──┼── AXIMMCrossBarIF ──── Ram (slave_0, FULL)
  Burster   (master_2) ──┘
```

**A — discovery latency.** The poller `poll_until(flag, Eq(1), poll_interval=8)`s a flag the producer sets later. The flag is observed exactly `(8-1)/2 = 3.5` cycles after the producer's write completes — the deterministic mean:

```
producer set flag=1 at t=13.143
poller observed it at  t=16.643   (value=1)
discovery delay = 3.500 cycles    (expected (poll_interval-1)/2 = 3.500)
```

(The producer's own write lands a little late, too — the poller's `ov` derates the very write it is waiting on, since both touch the same slave.)

**B — bandwidth steal.** A 64-word burst read is timed with and without a concurrent active poller (`poll_interval=4`, so `ov=0.25`):

```
burst (64 words) alone:           69.000 cycles
burst + poller (interval=4, ov=0.25): 90.333 cycles
```

The per-word term stretches by `1/(1-0.25) = 1.333×` (64 → 85.33 words of time); the total grows from 69 to 90.3 — only `1.31×`, because the init/access latency is *not* derated.

**C — saturation.** A 1-cycle poll (`ov = 1`) trips the clamp-and-warn.

Run it:

```bash
python examples/interface/poll_demo.py
```

---

## VMAC integration

`AXIMMQueue.get`'s empty-ring wait is built on `poll_until`: the consumer polls the ring tail with `poll_until(tail_addr, Ne(head), poll_interval)`. This retired the old coarse `poll_cycles=64` band-aid — a poll interval now *means* something. An aggressive consumer poll shows up as derated bus throughput (and, if too aggressive, the saturation warning), not merely shifted dequeue times, so the cycle-model calibration can reflect real polling cost.

---

## The synthesizable twin

`poll_until` is a legitimate hardware primitive too — a master spinning on a memory word. The **same** `poll_until` call that runs the LT model in simulation is `@synthesizable`: inside a synthesizable `run_proc`, the extractor lowers it to a C++ poll loop over the `m_axi` pointer, the hardware twin of the sim form. (The [AXI-MM command-queue example](../../examples/mmqueue/) shows the same lowering for the ring-dequeue hook it shares this primitive with.)

```python
# inside a synthesizable run_proc — the consumer waits for the ring to fill
head = yield from self.cmd_queue.get(self.Cmd)         # a prior runtime read
tail = yield from self.m_mem.poll_until(tail_addr, Ne(head), poll_interval)
```

lowers to a call into a small reusable C++ primitive (`waveflow/build/poll_until_impl.tpp`):

```cpp
// poll word `tail_addr` of gmem until (value != head); return the satisfying value
ap_uint<MEM_BW> tail = poll_until_impl::poll_until_ne<MEM_BW>(
    m_mem, memmgr::byte_addr_to_word_index<MEM_BW>(tail_addr), (ap_uint<MEM_BW>)head);
```

What lowers and what does not:

- **The `PollCond` is the lowerable subset.** `Eq(x)` / `Ne(x)` map to the `==` / `!=` C++ poll; the `rhs` may be a **constant** (`Eq(1)`) or a **runtime value already in scope** (`Ne(head)`, where `head` is a prior read). Supporting a runtime-variable rhs is the condition-IR extension this step added — the same capability now also lets a synthesizable `if a != b:` compare two runtime locals.
- **The timing parameters do not lower.** `poll_interval`, `poll_beat_cost`, and `discovery` are AT-model (loosely-timed) concerns — a hardware poll just spins on the word — so they never appear in the generated C++.

This is one poll loop, not two: `AXIMMQueue.get`'s ring-dequeue hook (`aximm_queue_impl::queue_get`) expresses its own `while (head == tail)` non-empty wait as `poll_until_ne(tail_word, head)`, so the standalone `poll_until` and the queue dequeue share the same primitive.

## See also

- [MM Interfaces](./aximm.md) — the `MMIFMaster` / `AXIMMCrossBarIF` endpoints and the FULL/LITE latency model `poll_until` plugs into.
- [Overview](./overview.md) — the `Words` type and the SimPy transaction lifecycle.
- [AXI-MM Command Queue example](../../examples/mmqueue/) — the ring-dequeue hook (`queue_get`) this poll primitive is shared with, and its cosim calibration.
