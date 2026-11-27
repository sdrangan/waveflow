---
title: The architecture
parent: Emitter detection and AoA
nav_order: 3
summary: "The architecture, being written top-down. Done: the front end. One streaming kernel, the capture and power detector, takes every channel's samples at line rate, writes them into a ring of block slots in a true dual-port BRAM through its own port, and sums each channel's energy as the samples pass. At each block's end it compares the energies with two thresholds, according to each channel's state (absent, active or pending), and notifies downstream on a credit stream: a dropped notification when no credit is free, never a stall. Downstream readers read the ring over the crossbar on the other port and check the slot's header word last, so a block overwritten while being read is reported late, never used. Downstream blocks: to be written."
---

# The architecture

**Status: under construction.** This page is being written top-down. The front end (the capture and
power detector, and the ring memory) is described below. The downstream blocks that search and track
are still to be designed.

## Overview

```text
 samples ──► [ capture + power detector ] ──port B (write only)──► ring of R block slots (T2P BRAM)
 B·C·n_rx       │ ▲          ▲                                              │ port A, over the crossbar
 a block        │ │          │ registers: arrival and                       ▼
                │ │          │ departure thresholds,          [ downstream: search and track jobs ]
                │ │          │ counters                                     │
                │ │          └──────── host (A53), memory-mapped            │
                │ └── state messages (channel → active / absent) ◄──────────┤
                └──── notifications (block, channel, type, energy, slot) ───►┘  credit stream
```

Two rules shape the front end:

- **The sample path never stalls.** Samples arrive from the converters at a fixed rate, and nothing
  that faces a converter may wait. The capture and power detector therefore never reads memory, never
  waits for a reader, and never blocks on its outputs.
- **Reading happens downstream.** Every read of the samples goes through the ring's other port, over
  the crossbar, where the downstream jobs compete with each other. That is where the read delay and the
  contention are, and they are part of what is measured.

## The capture and power detector

One streaming kernel (a `FreeRunMod`) at the head of the chain. It writes every sample into the ring
and computes each channel's energy **in the same pass, as the samples go by**. It makes no detection
decision: it only decides, from energy and state, whether downstream should look at a block.

### State

- **A state per channel**, one of three:
  - `absent`: no emitter is being tracked. The channel is notified when its energy is **above** the
    arrival threshold.
  - `active`: an emitter is being tracked. The channel is notified when its energy is **below** the
    departure threshold.
  - `pending`: a notification is out and its answer has not come back. The channel is not notified
    again until downstream sets it to `active` or `absent`.

  All channels start `absent`. Only a state message from downstream changes `active` or `absent`; the
  kernel itself moves a channel to `pending` when it notifies.
- **The block index**, counting blocks since start.
- **The ring's write slot**, the block index modulo $R$.

`pending` is what keeps one emitter from causing two searches. Without it, an absent channel whose next
block is also above the arrival threshold would be notified again before downstream had answered the
first notification. It also sets the late-block policy of [the problem](problem.md#what-can-be-designed):
while a channel is pending, its newer blocks are skipped.

### Interfaces

| Interface | Direction | What |
|---|---|---|
| Sample stream | in | blocks of $B \cdot C \cdot n_{rx}$ samples, at line rate |
| Ring, port B | out, write only | every sample, into the current slot |
| Registers (memory-mapped, host) | in | the arrival and departure thresholds, common to all channels for now, changed rarely |
| Counters (memory-mapped, host) | out, read only | blocks seen; notifications sent and dropped, per channel |
| State messages | in, a queue | from downstream: channel $j$ → `active` or `absent` |
| Notifications | out, a credit stream | to downstream: one message per notified channel-block |

**The thresholds are stored already scaled** by the host: the register holds the threshold on the
**sum** $\sum |x|^2$ over a channel's block ($B \cdot n_{rx}$ terms), not on the mean, so the kernel
compares without dividing.

### Operation, per block

1. **For each sample word:** write it to the ring through port B, into the current slot; add $|x|^2$ of
   each element into its channel's accumulator. A word holds several antennas and channels of one
   sample, so a lane loop adds each lane into its channel's sum, unpacked with the generated array
   utilities. The slot's **header word** (the block index) is written before the block's first sample.
2. **At the block's end**, with the $C$ energies complete, read the two thresholds.
3. **For each channel**, by its state:
   - `absent` and energy ≥ arrival threshold → an **arrival** notification;
   - `active` and energy < departure threshold → a **departure** notification;
   - `pending`, or neither condition → nothing.
4. **For each notification, in order:** if a credit is free, send the message and set the channel to
   `pending`. If not, **drop it**: count the drop, and leave the channel's state as it was.
5. Reset the accumulators; move to the next slot.

**A dropped notification repairs itself.** The channel's state is unchanged, so if its energy is still
past the threshold, the next block notifies again. What the drop costs is delay, which is measured.

**The notification order matters when credits are short.** If several channels qualify in one block and
fewer credits are free, a fixed channel order always favours the low channel numbers. Options:
departures first (cheap and important), round-robin over channels, or highest energy first. It is a
design parameter.

### The notification

| Field | What |
|---|---|
| `tx_id` | the notification's id, echoed by downstream's state message |
| `block` | the block index (the delay is measured from it) |
| `channel` | $j$ |
| `type` | arrival or departure |
| `energy` | the measured sum |
| `slot` | the ring slot holding the block |

The samples are **not** in the message. Downstream reads them from the ring.

### Credits

The notification stream carries a credit per message in flight; the credit pool's size, the most
notifications downstream can hold at once, is a design parameter. When downstream finishes with a
notification it returns the credit. A credit bounds how much work is queued downstream. It does **not**
protect the block's data: the ring does not wait for readers ([below](#the-ring)).

### Why it always keeps up

- Its writes go to its own memory port, which nothing else uses, at one word a cycle.
- It never reads memory.
- Its notification output never blocks: no credit means a drop, not a wait.
- The state-message input is polled, never waited on.

So it runs at line rate whatever happens downstream, and nothing downstream can make it lose a sample.

## The ring

A **true dual-port BRAM** (`bram_t2p.v`; in pysim `MemSlaveBramWindow`, `waveflow/hw/mm_bram.py`) holding
$R$ slots, each one block of every channel ($B \cdot C \cdot n_{rx}$ samples) plus a header word.

- **Port B: the capture and power detector only**, write only, one word a cycle.
- **Port A: every downstream reader**, over the crossbar.

**The memory has no lock.** The two ports are independent, and a read of the address being written in
the same cycle returns whatever the BRAM's read-during-write mode gives
([BRAM interface](../../guide/interface/primitive/bram.md)). This is what lets the writer never stall,
and it means nothing in the memory stops a reader from reading a slot the writer has started to reuse.
The ring's contract does that instead:

**The ring's contract: the header first, the header last.**

1. **The writer writes the header first.** Each slot's write starts with its header word, the block
   index $n$, then the samples, in order, on its own port.
2. **The reader reads the header last.** A reader takes $n$ and the slot from its notification, reads
   the samples it needs, and **then** reads the slot's header.
3. **If the header still says $n$**, the data was intact for the whole read. **If it says $n + R$**, the
   writer had started overwriting the slot: the job reports **late**, its result is discarded, and the
   lateness is counted.

One check, at the end, is enough. A notification goes out only after block $n$ is completely written.
The slot is next touched when block $n + R$ begins, and its header is the first word written. So if any
sample the reader read had been overwritten, the header was overwritten before it, and the reader's
header read, which comes last, sees it. Two orderings carry this: the writer's writes land in order on
port B, and the reader's header read is issued after its data reads have finished.

**The ring's depth sets how late a read may be.** A slot is overwritten $R$ block periods after it was
written, so a job must finish reading within about $R - 1$ block periods of its block's end. $R$ is a
design parameter: a deeper ring costs BRAM and tolerates more queueing. At the proposed sizes a slot
is about 32k samples, 128 KB at 32 bits a sample, so $R = 8$ is about 1 MB, which fits in the
xczu48dr's URAM.

**Lateness is a measured outcome, not an error.** It is how overload shows up in the ring, alongside
dropped notifications.

### Two checks for the timing model

- **pysim's port B is untimed** (a method call). The capture kernel's own timing model must spread its
  writes over the block period, one word a cycle. Otherwise a whole block appears at one instant in
  pysim, and the overwrite race the header guards against can never occur there.
- **A collision check at RTL.** No error from the memory is visible in the XSI flow today; the check is
  a scan of the trace after the run (`waveflow.utils.bram_trace.find_read_during_write`). A sticky
  `collision` output on the memory, proposed in `plans/rtl_module.md`, would make the header protocol
  checkable in both backends.

## Downstream

*To be written.* The jobs that a notification starts:

- **arrival** (the channel is searching): the Gram matrix, the direction scan, the SNR estimate and the
  detection test ([a.2 to a.4](algo.md#searching-no-emitter-detected-yet));
- **departure** (the channel is tracking): the power in the tracked direction and the stop test
  ([b.2](algo.md), tracking).

Each ends with a state message back to the power detector (`active` or `absent`) and a report to the
host, and returns its credit. Still to decide: the controller (the A53 or a hardware sequencer), the
units and how jobs reach them, where $\hat{Q}$ lives between a.2 and a.3, and what the host is told.

## Open

- The sample word's layout (antennas and channels per word), which sets the lane loop.
- The notification order when credits are short.
- The credit pool's size and the ring's depth $R$.
- Whether the arrival and departure thresholds become per channel.
