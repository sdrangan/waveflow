---
title: Two kernels on a bus
parent: Examples
nav_order: 10
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

What this adds to [A memory-mapped FIR](../mm_fir/index.md#learning-objectives) and the bus example
before it, in the order the pages teach it:

1. Model a **two-state Markov chain** -- its transition probabilities, its stationary distribution --
   and simulate it with a pseudo-random generator, in integer arithmetic that Python and the RTL agree
   on bit for bit.
2. Make a **kernel a bus master** that writes another kernel's input queue, through an
   [MM-stream](../../guide/interface/axi_mm/credit_streams.md) with a reverse **credit stream**
   (`MmCreditStreamIF`), its queue and credit mapped to views on the receiving kernel's slave adaptor.
3. **Batch** the credit (one bus write per 32 words) without losing liveness (`max_write`).
4. **Size** a credit link: a FIFO in front of a store-and-forward writer, and a credit window that
   covers the link's bandwidth-delay product.
5. Make a response mean "**done**", not "seen": the chain's response is forwarded by its memory writer
   only after the results are stored.
6. Write both kernel bodies as **loops, one step per cycle** -- a recurrence included -- and check
   credit between chunks, not inside the loop.
7. Run the same kernels in two wirings of one system: **joined directly**, and over the bus.
8. Put **four bus masters** on one crossbar at RTL, and account for its cycles with timing probes.
9. Generate a docs figure as a **build step**, from the golden model.

## Pages

- [Theory](theory.md) — what a Markov chain is and where it is used, the two-state chain's math, how
  one step is simulated, and the two modules.
- [Protocol](protocol.md) — every message and the order it flows in: command, forwarded command and
  draws, credit, states to memory, the response.
- [Python model](python.md) — the schemas, the golden model, the generator and the chain (a composite
  with the framework's memory writer).
- [The host](host.md) — `MarkovHost`, step by step: what a host owns, what the system hands it, its two
  processes, and a checklist for writing your own.
- [The system](system.md) — `markov_system`, step by step: the modules, the memory regions, the direct
  wiring, and the bus wiring (devices, the routed credit link, the crossbar, interrupts, the host's
  endpoints).
- [The credit link](credit_link.md) — how the generator's stream reaches the chain across the bus: the
  views, the routing, the numbers that size it, and what the RTL taught.
- [Python simulation](pysim.md) — both wirings, the results, what the gates check, the chain's output,
  and how close the timing is.
- [Build flow](build.md) — the build DAG (`markov_build.py`): which steps are the example's (codegen and
  the scenario), which are the framework's, the CLI, and what a second run skips.
- [Code generation](codegen.md) — what each pysim object generates (Fig. 1), the four tops, the two
  hand-written bodies as loops, and the timing they close at.
- [Synthesis](synth.md) — csynth of the four tops, the crossbar IP, and the Verilog top walked from the
  graph (Fig. 2).
- [XSI testbench](xsi.md) — the RTL under a testbench (Fig. 3): the host's C++ twin, the generated
  harness, the run against pysim, reading the traces back, and the gates.
- [RTL simulation](rtlsim.md) — the four-master system under XSI, the gates, and finding the time with
  probes.

The code is in [`examples/markov`](../../../examples/markov).
