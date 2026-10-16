---
title: FFT parameters
parent: The Vitis FFT
grand_parent: Vitis L1 Blocks
nav_order: 1
audience: python
api: [VitisFft, ScalingMode, OutputOrder]
snippets: run
summary: "Constructing a VitisFft and every parameter it takes: the build-time HwParams that become C++ template arguments (L, R, the input and twiddle widths), the scaling mode and output order (template arguments too, but plain enum fields), and the timing fields (explicit cycles, or a calibrated platform). What it derives (the output format, the stage count, the ports) and what it refuses."
---

# Parameters

## Constructing one

```python
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation
from waveflow.vitis_l1.hw import VitisFft

fft = VitisFft(name="fft", sim=Simulation(), clk=Clock(freq=250e6), L=1024)
print("ports per side:", len(fft.s_in), "in,", len(fft.m_out), "out")
print("input word bits:", fft.s_in[0].bitwidth)
print("output word bits:", fft.m_out[0].bitwidth)
print("output format: ap_fixed<%d,%d>" % (fft.out_fmt.W, fft.out_fmt.int_bits))
print("stages:", fft.n_stages)
print("template args:", fft.kernel_task().template_args)
```

```text
ports per side: 4 in, 4 out
input word bits: 32
output word bits: 54
output format: ap_fixed<27,13>
stages: 5
template args: (1024, 16, 2, 18, 2, 0, 0, 27)
```

That module is **untimed**: it produces exact bits, in zero simulated time. To time it, point it at
a calibrated platform (or give the two delays explicitly):

```python
from waveflow.vitis_l1.testbench import default_platform_dir

timed = VitisFft(name="fft", sim=Simulation(), clk=Clock(freq=250e6), L=256,
                 platform_dir=default_platform_dir())
print("frames in flight:", timed.max_inflight)
```

```text
frames in flight: 1
```

## Every parameter

| parameter | type / binding | default | meaning |
|---|---|---|---|
| `name`, `sim` | | | as for every `HwModule` |
| `clk` | `Clock` | 100 MHz | the module's clock; must match the platform's (250 MHz for `rfsoc4x2_bfm_250mhz`) when timed from one |
| `L` | `HwParam[int]` | 1024 | transform length, a power of `R`; `ssr_fft_param_struct::N` |
| `R` | `HwParam[int]` | 4 | radix = super-sample rate = lanes per side; only 4 is supported |
| `in_w`, `in_i` | `HwParam[int]` | 16, 2 | input `ap_fixed<in_w, in_i>`, per real/imaginary part |
| `tw_w`, `tw_i` | `HwParam[int]` | 18, 2 | twiddle table `ap_fixed<tw_w, tw_i>` |
| `scaling_mode` | `ScalingMode` field | `NO_SCALING` | `NO_SCALING`, `GROW_TO_MAX_WIDTH` or `SCALE`; the last two only at `L = 16` |
| `output_order` | `OutputOrder` field | `NATURAL` | only `NATURAL` is modelled |
| `proc_cycles`, `ii_cycles` | `float` | `None` | explicit timing, as a pair: processing delay and frame interval, in cycles |
| `platform_dir` | path | `None` | a calibrated platform; supplies both delays for this `L` and width configuration |
| `require_calibrated` | `bool` | `True` | refuse a platform with no measurement for this configuration |
| `max_inflight` | `int` | `ceil(proc / ii)` | frames inside the block at once -- the back-pressure bound |

Give `proc_cycles`/`ii_cycles` **or** `platform_dir`, not both; give neither for an untimed module.
What the two delays mean, and how the platform's are measured, is
[Latency and II for a vendor block](timing.md).

**Binding.** `L`, `R` and the widths are `HwParam`s: C++ template arguments, so two values are two
artifacts. `scaling_mode` and `output_order` are template arguments too, but plain fields:
`HwModule` rewrites every `HwParam` value as `HwParamValue(int(value))`, which would flatten an enum
to a bare int and lose the name `run_iter` needs. `kernel_task()` casts them explicitly.

## What it derives

- **`out_fmt`**, the output format, by running the model -- so a changed `L` cannot leave a port at a
  stale width. Do not read `stage_formats(...)[-1][1]` instead: that is the last stage's tree output,
  one bit wider than the port, because the library casts down at the end (22 against 21 at `L=16`).
- **`n_stages`** = `log4 L`.
- **The ports**: `s_in` / `m_out` (lists) and `s_in_0..3` / `m_out_0..3` (attributes) -- see
  [Interfaces](interfaces.md).
- **`kernel_task()`**: the C++ body and its eight template arguments, in the order the body declares
  them: `L, IN_W, IN_I, TW_W, TW_I, SCALING, ORDER, OUT_W`. `R` is not one: a template cannot vary a
  function's arity, so the body fixes it at 4.

## What it refuses

A loud refusal is the feature: silently accepting what the model does not cover would produce
confident wrong bits, or a confident wrong time.

```python
for kwargs in ({"L": 32}, {"R": 8}, {"L": 16384, "platform_dir": default_platform_dir()}):
    try:
        VitisFft(name="bad", sim=Simulation(), clk=Clock(freq=250e6), **kwargs)
    except (ValueError, NotImplementedError) as exc:
        print(type(exc).__name__ + ":", str(exc).split(" on ")[0].split(". ")[0])
```

```text
ValueError: L=32 is not a power of R=4
NotImplementedError: VitisFft: only R=4 is modelled (got R=8)
ValueError: VitisFft: vitis_fft_task_16_2_18_2_0_0.proc has no measurement at L=16384
```

Refused: a radix other than 4; a length that is not a power of the radix (32, 128, 512 take a
different "forked" architecture in the library); an output order other than natural; a scaling mode
other than `NO_SCALING` above `L = 16`; and, when timed from a platform, a configuration the platform
has not measured -- the error names the command that calibrates it.
