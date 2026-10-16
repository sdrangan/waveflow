---
title: Vitis L1 Blocks
parent: Guide
nav_order: 10.75
has_children: true
audience: python
api: [VitisFft]
summary: "Waveflow HwModules that wrap AMD Vitis Libraries vendor IP, starting with the SSR FFT. What the L1 / L2 / L3 levels of the Vitis Libraries are and which one a Waveflow design uses; the three things a vendor block needs to be an ordinary Waveflow module (a bit-exact Python model, the vendor call as its task body, and timing and resources calibrated on a platform); and which blocks exist today."
---

# Vitis L1 Blocks

A Vitis Libraries block is vendor IP: AMD wrote the HLS, and a Waveflow design wants to
*instantiate* it rather than reimplement it. That raises a problem Waveflow's usual story does not
answer. A Waveflow module is normally twins -- a Python `run_iter` that simulates and a generated C++
body that synthesizes -- and for vendor IP there is no generator that can write
`xf::dsp::fft::fft<>` from Python.

## L1, L2, L3

The Vitis Libraries ship each function at up to three levels. The levels are about how much of a
deployable design you are handed, not about the arithmetic:

| level | what it is | interface | who calls it |
|---|---|---|---|
| **L1** | HLS primitives: C++ templates called inside *your* kernel | whatever suits a building block -- usually `hls::stream`s | your HLS code |
| **L2** | complete kernels, ready for `v++` and a platform | AXI-MM (`m_axi`) to DDR, AXI-Lite control | host software, through XRT |
| **L3** | host APIs over one or more L2 kernels | a library call | an application |

Waveflow wraps **L1**: it is the building block, and it reaches every system target -- inside a
Waveflow composite, behind a `MemSlaveAdaptor`, or as a stream-only kernel in an XRT design with
Waveflow's own data movers. An L2 kernel is the right choice only when the block *is* the whole
accelerator and its data already sits in DDR; for the FFT, L2 wraps the identical L1 core in AXI-MM
plumbing and adds no arithmetic, so it needs no second model.

## What makes a vendor block a Waveflow module

Three things, each supplied by `waveflow.vitis_l1`:

| | supplied by | so that |
|---|---|---|
| **the bits** | a bit-exact Python model of the vendor's arithmetic (`waveflow.vitis_l1.fft`), gated against goldens produced by the vendor's own code | the pysim and the RTL are twins, not approximations |
| **the hardware** | the module's `kernel_task()` names a hand-written body that calls the vendor template, copied into the build; the vendor headers stay in the Vitis install | the generated free-running top instantiates the vendor IP like any other task |
| **the timing and resources** | calibrated at RTL on a **platform** (part + clock) by a registered fixture, and reloaded by every design on that platform | the pysim predicts *when* as well as *what*, with a stated error |

## The blocks

| block | module | status |
|---|---|---|
| SSR FFT (`xf::dsp::fft::fft<>`) | [`VitisFft`](fft/index.md) | bit-exact model, module, synthesis, RTL gates, calibrated on the RFSoC 4x2 |
| `gemv` (BLAS L1) | -- | bit-exact model only (`waveflow.vitis_l1.gemv`); the module is future work |

The worked example is [Wrapping vendor IP: the Vitis FFT](../../examples/vitis_fft/index.md).
