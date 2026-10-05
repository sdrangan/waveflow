---
title: Two kernels on a bus
parent: Examples
nav_order: 9.56
has_children: true
example_dir: examples/markov
summary: "Two free-running kernels that talk over a shared bus, with no polling anywhere: a host sends commands and sleeps on interrupts; a generator kernel draws pseudo-random numbers and streams them to a Markov-chain kernel through a credit stream routed over the crossbar; the chain writes its states to shared memory and answers the host. The kernels are deliberately simple so that the example is about the links -- why a kernel writing another kernel's queue must hold credit, how the credit comes back without stalling the bus, and how the same kernels run joined directly. Bit-exact in pysim and at RTL with four bus masters on one crossbar, and pysim's timing within 3% of the RTL."
---

# Two kernels on a bus

In the [memory-mapped FIR](../mm_fir/index.md) the only bus master is the host. Here a **kernel** is a
bus master too: a generator kernel writes another kernel's input queue across the same shared bus the
host uses. That changes one thing that matters. A stream's usual back-pressure -- the consumer simply
not taking the next word -- does not work across a bus: a write into a full queue **stalls the bus**,
and can deadlock it. The producer has to know there is room *before* it writes. That is
**credit-based flow control**, and this example is the worked design for it.

The computation is a toy on purpose: a two-state **Markov chain** driven by a pseudo-random generator.
Each kernel is a few lines; what the example is about is everything between them.

## Learning objectives

In going through this example, you will learn how to:

- Model a **two-state Markov chain** -- its transition probabilities, its stationary distribution --
  and simulate it with a pseudo-random generator, in integer arithmetic that Python and the RTL agree on
  bit for bit.
- Split a computation into **two kernels joined by a stream**, and pass the job's command down the
  pipeline ahead of its data -- the [command-response pattern](../../guide/patterns/command_response.md)
  through a pipeline.
- See why **back-pressure does not cross a shared bus**, and use the pattern that does: an
  [MM-stream with credit](../../guide/interface/axi_mm/credit_streams.md) -- a credit stream
  (`CreditStreamIF`) routed over the crossbar (`MmCreditStreamIF`), with the kernels' code unchanged.
- Declare the views a kernel is reached through -- a **queue in** for data, a **credit-in register**
  for credit -- and route the link between two kernels' views.
- **Batch** the credit (one bus write per 32 words) without losing liveness (`max_write`).
- **Size** a credit link: a FIFO in front of a store-and-forward writer, and a credit window that covers
  the link's bandwidth-delay product.
- Make a response mean "**done**", not "seen": the chain's response is forwarded by its memory writer
  only after the results are stored.
- Write both kernel bodies as **loops, one step per cycle** -- a recurrence included -- and check credit
  between chunks, not inside the loop.
- Put **four bus masters** on one crossbar at RTL, and use **timing probes** to find where the cycles
  go, then calibrate pysim's timing against the RTL.
- Generate a docs figure as a **build step**, from the golden model.

## Pages

- [Theory](theory.md) — what a Markov chain is and where it is used, the two-state chain's math, how
  one step is simulated, and the two modules.
- [Protocol](protocol.md) — every message and the order it flows in: command, forwarded command and
  draws, credit, states to memory, the response.
- [Python model](python.md) — the schemas, the golden model, the generator and the chain (a composite
  with the framework's memory writer), the host.
- [The credit link](credit_link.md) — how the generator's stream reaches the chain across the bus: the
  views, the routing, the numbers that size it, and what the RTL taught.
- [Python simulation](pysim.md) — both wirings, the results, what the gates check, the chain's output,
  and how close the timing is.
- [Code generation](codegen.md) — the four synthesized tops, the two hand-written bodies as loops, and
  the timing they close at.
- [RTL simulation](rtlsim.md) — the four-master system under XSI, the gates, and finding the time with
  probes.

The code is in [`examples/markov`](../../../examples/markov).
