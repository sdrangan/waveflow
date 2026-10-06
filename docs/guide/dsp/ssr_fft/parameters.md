---
title: SSR FFT parameters
parent: The SSR FFT
grand_parent: DSP Blocks
nav_order: 1
audience: python
api: [SsrFft, Geometry]
snippets: run
summary: "Constructing an SsrFft and every parameter it takes: the build-time HwParams (L and the input and twiddle widths), the reorder implementation, the clock and whether the pysim is timed. What it derives through Geometry -- the stage count, the commutator block sizes, every edge's format, the task list and the ports -- and what it refuses."
---

# Parameters

## Constructing one

```python
from waveflow.dsp.ssr_fft.hw import SsrFft
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation

fft = SsrFft(name="fft", sim=Simulation(), clk=Clock(freq=250e6), L=1024)
print("ports per side:", len(fft.s_in), "in,", len(fft.m_out), "out")
print("input word bits:", fft.s_in[0].bitwidth)
print("output word bits:", fft.m_out[0].bitwidth)
print("output format: ap_fixed<%d,%d>" % (fft.out_fmt.W, fft.out_fmt.int_bits))
print("stages:", fft.geo.S)
print("tasks:", len(fft.tasks), "->", " ".join(c.task.inst for c in fft.tasks))
```

```text
ports per side: 4 in, 4 out
input word bits: 32
output word bits: 54
output format: ap_fixed<27,13>
stages: 5
tasks: 18 -> lanes_in tp0 tp1 tp2 tp3 st0 cm0 st1 cm1 st2 cm2 st3 cm3 st4 rc rw rr lanes_out
```

The ports and formats are `VitisFft`'s at the same parameters; the inside is one child per task --
`tp*` the transposer, `st*` the stages, `cm*` their commutators, `rc`/`rw`/`rr` the reorder.

Everything that follows from `L` and the formats lives in one object, `Geometry`, which the model,
the module and the C++ generator all read:

```python
from waveflow.dsp.ssr_fft.model import Geometry

geo = Geometry(1024)
print("transposer block sizes:", geo.transposer_ds)
print("stage commutator block sizes:", [geo.stage_d(s) for s in range(geo.S - 1)])
for name, fmt in geo.edges()[4:8]:
    print(f"edge {name}: ap_fixed<{fmt.W},{fmt.int_bits}>")
```

```text
transposer block sizes: [1, 4, 16, 64]
stage commutator block sizes: [64, 16, 4, 1]
edge tp3: ap_fixed<16,2>
edge st0: ap_fixed<19,5>
edge cm0: ap_fixed<19,5>
edge st1: ap_fixed<21,7>
```

The reorder's frame buffer has two implementations ([Synthesis](synthesis.md#the-reorder-two-ways));
the ping-pong one is a single task:

```python
pp = SsrFft(name="fft", sim=Simulation(), clk=Clock(freq=250e6), L=1024, reorder="pingpong")
print("reorder tasks:", [c.task.inst for c in pp.tasks if c.task.inst.startswith("r")])
```

```text
reorder tasks: ['rc', 'rp']
```

## Every parameter

| parameter | type / binding | default | meaning |
|---|---|---|---|
| `name`, `sim` | | | as for every `HwModule` |
| `clk` | `Clock` | 250 MHz | the module's clock: the pysim times every task in its periods |
| `L` | `HwParam[int]` | 64 | transform length, `4^S` with `S >= 2` |
| `in_w`, `in_i` | `HwParam[int]` | 16, 2 | input `ap_fixed<in_w, in_i>`, per real/imaginary part |
| `tw_w`, `tw_i` | `HwParam[int]` | 18, 2 | twiddle table `ap_fixed<tw_w, tw_i>` |
| `reorder` | `str` | `"sob"` | the reorder's frame buffer: `"sob"` (a `stream_of_blocks` between two tasks) or `"pingpong"` (both halves in one task) |
| `timed` | `bool` | `True` | `False` gives exact bits in zero simulated time |

`L` and the widths are `HwParam`s: they reach the C++ as the generated configuration (formats,
ROMs), so two values are two artifacts. There is no `R` parameter: 4 is the only radix the model and
the task bodies cover.

**No timing parameters.** Unlike `VitisFft`, nothing is calibrated per length: every task runs at one
word a cycle, so the interval is `L/R` by construction, and each task's latency follows from its
structure. See [Timing](timing.md).

## What it derives

- **`geo`**, the `Geometry`: stages `S`, words per frame `L/R`, every commutator's block size, every
  edge's format (`geo.edges()`), the twiddle table and the reorder's permutation.
- **`out_fmt`**, the output format -- read it rather than computing it.
- **`tasks`**: the children, in pipeline order, each with its `kernel_task()` -- the generated wrapper
  it synthesizes to.
- **The ports**: `s_in` / `m_out` (lists) and `s_in_0..3` / `m_out_0..3` (attributes) -- see
  [Interfaces](interfaces.md).

## What it refuses

```python
for kwargs in ({"L": 32}, {"L": 4}, {"L": 64, "reorder": "fifo"}):
    try:
        SsrFft(name="bad", sim=Simulation(), **kwargs)
    except (ValueError, NotImplementedError) as exc:
        print(type(exc).__name__ + ":", exc)
```

```text
ValueError: 32 is not a power of 4
ValueError: L=4: need L = 4^S with S >= 2.
ValueError: reorder='fifo': one of ('sob', 'pingpong')
```

Also refused, in `Geometry`: a radix other than 4, and a scaling mode other than `NO_SCALING` -- the
modes the per-stage model has measured. The inverse transform is not built yet.
