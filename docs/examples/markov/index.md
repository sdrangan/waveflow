---
title: Two kernels on a bus
parent: Examples
nav_order: 9.56
has_children: false
example_dir: examples/markov
summary: "Two free-running kernels that talk over a shared bus, with no polling anywhere: a host sends commands and sleeps on interrupts; a generator kernel draws pseudo-random numbers and streams them to a Markov-chain kernel through a credit stream routed over the crossbar; the chain writes its states to shared memory and answers the host. The kernels are deliberately trivial so that the example is about the links -- why a kernel writing another kernel's queue must hold credit, how the credit comes back without stalling the bus, and how the same kernels run joined directly. Bit-exact in pysim and at RTL with four bus masters on one crossbar."
---

# Two kernels on a bus

In the [memory-mapped FIR](../mm_fir/index.md) the only bus master is the host. Here a **kernel** is a
bus master too: it writes another kernel's queue. That changes one thing that matters. A host that
writes a full queue can wait on the queue's interrupt; a kernel writing over a shared bus cannot just
block on the write, because a write into a full queue **stalls the bus** -- the crossbar path and the
target's adaptor front, which serves every view behind it for every master, including the reads that
would drain the queue. The producer has to know there is room *before* it writes.

That is credit-based flow control, and the repo already has it: a
[credit stream](../../guide/interface/derived/credit_stream.md). This example routes one over the bus.

The computation is a toy on purpose: a two-state Markov chain driven by a pseudo-random generator.

## The system

```
 host ──cmd──▶ gen.qcmd ─▶ MarkovGen ══ u (credit stream, over the bus) ══▶ MarkovChain ─▶ x ─▶ memory
   ▲                                    ◀══ credit (over the bus) ═════════                 │
   └──── irq ◀── chain.qresp ◀── response (once x is stored) ◀──────────────────────────────┘
```

| step | what | how |
|---|---|---|
| host -> generator | `MkvCmd(tx_id, n, x0, seed, p01, p10, dstaddr)` | queue in `qcmd`; the host waits for room on its interrupt |
| generator -> chain | the command, then `u[0..n-1]` | a credit stream **routed over the crossbar**: the generator's bus writer -> the chain's queue in `qu` |
| chain -> generator | cumulative words consumed | the credit half, routed back: the chain's bus writer -> the generator's credit in `u_crd` |
| chain -> memory | `x[0..n-1]` at `dstaddr` | the framework's in-band memory writer (`MemWStream`) |
| chain -> host | `MkvResp(tx_id, n, ones)` | forwarded by the memory writer **only once x is stored**, to queue out `qresp`; the host waits on its interrupt |
| host <- memory | `x` | a bus read |

**The generator** (`MarkovGen`) draws `u[k]` from xorshift32, seeded per command, keeping the top 16
bits, and sends them in writes of 64 draws.

**The chain** (`MarkovChain`) is a composite: `ChainCore` runs the chain, and the framework's in-band
`MemWStream` stores `x`. With `P(0 -> 1) = p01` and `P(1 -> 0) = p10` in Q16, one step is

```
t0 = u <  p01        # from state 0: go to 1
t1 = u >= p10        # from state 1: stay at 1
x' = x ? t1 : t0
```

Both compares depend only on `u`, so the dependency carried from step to step is a 2:1 select -- and
the HLS body runs one step per cycle despite the recurrence.

## Flow control, with no polling

**Between the kernels: credit.** The generator writes only what its credit says fits, so it never
writes into a full queue and never stalls the bus. The chain returns its cumulative count; because the
count is cumulative, the credit-in view can be a register that a new value simply overwrites -- so a
credit write never waits either.

**Batched.** Every credit is a bus write, so the chain offers one per 32 words consumed (`crd_every`,
half the queue). A consumer may then sit on up to 31 unreported words indefinitely; the generator's
`max_write` shrinks by exactly that much (`64 - 1 - 31 = 32` words, and its writes are 16), so a
waiting generator always gets its room.

**End to end: admission.** The host keeps at most two jobs outstanding. That bounds what credit cannot
-- the number of responses in flight.

**The host waits on interrupts.** Room on `qcmd`, data on `qresp`. It never reads a vacancy or an
occupancy, in pysim or at RTL.

## Two wirings, one pair of kernels

`MarkovSystem(link="direct")` joins the kernels with a plain `CreditStreamIF` and the host straight to
them; `link="mm"` builds each kernel's memory-mapped device from the views its type declares, routes the
link with `MmCreditStreamIF`, and puts four bus masters on one crossbar -- the host, the generator's
writer, the chain's credit writer and the chain's memory writer. The kernels' code is the same in both.

```python
from examples.markov.markov import demo
demo("direct")["bit_exact"], demo("mm")["bit_exact"]      # (True, True)
```

## At RTL

`python -m examples.markov.markov_build` generates every header and top and synthesizes four:

| top | what | II | estimated clock (10 ns target) |
|---|---|---|---|
| `markov_gen` | the generator; credit accounting in the body | 1 | 9.92 ns |
| `markov_chain` | the chain core + the in-band memory writer | 1 | 6.8 ns (core) |
| `mm_queue_writer_64_64` | the routed link's forward writer | -- | 7.3 ns |
| `mm_credit_writer_64` | the routed link's credit writer | -- | 7.3 ns |

Two bodies first missed the clock, and both fixes are the same lesson: do not decide, compute and
commit in one firing. The generator admitted a write and drew its first sample in the same cycle
(17.4 ns); admission now takes its own firing, one cycle per 64 draws. The chain tested a field of the
header word it had just read (10.3 ns); the test moved to the next firing.

`examples/markov/markov_xsi.py` puts all four under one generated top with AMD's crossbar (from the
pysim crossbar: 4 SI, 3 MI), the adaptor views, and a BRAM as the shared memory. Each bus writer's
`target` -- its peer view's word address -- is a constant the top drives, so neither kernel's RTL
depends on where the other is placed. The C++ host is the pysim host on the testbench endpoints.

| | pysim | RTL |
|---|---|---|
| 4 jobs x 300 steps, 2 in flight | 1700 cycles | **2356 cycles** |
| `x` | bit-exact | **bit-exact** |
| host polls | 0 | **0** |

pysim is 28% fast. The likely cause is in the link: the RTL queue writer gathers a whole write before
it bursts, and nothing buffers the generator while it does -- neither is in the pysim model. That gap
is open.

## Files

- `examples/markov/markov.py` -- the kernels, the golden, the host and the system (both wirings).
- `examples/markov/include/markov_gen_task.h`, `markov_chain_core_task.h` -- the hand-written HLS
  bodies.
- `examples/markov/markov_build.py` -- headers, tops, csynth.
- `examples/markov/markov_xsi.py` -- the RTL top and the C++ host.
- `tests/examples/test_markov.py`, `tests/examples/test_markov_xsi.py` -- the gates.
