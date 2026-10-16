---
title: DSP Blocks
parent: Guide
nav_order: 10.8
has_children: true
audience: python
api: [SsrFft]
summary: "Waveflow's own DSP blocks, written as free-running Waveflow modules rather than wrapped vendor IP: today the full-rate SSR FFT, bit-exact with AMD's Vitis L1 FFT and ten times its throughput as shipped."
---

# DSP Blocks

[Vitis L1 Blocks](../vitis_l1/index.md) wraps AMD's library blocks as they ship. This section is the
other route: blocks written in Waveflow -- a bit-exact Python model, one free-running `hls::task` per
piece of hardware, a composite module that generates its own top -- which take what is good from a
vendor library and rebuild what is not.

- [The full-rate SSR FFT](ssr_fft/index.md) -- `SsrFft`: the Vitis FFT's arithmetic, bit for bit, at a new
  frame every `L/R` cycles.
