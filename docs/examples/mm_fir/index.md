---
title: A memory-mapped FIR
parent: Examples
nav_order: 9.55
has_children: true
example_dir: examples/mm_fir
summary: "The first kernel that is REACHED over the bus rather than driving it. A free-running FIR whose taps are a register bank and whose samples and results are queues, all behind an AXI slave port that a host program writes and reads through AMD's crossbar. The kernel stays stream-only; a memory-mapped adaptor in the RTL top turns bus transactions into its messages. Because a config and the samples travel on different streams, the order between them is carried in the messages: each config says the sample it applies at, and the host waits for it to be received. Bit-exact against an exact-integer golden in pysim and at RTL, in two adaptor topologies."
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
                                        queue in       0x1000   s_in   ◀── samples
                                        queue out      0x2000   m_out  ──▶ results
```

| address | view | the host | the kernel |
|---|---|---|---|
| `0x0000` | register bank | writes a `FirCfg` into the shadow, then writes COMMIT at `0x0800`; reads `FirStatus` at `0x0C00` | reads one `FirCfg` message per commit; pushes a `FirStatus` |
| `0x1000` | queue in | checks the vacancy, then writes packets `[len \| samples]` | reads samples |
| `0x2000` | queue out | checks the occupancy at `0x2800`, then pops results | writes one result per sample |

The filter is an exact integer FIR: int16 samples (one per 64-bit word), up to 16 int16 taps, and the
full sum as an int64. Nothing rounds, so the numpy golden is bit-exact by construction and any
mismatch is a real bug.

## Why taps are registers and samples are a queue

They have different semantics, and the adaptor gives each the one it needs:

- **Taps are configuration.** The kernel must never filter with half of an old tap set and half of a
  new one, so the register bank is **shadow-and-commit**: the host writes the whole `FirCfg` into a
  shadow, and a write to COMMIT sends it to the kernel as **one message**.
- **Samples are a stream.** They arrive continuously, in order, and the host must not overrun the
  kernel — a queue gives back-pressure (a full queue stalls the host's write) and a vacancy register
  the host can check first.
- **Status is latest-value.** The kernel publishes how many samples it has filtered, how many configs
  it has received, and how many arrived late. The host reads the most recent; nothing queues up.

## The one subtle part: switching taps mid-stream

A config travels on one stream and the samples on another. Even when the host writes the config
first, **nothing guarantees the kernel reads it first** — two streams have no order between them. If
the protocol were "the new taps apply from whenever they arrive", the output would depend on timing.

So the order is carried **in the message**:

1. Every `FirCfg` names `apply_at`: the index of the first sample it filters.
2. The kernel holds a received config as *pending* and switches exactly at `apply_at`.
3. The host does not send sample `apply_at` until the status shows the config **received**.
4. A config that arrives after its sample has already been filtered is put in force immediately and
   counted in `late` — detected, never silently misapplied.

The kernel checks for a config before every sample, and also when it has no samples, so the host's
step 3 cannot deadlock. A negative control — a host that commits 32 samples late — shows `late = 1`
and an output equal to "switched where it arrived", not to the plan.

## What it demonstrates

- A kernel reached through registers and queues, with **no** change to how a kernel is written.
- The adaptor's views behind the real AMD crossbar, gated bit-exact at RTL in two shapes: one view
  per crossbar slot (811 cycles), and all three views behind one front with a generated decoder
  (776 cycles).
- One host program, holding endpoints and never an address, run over the bus in pysim, joined
  directly to the kernel in pysim, and — written against the C++ twins of the same endpoints — over
  the bus at RTL.
- A cross-stream protocol made deterministic by carrying the order in the messages.
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
