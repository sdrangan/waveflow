---
name: waveflow-architecture
description: The big picture of a Waveflow design, and how to map an algorithm onto it -- a system is a testbench around a DUT; the DUT is devices, memories and the interconnect (streams and a crossbar); a device is kernels plus the adaptors that join them to memory, a host's bus or the RF converters; a kernel works on streams only, a leaf or a composite of child kernels; messages are typed and command-response; golden, pysim and RTL check each other. Use whenever someone asks how they would build something in Waveflow (a receiver block, a detector, a filter, an accelerator), how a Waveflow design is organized, or what a kernel, device, adaptor or system is, before any code.
---

# Waveflow architecture: the big picture, and an algorithm mapped onto it

Every other `waveflow-*` skill covers one piece. This one is the map they fit on. When someone asks
"how would I build X in Waveflow", answer **in chat, in a minute or two**: X drawn in Waveflow's terms
-- which kernels, which adaptors, which devices, how they are joined -- and which piece to build first
with which skill. Then stop: build only when the person asks. Even when the request says "build", this
answer comes first.

## The picture

```text
 testbench (derived: drivers, sinks, memory models, a host program and its C++ twin)
 ┌──────────────────────────────── DUT ───────────────────────────────────────┐
 │  device A                          device B                       memory    │
 │  ┌──────────────────────┐ stream  ┌──────────────────────────┐   ┌───────┐  │
 │  │ adaptor ─ kernel(s)  │────────▶│ kernel(s) ─ adaptor      │   │ RAM   │  │
 │  └──────────┬───────────┘         └──────────────┬───────────┘   └───┬───┘  │
 │             └──────────── crossbar (AXI-MM, an address map) ─────────┘      │
 └─────────────────────────────────────────────────────────────────────────────┘
```

| level | what it is | page |
|---|---|---|
| **system** | `HwSystem(sim, dut)`: the DUT and a testbench derived around it (only what has an RTL twin); in pysim, a `Simulation` that can hold anything else too (a channel model, a processor model, traffic) | `systems/index.md` |
| **DUT** | devices, memories (`MemoryMod`) and the interconnect: **streams** between devices, and a **crossbar** when a host or a memory is reached by address | `systems/devices.md` |
| **device** | one or more kernels plus their **adaptors**: one sub-top in the Verilog, one IP | `systems/devices.md` |
| **adaptor** | streams on one side, something else on the other: a memory reader / writer (`MemRStream` / `MemWStream`, `m_axi`); a slave adaptor whose views a host reaches (`QueueIn`, `QueueOut`, `RegBank`, interrupts); a credit stream's bus writer; the RF converters (`Rfdc`) | `systems/devices.md` |
| **kernel** | a `FreeRunMod` whose interfaces are **all streams**: a **leaf** (a Python `run_iter`, one firing per message, and an HLS body) or a **composite** (child kernels joined by internal streams, a pipeline) | `systems/kernels.md` |
| **message** | a `DataSchema`; on every stream a command carrying its size and a `tx_id`, the results headed by a response, a status per command (command-response) | `systems/command_response.md` |
| **software** | a host program on the bus (`SwHost`, with a C++ twin for RTL), or a calibrated processor (`Processor`) for control and rare paths | `systems/rtl.md`, `cpu/index.md` |

Three levels check each other: the **golden** (pure Python functions, bit-exact, untimed), **pysim**
(the modules in Python, timed; `run_iter` calls the golden) and **RTL** (the HLS bodies, cycle by cycle,
compared with pysim word for word). Any module can be the DUT of its own harness, so each kernel and
device is tested alone before the whole.

## Mapping an algorithm onto it

Answer these, in order, briefly:

1. **The stages and where the work is.** The algorithm's steps in a line, and what collapses the work
   (shared factors, something computed once per block and applied per sample, a convolution done by
   FFT). One rate check (cycles available a job against cycles needed) and, if something is stored, one
   memory check -- each number with its source.
2. **The kernels.** Each stage a leaf kernel, or several stages fused into one when they are simple and
   at the same rate. Stages that must run concurrently at different rates, or that both need one input,
   make a **composite** (`waveflow-pipeline`). Use a library kernel only where it is hard to build well
   -- an FFT (`SsrFft`), a large complex matrix product (`SystolicEngine`) -- and write simple ones
   (element-wise, magnitude, peak search, reductions) yourself (`systems/library.md`). If the best map
   needs a block or an option Waveflow does not have yet (a stream arbiter, an FFT whose direction
   changes per frame), name it as an extension to build -- Waveflow is early and meant to grow --
   rather than bending the map around the gap.
3. **What each kernel talks to**, and so its adaptors: samples from the ADC (`Rfdc`) or from a buffer in
   memory (a memory reader); results to memory (a memory writer, "done" when it echoes) or to a host
   (a queue out, an interrupt); configuration from a host (a register bank, or a queue in for commands);
   another kernel (a stream). The table in `systems/kernels.md` gives the stream for each.
4. **The devices.** Group each kernel with its adaptors; kernels with no adaptor between them can share
   a device. Say which device is reached by a host, which reads memory, which touches the converters.
5. **The system.** The devices, any memory, the crossbar if anything is reached by address, the host
   or processor, and the testbench's stand-ins. Name the closest frame (`waveflow_list_frames`) and the
   closest worked example.
6. **The order to build it, and the skill for each piece**: the messages (`waveflow-schema`), each
   kernel in Python and its harness test (`waveflow-kernel`), its HLS body (`waveflow-kernel-hls`), a
   composite (`waveflow-pipeline`), a device's adaptors (`waveflow-device`), the system and its tests
   (`waveflow-system`). Bottom-up: the golden first, each module alone, then the whole.

End with two or three open questions that change the map (the rate, what feeds it, who configures it).
Keep it to about a screen; read only what a number or a name needs.

## The shape of an answer

> **Stages.** Range FFT per chirp, corner turn, Doppler FFT per range bin, `|·|²`, CFAR. One FFT of each
> keeps up: 256 cycles a chirp at `L/R` (`dsp/ssr_fft/timing.md`) against 500 available.
> **Kernels.** `SsrFft` L=1024 and L=256 (library); `power_cfar`, a leaf kernel of mine (`|·|²` and CFAR
> fused). The corner turn is a memory, not a kernel.
> **Adaptors and devices.** Device 1: `Rfdc` → range FFT → memory writer (chirps into the corner-turn
> buffer). Device 2: memory reader (by range bin) → Doppler FFT → `power_cfar` → queue out to the host,
> an interrupt per frame. The host sets the threshold through a register bank on device 2.
> **System.** Two devices, one memory, a crossbar, a `SwHost`; closest frame `bus_system`.
> **Order.** Schemas; the golden; `power_cfar` alone; each device in its harness; the system.
> **Open.** Chirp period; whether detections need angle (a third FFT across antennas).

## Before you answer, check

- [ ] Every block is named as a kernel (leaf or composite), an adaptor, a device or a memory.
- [ ] Each kernel's interfaces are streams; whatever is not a stream is reached through an adaptor.
- [ ] Library kernels only where they are hard to build; simple stages fused and written.
- [ ] A missing block or option the best map needs is named as an extension, not designed around.
- [ ] One rate check, each number with its source.
- [ ] The frame, the closest example, the build order and the skill for each piece are named.
- [ ] A chat answer of about a screen: no files, no code.
