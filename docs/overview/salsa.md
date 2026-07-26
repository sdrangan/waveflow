---
title: "SALSA: the motivating system"
parent: Overview
nav_order: 6
---

# SALSA — the system Waveflow was built for

Emerging wireless systems face two demands at once: **massive-scale MIMO** — fully-digital,
high-dimensional antenna arrays for beamforming, interference coordination, and high-resolution
sensing — and **spectral agility** — the ability to rapidly change the frequency of operation and
reconfigure processing for joint communication and sensing, resiliency, and dynamic spectrum use.

**SALSA** (Spectrally Agile Large-Scale Arrays) is a large NTIA NOFO-2 project at NYU developing
massive (1000+ element) antenna systems for future wireless. A central focus is the emerging
[**FR3** band](https://ieeexplore.ieee.org/abstract/document/10459211/) (6–24 GHz), where cellular
systems must coordinate with incumbent satellite services. Large-scale *reconfigurable* platforms
like SALSA are also key to integrated sensing-and-communication (ISAC) and to the resiliency of
commercial and public systems against interference and hostile attack.

SALSA was the **actual motivation** for Waveflow — and it is exactly the kind of system that current
AI-assisted hardware tools cannot build reliably.

## Why SALSA is hard

SALSA represents a class of modern wireless architecture that combines **heterogeneous accelerators**,
**high-rate antenna interfaces**, and **dynamic dataflow reconfiguration**. That combination demands
three things existing tools do not provide together:

- **architectural exploration** — over the tile mix, the interconnect, and the dataflow graphs;
- **concurrency modeling** — many dataflows run *simultaneously* and reconfigure at runtime;
- **unified firmware generation** — the control that programs the dataflow must stay in lock-step
  with the hardware it drives.

And the reconfigurability problem made concrete: because SALSA's behavior is *runtime-programmable
and data-dependent*, timing and resource contention cannot be reasoned about statically. The only
way to see how a given task mix performs is to **simulate** it — fast, bit-exact, and timing-aware.

## The architecture

SALSA is a **tile-based wireless processor** — *not a fixed pipeline*, but a runtime-programmable dataflow
machine whose behavior changes with the wireless environment and the application. It is organized in three
levels around a shared bus:

![SALSA system architecture: RF tiles, interconnected distributed tiles, common (multi-user / MIMO) tiles, and separate control / LDPC / upper-layer processing around a shared bus](./figures/salsa_system.svg)

- **RF tiles** sit at the antennas — each drives the RF front-end for a group of the antenna elements,
  exchanging that group's IQ streams with the layer below.
- **Distributed tiles** do the per-antenna or per-user signal processing. Crucially they connect **to their neighbors**
  (left/right) as well as up to the RF tiles and down to the bus — this neighbor-to-neighbor mesh is what lets
  array-wide operations (beamforming, angle-of-arrival, nulling) span tiles.
- **Common tiles** perform multi-user processing, including certain MIMO matrix operations.

LDPC decoders, physical-layer control, and upper-layer processing are handled outside the SALSA tile array, since they are better suited to specialized hardware or more general-purpose DSPs and processors.

Each distributed tile is itself **heterogeneous** — FIR, FFT, and systolic-array processing elements plus
general-purpose processor cores, sharing the tile's memory and fed by **serial interfaces** (up to the RF
tiles, left/right to neighbor tiles, and down to the bus):

![A distributed tile: FIR / FFT / systolic-array PEs and processor cores behind serial and bus/memory interfaces](./figures/salsa_tile.svg)


## Dynamic Task Reconfiguration

Dynamic processing is configured in **flows** in an architecture that builds on the team's work in the [DARPA TRACER project](https://ieeexplore.ieee.org/abstract/document/11310646).  Specifically, computation is performed in flows, with each flow being a sequence of atomic operations or **tasklets**.  For example, a flow could consist of tasklets such as channelization filtering, FFT, spatial covariance estimation, and an eigenvector-based AoA estimation that could be activated, for example, after a signal of interest and its bandwidth are identified.
Each tasklet in a flow is run on a PE specific to the type of processing and exchanges messages and data with other tasklets on other PEs over the interconnect fabric and shared memory.  Importantly, PEs may be assigned multiple tasklets and have a job queue and micro-scheduler for selecting tasklets to process.  
The flow configuration across this fabric is **workload-dependent**: each flow — communication, spectrum sensing,
angle-of-arrival, beam nulling, interference mitigation — lights up a different graph across the tiles and
PEs, and several run *concurrently*.

## Building SALSA with Waveflow

Waveflow is the single executable model for exactly this kind of system — each piece of SALSA maps
onto a Waveflow abstraction, and the whole thing simulates, verifies, and generates from one source:

- **Tiles and their PEs are `HwModule`s.** A distributed tile — and each processing element inside
  it (FIR, FFT, systolic, vector) — is a `HwModule` with typed interfaces (the **Serial I/F** and
  **Bus/memory I/F** of the figure above) and a compute hook: modeled in Python, simulated at NumPy
  speed, verified bit-exact against its golden, and lowered to HLS. The reconfigurable **vector-MAC
  engine (VMAC)** is the first PE through that flow — already bit-exact on real Vitis and
  throughput-characterized — a concrete SALSA building block, not a sketch.
- **The tasklet vocabulary maps straight onto Waveflow.** A *tasklet* is a compute hook on a PE
  `HwModule`; a *flow* is a sequence of tasklets wired through the typed interfaces; and a PE's
  *job queue + micro-scheduler* is an **AXI-MM command-queue interface** feeding the hook. So the one
  model that simulates a tasklet bit-exact also describes the queue that schedules it — one source for
  the compute *and* its dispatch.
- **The dataflow fabric is interfaces + concurrency.** Tiles connect through typed, transactional
  interfaces, and the simulator runs them as **truly concurrent processes**, so the *simultaneous,
  runtime-reconfigured* dataflows that define SALSA are modeled directly — something a sequential HLS
  testbench cannot express.
- **Architectural exploration is fast *and* honest.** Because the model is fast, bit-exact, *and*
  timing-aware, you can sweep the tile mix, the fabric, buffering, and the flow graphs in Python —
  the design-space exploration a runtime-programmable dataflow machine needs but static analysis
  can't deliver.
- **Firmware comes from the same source.** The control that programs the dataflow — the per-tile
  commands and the routing — is generated alongside the hardware from the one model, so the firmware
  and the silicon cannot drift.
- **Verification is end-to-end and bit-exact.** Every tile is checked against its Python golden on
  the real toolchain; the system is simulated against the very wireless algorithm it implements.

SALSA is a reconfigurable, concurrent, heterogeneous system whose **correctness**, **performance**,
and **firmware** must all be reasoned about *together* — and Waveflow keeps them in one fast,
bit-exact, executable model. VMAC is the first tile through that flow; the rest of SALSA follows the
same path.

## References
- French, Matthew, et al. "TRACER, the Next Generation Ultra-wideband Spectrum Processor." MILCOM 2025-2025 IEEE Military Communications Conference (MILCOM), 2025.
- Rasteh, A., Hennessee, A., Shivhare, I., Garg, S., Rangan, S., & Reagen, B. "A Spatial Array for Spectrally Agile Wireless Processing", IEEE Asilomar, 2025
- Rasteh, A., Kiani, A., Mezzavilla, M., & Rangan, S. "Scalable Long-Term Beamforming for Massive Multi-User MIMO," arXiv preprint arXiv:2511.09464, 2025
- Kang, S., Mezzavilla, M., Rangan, S., Madanayake, A., Venkatakrishnan, S. B., Hellbourg, G., ... & Dhananjay, A.  "Cellular wireless networks in the upper mid-band", IEEE Open Journal of the Communications Society, 5, 2058-2075, 2024