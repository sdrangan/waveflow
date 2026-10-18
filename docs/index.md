---
title: Waveflow
nav_order: 1
has_children: true
---

# Waveflow

**From waveform to silicon — in Python.**

Waveflow is a Python-native framework for **algorithm–hardware co-design**: describe your
datapath once in Python, simulate it *fast* and *bit-exact*, and generate its hardware
implementation from the same source. Its flagship domain is **wireless**, but it fits any
project for efficiently realizing complex signal-processing algorithms in hardware (FPGA or
ASIC).

<p align="center">
  <img src="./assets/waveflow_hero.svg" width="760"
       alt="Waveflow flow: Python model to Python sim to HLS codegen to RTL synth/sim, with a Design, verify, calibrate, iterate loop and AI assisting codegen and iteration">
</p>

One structured Python representation is the **single source of truth** — simulation, synthesis,
software bindings, and documentation all derive from it, instead of drifting apart across
notebooks, HDL fragments, and build glue.

> *Waveflow is the substrate that makes AI effective for hardware — fast to simulate, structured
> so generation stays local and contract-guided, reproducible to build, and bit-exact to verify.*

## Start here

- **[Overview](./overview/)** — the motivation, what makes Waveflow different, and the flow
- **[Project status](./overview/status.md)** — what works today, what's next, and the first integrated milestone
- **[Installation](./guide/installation/)** — install Waveflow from source
- **[Guide](./guide/)** — systems first: the [Flow Overview](./guide/systems/index.md) builds one system a page per step (the DUT, pysim, RTL simulation, verification, calibration, exploration), module by module, with the references beneath it
- **[Examples](./examples/)** — worked designs, starting with [a free-running polynomial](./examples/stream_kernel/): the command-response kernel, its harness, its calibrated timing, and the same kernel behind AXI-MM

## People

Waveflow is developed by
[Sundeep Rangan](https://wireless.engineering.nyu.edu/sundeep-rangan/), Professor of Electrical
and Computer Engineering and Director of [NYU Wireless](https://wireless.engineering.nyu.edu/)
at NYU.

## Support

This project is generously supported by:

- [NTIA NOFO-2](https://www.ntia.gov/press-release/2025/biden-harris-administration-awards-117-million-wireless-innovation) award on Spectrally Agile Large Scale Arrays with NYU, Rutgers, Pi-Radio, Nokia, and Princeton University.
- [NSF NeTS Medium: Energy-Efficient and Reconfigurable Wireless Networks through Hardware-Algorithm Co-Design](https://www.nsf.gov/awardsearch/show-award?AWD_ID=2504400) with NYU PIs Sundeep Rangan, Brandon Reagen, Elza Erkip, and Hamed Rahmani.
- The industrial affiliates of [NYU Wireless](https://wireless.engineering.nyu.edu/).

## Feedback

Waveflow is an early-stage research project and the ideas are still evolving. Feedback is very
welcome — especially from people working in accelerator design, wireless and DSP systems,
hardware/software co-design, simulation and verification, and AI-assisted engineering tools.
