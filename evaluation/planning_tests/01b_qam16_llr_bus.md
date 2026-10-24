# Planning test 1b: the soft demapper behind a bus, with a host

Plan the Python and RTL models of the system below.  The deliverable -- what to write, and how -- is in
[deliverable.md](deliverable.md); read it first.

## 1. Purpose

The 16-QAM soft demapper of [01_qam16_llr.md](01_qam16_llr.md) -- the same kernel, the same
messages, the same arithmetic, at `WORD_BW = 32` only -- reached by a **host program over an AXI-MM
bus**, as a processor would reach it.  Read that spec for the kernel; this one adds only the system
around it.

## 2. Architecture

```text
              +---------------------------- DUT ----------------------------+
host --AXI-MM--> [ crossbar ] --> [ device: views | qam16_llr kernel ]       |
  ^                                     | interrupts                         |
  +-------------------------------------+------------------------------------+
```

- **The kernel** is rung 1's, unchanged: its three streams `s_in`, `m_llr`, `m_status`.
- **The device**: the kernel behind memory-mapped views of its streams, on one AXI-MM slave port --
  a queue the host writes into `s_in` (commands and samples), a queue the host reads from `m_llr`, a
  queue the host reads from `m_status`.  Each view raises an interrupt.
- **The crossbar**: one master (the host), the device's slave port(s).
- **The host** (the testbench): a software program of **two threads** -- a writer that sends each
  command and its samples, and a reader that takes each command's results and its status.  It keeps
  at most **two commands in flight**.  It **never polls**: every wait is on an interrupt.  In the Python
  simulation it is Python; at RTL it is C++ driving the same bus.
- The DUT is the device and the crossbar.

## 3. What the host does

For each command, the writer writes the command and its samples into the input queue.  The reader, in
the order the kernel writes: the results' header, then as many LLR entries as the header announces, then
the status; it checks the header's and the status's `tx_id` against the command, and the status is `OK`.

The host runs only good commands: an error stops the kernel (rung 1), and recovering from it -- a status
register, a reset -- is outside this test.

## 4. The test plan

| # | scenario | what it exercises |
|---|---|---|
| 1 | rung 1's worked examples, one command each | the arithmetic through the bus |
| 2 | 12 commands from a fixed seed, `nsamp` from 1 to 256, two in flight | the host's two threads, the queues filling and draining |
| 3 | one command, `nsamp = 256` | a command larger than a small queue: the writer and reader overlap |

**Acceptance:**

- the kernel first, **alone**: rung 1's harness gates (pysim bit-exact, RTL against pysim) at
  `WORD_BW = 32`;
- the system in pysim: every result and status the host reads equal to the golden's;
- the system at RTL: the host's reads and writes on the bus at RTL identical to pysim's, the host
  finished, **zero polls**;
- the system's cycle count at RTL recorded, and the Python model's -- built from the kernel's timing
  calibrated alone, with nothing fitted to the system -- within 5% of it.
