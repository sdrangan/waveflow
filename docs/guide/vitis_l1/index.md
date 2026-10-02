---
title: Vitis L1 Blocks
parent: Guide
nav_order: 10.75
has_children: true
audience: python
api: [VitisFft, ScalingMode, OutputOrder]
snippets: run
summary: "Waveflow HwModules that wrap AMD Vitis L1 vendor IP, starting with the SSR FFT. The block simulates through a bit-exact Python model of the vendor library, so the pysim and the RTL are twins rather than approximations, and synthesizes into a design that calls xf::dsp::fft::fft<> itself. Covers the R-wide AXI-Stream port group, which parameters bind at build time and why the scaling enums cannot be HwParams, how the output width is derived rather than declared, and why the module refuses configurations the model does not cover."
---

# Vitis L1 Blocks

A Vitis L1 block is vendor IP: AMD wrote the HLS, and a Waveflow design wants to *instantiate* it
rather than reimplement it. That raises a problem Waveflow's usual story does not answer. A
Waveflow module is normally twins — a Python `run_iter` that simulates and a generated C++ body
that synthesizes — and for vendor IP there is no generator that can write
`xf::dsp::fft::fft<>` from Python.

`waveflow/vitis_l1/` closes that by supplying the missing half: a Python model that is **bit-exact**
against the vendor library, gated in `tests/vitis_l1/`. The modules in `waveflow/vitis_l1/hw.py`
are the part a design instantiates — the vendor block's interface, simulating through that model,
and synthesizing by handing the vendor call over as the task body.

| | |
|---|---|
| the bits | `waveflow.vitis_l1.fft`, bit-exact against the library |
| the module | `VitisFft` — ports, parameters, `run_iter`, `kernel_task` |
| the C++ | `waveflow/build/vitis_fft_task.h`, copied in, not generated |
| the evidence | `tests/vitis_l1/fft/`, including a co-simulated RTL run |

## The port group is `R` wide

The DUT AMD ships is

```cpp
void fft_top(hls::stream<T_in> p_in[FFT_R], hls::stream<T_out> p_out[FFT_R]);
```

`R` is the butterfly radix, the SSR factor **and** the port count. In Waveflow that is an `R`-wide
AXI-Stream port group — `R` slaves in and `R` masters out — not one port carrying an `R`-element
word. Sample `n` travels on port `n % R` at time `n // R`, on both sides, which is the layout the
goldens record as `stream_layout`.

Each word carries one complex sample, with the **real part in the low bits**, matching
`std::complex`'s first member. That is not a convention chosen for tidiness: the same packing is
used by the C++ body, and co-simulation reproduces the vendor golden through it, so it is measured
rather than asserted.

```python
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation
from waveflow.vitis_l1.hw import VitisFft

fft = VitisFft(name="fft", sim=Simulation(), clk=Clock(freq=100e6), L=1024)
print("ports per side:", len(fft.s_in), "in,", len(fft.m_out), "out")
print("input word bits:", fft.s_in[0].bitwidth)
print("output word bits:", fft.m_out[0].bitwidth)
print("output format: ap_fixed<%d,%d>" % (fft.out_fmt.W, fft.out_fmt.int_bits))
print("stages:", fft.n_stages)
```

```text
ports per side: 4 in, 4 out
input word bits: 32
output word bits: 54
output format: ap_fixed<27,13>
stages: 5
```

## The output type is not the input type

It widens with `L`, by the library's own `OUTPUT_WL = in_W + log2(L) + 1`. `VitisFft` **derives**
it by asking the model what format it produced, rather than restating the formula — so changing
`L` cannot leave the port at a stale width. The C++ body then `static_assert`s the same number
against the vendor's own `ssr_fft_output_type`, which turns the derivation into a compile-time
check: a wrong width cannot reach synthesis.

Do not reach for `stage_formats(...)[-1][1]` instead. That is the last stage's tree output, one bit
*wider* than the port, because the library applies a final narrowing cast — 22 against 21 at
`L=16`.

## Which parameters bind when

`L` is fixed at build time. It is `ssr_fft_param_struct::N`, a `static const int` consumed as a
template argument, so two lengths are two bitstreams. There is no runtime `N`.

| | binding | why |
|---|---|---|
| `L`, `R`, `in_w`, `in_i`, `tw_w`, `tw_i` | `HwParam[int]` | C++ template arguments; distinct values are distinct artifacts |
| `scaling_mode`, `output_order` | plain field holding an `IntEnum` | still template arguments — see below |
| `latency_cycles`, `ii_cycles` | plain field, no default | timing, not structure; see [Latency and II for a vendor block](timing.md) |

**The enums are not `HwParam`s, and that is mechanical rather than a judgement.**
`HwModule.__post_init__` rewrites every `HwParam` value as `HwParamValue(int(value))`, so an enum
member would be flattened to a bare int and lose the name `run_iter` needs to pick the model's
mode. They remain template arguments; `kernel_task()` casts with `int(...)` explicitly. This is the
same constraint that made `Rfdc.word` a plain field.

## It refuses what the model does not cover

A loud refusal is the feature here. Silently accepting a configuration the model does not cover
would produce confident wrong bits, which is the one outcome worth engineering against.

```python
for kwargs in ({"L": 32}, {"R": 8}):
    try:
        VitisFft(name="bad", sim=Simulation(), clk=Clock(freq=100e6), **kwargs)
    except (ValueError, NotImplementedError) as exc:
        print(type(exc).__name__ + ":", str(exc).split(".")[0])
```

```text
ValueError: L=32 is not a power of R=4
NotImplementedError: VitisFft: only R=4 is modelled (got R=8)
```

Refused today: radix other than 4, lengths that are not a power of the radix (32, 128, 512 take a
different "forked" architecture in the library), output orders other than natural, and scaling
modes beyond `NO_SCALING` above `L=16`. All three scaling modes *are* available at `L=16`, where
the model validates them — `SCALE` holds the width at 16 while the other two widen to 21.

## Where to go next

* [Synthesizing a Vitis L1 block](synthesis.md) — how the vendor call reaches a build, the include
  path it needs, and the co-simulated RTL result.
* [Latency and II for a vendor block](timing.md) — why bits are not enough, and what the hardware
  measured.
