---
title: A memory-mapped FIR
parent: Examples
nav_order: 9.55
has_children: true
example_dir: examples/mm_fir
summary: "The first kernel that is REACHED over the bus rather than driving it. A free-running FIR whose taps are a register bank and whose samples and results are queues, all behind an AXI slave port that a host program writes and reads through AMD's crossbar. The kernel stays stream-only; a memory-mapped adaptor in the RTL top turns bus transactions into its messages. Because a config and the samples travel on different streams, the order between them is carried in the messages: each packet's in-band header names the config it needs by sequence number, the kernel waits for that config, and a response FIFO echoes which config each packet was filtered with. Bit-exact against an exact-integer golden in pysim and at RTL, in two adaptor topologies."
---

# A memory-mapped FIR

Every example before this one has a kernel that *drives* the bus: it sends commands to a
`MemRStream` / `MemWStream`, and those turn them into AXI bursts. This example is the first kernel
that is **reached** over the bus. A host writes its taps into registers, pushes samples into a queue,
and reads results out of another queue — all through one AXI slave address range, the way a
processor talks to a peripheral.

Vitis HLS cannot generate that slave side. So the kernel stays exactly what the earlier examples'
kernels are — a stream-only `FreeRunMod` — and a [memory-mapped slave
adaptor](../../guide/interface/axi_mm/slave.md) in the RTL top does the translation: bus
transactions in, stream messages out. This example is the worked design for that adaptor, and for the
[components of the XSI simulation](../../guide/flows/concurrent_layers.md) a design with one has.

## The design

```
 host program ──AXI──▶ axi_crossbar ──▶ adaptor ────────────────────────▶ mm_fir kernel
                                        register bank  0x0000   s_cfg  ◀── one FirCfg per COMMIT
                                                                m_status ──▶ FirStatus (latest value)
                                        queue in       0x1000   s_in   ◀── FirCmdHdr | samples, per packet
                                        queue out      0x2000   m_out  ──▶ results
                                        queue out      0x3000   m_resp ──▶ one FirRespHdr per packet
```

| address | view | the host | the kernel |
|---|---|---|---|
| `0x0000` | register bank | sends each `FirCfg` (the shadow, then COMMIT); reads `FirStatus` | takes `FirCfg` messages when a packet asks for them; pushes a `FirStatus` |
| `0x1000` | queue in | sends each packet as a `FirCmdHdr`, then its samples | reads a header, then that many samples |
| `0x2000` | queue out | takes the results | writes one result per sample |
| `0x3000` | queue out (responses) | takes one `FirRespHdr` per packet and checks it | writes one `FirRespHdr` per packet |

The host does all of this through [endpoints](../../guide/interface/axi_mm/slave.md#reaching-the-views-from-a-bus-master)
and never names an address; the addresses above are where the map puts the views.

The filter is an exact integer FIR: int16 samples (packed four to a 64-bit word by the serializer),
up to 16 int16 taps, and the full sum as an int64. Nothing rounds, so the numpy golden is bit-exact by construction and any
mismatch is a real bug.

## Why taps are registers and samples are a queue

They have different semantics, and the adaptor gives each the one it needs:

- **Taps are configuration.** The kernel must never filter with half of an old tap set and half of a
  new one, so the register bank is **shadow-and-commit**: the host writes the whole `FirCfg` into a
  shadow, and a write to COMMIT sends it to the kernel as **one message**.
- **Samples are a stream.** They arrive continuously, in order, and the host must not overrun the
  kernel — a queue gives back-pressure, which the host's endpoint waits on by reading the free space.
- **Responses are a stream too.** One per packet, in order, and none may be lost — so they are a
  second queue, not a register.
- **Status is latest-value.** The kernel publishes how many samples it has filtered and how many
  configs it has taken. The host reads the most recent; nothing queues up.

## The one subtle part: switching taps mid-stream

A config travels on one stream and the samples on another. Even when the host sends the config
first, **nothing guarantees the kernel sees it first** — two streams have no order between them (the
slave page's [ordering statement 2](../../guide/interface/axi_mm/slave.md#ordering)). If the
protocol were "the new taps apply from whenever they arrive", the output would depend on timing.

So the order is carried **in the messages**, with a sequence number.

**Configs are numbered by when they are committed.** Config 1 is the first COMMIT, config 2 the
second, and so on. Nothing is added to the config itself: both ends count.

**Every packet names the config it needs.** In front of its samples, on the same stream, each packet
carries a one-word header:

```text
FirCmdHdr(nsamp, tx_id, cfg_seq) | x[0] ... x[nsamp-1]
```

**The kernel waits for that config.** Per packet it reads the header, then takes configs from the
register bank until it has taken `cfg_seq` of them, then filters the samples. That rules out both
ways the order could go wrong:

- **a packet cannot use an older config** than the one it names — the kernel waits until it has
  arrived;
- **a packet cannot use a newer one** — a config no packet has asked for yet stays where it is, in
  the stream from the register bank.

So the host commits a config and sends the packets that need it, in either order, and never asks
whether the config arrived. The test suite commits the second config 32 samples *after* the packets
that need it, and the output is still exact: those packets wait in queue in until it lands.

**Waiting costs nothing on the bus.** The kernel waits on its own stream from the register bank,
which sits beside it; it never touches the bus. While it waits it takes no samples, so queue in fills
and the host's writer waits for room — asleep on queue in's interrupt, which fires when the kernel
has drained enough. Nothing polls.

**The response FIFO lets the host check.** After each packet the kernel writes a `FirRespHdr` to a
second queue out: the packet's `tx_id` and the `cfg_seq` it was actually filtered with. The host
compares each response with the config it *meant* the packet to use. The wait makes a wrong config
impossible from the kernel's side; the echo catches the host's side — a packet tagged with the wrong
number. The negative control is exactly that: a host that tags every packet with config 1 while
meaning config 2 after the switch. The kernel obeys the tag, the output does not match the plan, and
every response after the switch is flagged.

**The limits, stated:**

- **One config outstanding.** The register bank holds one config the kernel has not taken; a second
  COMMIT waits on the bus until the first is taken. So the host commits config *k+1* only after a
  packet tagged *k* has gone out.
- **Packets sent ahead of their config must fit in queue in.** Otherwise the host waits for room
  while the kernel waits for the config.
- **A config that is never sent is waited for forever.** The kernel stops, and the host can see it:
  the status's `nsamp` stops advancing.

An earlier version of this example carried the order differently — each config named the sample index
it applied at, and the host polled the status until the config showed as received before sending that
sample. It worked, but it cost a status round trip per config and was not the in-band header pattern
the other examples use.

## What it demonstrates

- A kernel reached through registers and queues, with **no** change to how a kernel is written.
- The adaptor's views behind the real AMD crossbar, gated bit-exact at RTL in two shapes: one view
  per crossbar slot (618 cycles), and all four views behind one front with a generated decoder
  (611 cycles).
- **No polling anywhere.** The host sleeps on the queue views' interrupts — queue in's for room, queue
  out's and the response FIFO's for data — and reads the final status once, because the kernel
  publishes it before each response. Tests check it at both levels: in pysim and at RTL, the host
  never reads a count.
- One host program, holding endpoints and never an address, run over the bus in pysim, joined
  directly to the kernel in pysim, and — written against the C++ twins of the same endpoints — over
  the bus at RTL.
- A cross-stream protocol made deterministic by carrying the order in the messages — a config
  sequence number in the packet header — and checked end to end by a response FIFO.
- A hand-written HLS body that had to be restructured to pipeline: from ~1 sample per 10 cycles to 1
  per cycle, by moving at most one word per stream per firing.

## Pages

- [Python model](python.md) — the schemas, the kernel's `run_iter`, the host program, the golden.
- [Python simulation](pysim.md) — the system wired in pysim, the tap switch, the negative control, and how
  close pysim's timing is.
- [Code generation](codegen.md) — the generated top and headers, the hand-written HLS body, and why
  it had to change to pipeline.
- [RTL simulation](rtlsim.md) — the RTL top (crossbar, adaptor, kernel), the C++ host program, the two
  topologies, and the results.

The code is in [`examples/mm_fir`](../../../examples/mm_fir).
