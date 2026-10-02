---
title: AXI-MM
parent: Interfaces
nav_order: 4
has_children: true
audience: python
summary: "Memory-mapped access in Waveflow: one Python model of AXI-MM traffic (MMIFMaster / MMIFSlave endpoints, joined directly or through a crossbar) used unchanged by both flows. What differs between the flows is how each endpoint is realized, and that is decided by what Vitis HLS can generate: an AXI master always, an AXI-Lite register file usable only by a host-activated kernel, an AXI4-full slave never. A free-running kernel's own logic sees only streams, so it reaches the bus through adaptors — a master adaptor that is a kernel module, and a slave adaptor that is an RTL module."
---

# AXI-MM

AXI-MM is how hardware is reached by address: a host writing a kernel's registers, a kernel reading
a buffer in DDR, one kernel filling another's queue. This section covers all of it, in both flows.

## One model, both flows

In Python there is **one** model of memory-mapped traffic, and both flows use it unchanged:

- an **`MMIFMaster`** issues reads and writes — in bursts, typed or raw;
- an **`MMIFSlave`** answers them, through callbacks a module supplies;
- they are joined either one-to-one by a **`DirectMMIF`**, or many-to-many by an **`AXIMMCrossBarIF`**
  that routes each transaction by address.

[Modeling memory-mapped traffic](./modeling.md) is that model in detail — endpoints, interconnects,
the latency model.

## What decides the realization: what Vitis HLS can generate

What differs between the flows is how each endpoint becomes hardware, and that comes down to what
Vitis HLS can generate for a kernel:

| | Vitis HLS can generate it? | host-activated kernel | free-running kernel |
|---|---|---|---|
| an **AXI master** (`m_axi`) | yes | the kernel's own `MMIFMaster` becomes an `m_axi` port its body reads and writes | yes, but only inside a **master adaptor** — `MemRStream` / `MemWStream` — never in the kernel's own logic |
| an **AXI-Lite register file** (`s_axilite`) | yes | the kernel's [register map](./regmap.md) | unusable: a free-running kernel cannot see a write happen |
| an **AXI4-full slave** | **no** | — | a **slave adaptor** — hand-written Verilog beside the kernel |

Everything around the kernel is the same in both flows: a memory is a `MemoryMod` (an `MMIFSlave`), a
host is a module holding an `MMIFMaster`, and the interconnect is a `DirectMMIF` or an
`AXIMMCrossBarIF`. At RTL, in an [XSI simulation](../../flows/concurrent_layers.md), the memory and the
host become C++ BFMs and the crossbar becomes AMD's `axi_crossbar` IP.

## A free-running kernel reaches the bus through adaptors

A free-running kernel's own logic sees **only streams**. Every synchronization it takes part in is a
stream message — that is what keeps the order of events in a design of concurrent tasks defined. So
whenever it touches AXI-MM, an **adaptor** sits between it and the bus, converting stream messages to
bus transactions or back:

```
  kernel ──stream (commands, data)──▶ master adaptor ──m_axi──▶ ┐
  (stream-only)                       MemRStream / MemWStream   │
                                      a KERNEL MODULE           ├── crossbar ── memory, other slaves
                                                                │
  kernel ◀──stream (messages)──────── slave adaptor  ◀──AXI──── ┘ ◀── host, other masters
                                      queues · register bank · BRAM window
                                      an RTL MODULE
```

The two adaptors sit in different places for one reason — what HLS can generate:

- **The master adaptor is a kernel module.** HLS generates `m_axi` ports, so `MemRStream` and
  `MemWStream` are ordinary tasks inside the Vitis kernel, each owning one port and turning a command
  stream into AXI bursts. See [Master side](./master.md).
- **The slave adaptor is an RTL module.** HLS cannot generate an AXI4-full slave, so the slave side is
  hand-written Verilog in the RTL top, turning bus transactions into the stream messages the kernel
  reads. See [Slave side](./slave.md).

Either way the kernel is written the same way: it reads and writes streams, and never an address.

## Pages

- [Modeling memory-mapped traffic](./modeling.md) — `MMIFMaster` / `MMIFSlave`, `DirectMMIF`,
  `AXIMMCrossBarIF`, the latency model; the model both flows share.
- [Master side — streaming memory kernels](./master.md) — `MemRStream` / `MemWStream`: how a
  free-running kernel reads and writes memory.
- [Slave side — memory-mapped adaptor](./slave.md) — queues, a register bank and a BRAM window behind
  one AXI slave port: how a free-running kernel is reached, and the ordering guarantee.
- [Register Maps](./regmap.md) — the AXI-Lite register file of a host-activated kernel.
