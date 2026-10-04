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
| `markov_gen` | the generator; credit accounting in the body | 1 | 6.8 ns |
| `markov_chain` | the chain core + the in-band memory writer | 1 | 6.6 ns (core) |
| `mm_queue_writer_64_128` | the routed link's forward writer | -- | 7.3 ns |
| `mm_credit_writer_64` | the routed link's credit writer | -- | 7.3 ns |

Both bodies are written as the [command-response pattern](../../guide/patterns/command_response.md)
reads: one firing is one job -- read the command, then per chunk a pipelined loop of one step per cycle
(a word of draws read every four steps: the
[lane loop, one element per iteration](../../guide/vectorization/hls/loop_optimization.md)), then the
response. The generator checks credit between chunks, never inside the loop; the chain offers credit
after each chunk. Their first versions were single-firing state machines; the loops read like the
Python, close timing with more margin (the generator went from 9.9 to 6.8 ns), and run 4.7% faster at
RTL -- these firings are long, so the drain at the end of each chunk costs little.

`examples/markov/markov_xsi.py` puts all four under one generated top with AMD's crossbar (from the
pysim crossbar: 4 SI, 3 MI), the adaptor views, and a BRAM as the shared memory. Each bus writer's
`target` -- its peer view's word address -- is a constant the top drives, so neither kernel's RTL
depends on where the other is placed. The C++ host is the pysim host on the testbench endpoints.

| | pysim | RTL |
|---|---|---|
| 4 jobs x 300 steps, 2 in flight | 1926 cycles | **1865 cycles** |
| `x` | bit-exact | **bit-exact** |
| host polls | 0 | **0** |

pysim is within 3.3% of the RTL, and a gate keeps it within 5%.

## Finding the time

The first RTL run took 2356 cycles against pysim's 1700. `markov_xsi.run_xsi(..., probes=True)` exposes
one-bit probes on every link's handshake -- the generator's words to its writer, each writer's bus
bursts and acknowledgements, the chain's reads, credit offered and taken, responses -- and the
testbench prints the cycles each fired. Lined up, they found two defects in the **design** and two
costs missing from the **model**:

| step | what the probes showed | the change | RTL cycles |
|---|---|---|---|
| -- | state-machine bodies | -- | 2356 |
| 1 | -- | both bodies rewritten as loops per job | 2246 |
| 2 | a chunk left the generator every 103 cycles for 64 draws: the queue writer is store-and-forward (it needs a write's length before its words), so while it burst one chunk it read nothing -- and nothing buffered the generator, which stalled | a FIFO between the generator and its writer (`MmCreditStreamIF.fwd_depth`, two chunks; the RTL top instantiates it at that depth) | 2015 |
| 3 | at every job start the generator stalled for 70--110 cycles waiting for credit, then the chain starved for ~60: the 63-word credit window was smaller than the link's round trip (FIFO, writer, queue, up to 31 unreported words, the credit path back) | the chain's queue 64 -> 128 words -- a window that covers the bandwidth-delay product | **1865** |
| 4 | the chain now the bottleneck at 79 cycles a chunk, the generator at 71: 64 steps plus a fixed cost (the loop's fill and drain, the `MemWCmd` words, the credit check) | the model charges each kernel's measured `chunk_overhead` (15 and 7) | pysim 1700 -> 1926 |

Steps 2 and 3 are the lessons that carry over: a store-and-forward stage needs a buffer in front of it
the size of what it gathers, and a credit window must cover the link's bandwidth-delay product -- see
[Credit stream over a shared bus](../../guide/interface/derived/credit_stream.md#over-a-shared-bus).

## Files

- `examples/markov/markov.py` -- the kernels, the golden, the host and the system (both wirings).
- `examples/markov/include/markov_gen_task.h`, `markov_chain_core_task.h` -- the hand-written HLS
  bodies.
- `examples/markov/markov_build.py` -- headers, tops, csynth.
- `examples/markov/markov_xsi.py` -- the RTL top and the C++ host.
- `tests/examples/test_markov.py`, `tests/examples/test_markov_xsi.py` -- the gates.
