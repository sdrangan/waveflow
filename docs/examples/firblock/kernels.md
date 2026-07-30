---
title: The two kernels
parent: Block FIR (state + fixed point)
nav_order: 6
---
# The two kernels — one filter, two unrolling structures

`fir_block` ships **two** hand-written task bodies that compute bit-identical results and differ only
in how much work an iteration does. One parameter picks between them:

```python
unroll_lane: HwParam[bool] = False
```

- `False` → `fir_compute_serial_task.h` — **one output per iteration**, carrying a lane index across
  iterations.
- `True` → `fir_compute_unroll_task.h` — **`LW` outputs per iteration**, the whole transport word at
  once.

Same arithmetic, same `acc_t`, same rounding, one [golden](./testbench.md). So the parameter changes
only the *physics*, which is what makes the pair a QoR probe rather than two designs.

## Why hand-written at all

The extractor lowers `run_iter` to a task body for leaves shaped like `get → hook → write`. These two
are not: they construct descriptors and drive a `framed_word` channel, which is outside its
vocabulary — real extraction stops at `int(self.mem_dwidth)`. That is not special to this design;
**every in-band framer in the tree is hand-written for the same reason** (`mem_copy`'s `Sequencer`, all
four `il_*` tasks).

So both leaves override `kernel_task()` to hand over a body, and `run_iter` stays as the pysim golden.
What is *not* hand-written is the storage — see [DUT codegen](./codegen_dut.md).

## Why two functions, not one with a branch

The obvious alternative is one body with `if constexpr (UNROLL)`. Three reasons against, and Vitis's
patchy appetite for `if constexpr` is the least of them:

1. **They need different state shapes.** The serial body carries a `dline[NTAP]` shift register; the
   unrolled one carries a `dl[NTAP+LW-1]` shared history. A single function would have to declare
   both, or declare the union and index it differently.
2. **One function is one csynth report.** The whole point of having both is comparing DSP counts and
   loop latencies side by side. Collapsing them into one module name throws that away.
3. **`kernel_task()` already dispatches by name**, so selection costs nothing:

```python
def kernel_task(self) -> KernelTask:
    return KernelTask(f"fir_compute_{self.variant}_task",
                      f"fir_compute_{self.variant}_task.h", ("s_in", "cmd_out"),
                      template_args=(int(self.mem_dwidth),))
```

## The seeding rule

Both bodies share one subtlety, and it is the bug this kernel actually shipped to RTL before the
[gate](./rtlsim.md) caught it.

Each iteration **shifts before it accumulates**. So the value seeded into the delay line is the state
at the **top** of the first iteration — *not* the invariant that holds at multiply time.

At MAC time the invariant is `dline[k] == x[i-k]`. Writing that down as the seed —
`dline[k] = carry[NTAP-1-k]` — reads correct and is wrong: the first `SHIFT` slides everything one
slot and pushes `dline[0]`'s dummy zero into `dline[1]`, dropping the newest carry sample. The
pre-shift seed is one index different:

```cpp
SEED: for (int j = 0; j < NTAP - 1; ++j) {
#pragma HLS UNROLL
        dline[j] = (d.zero_state != 0) ? (samp_t)0 : carry[NTAP - 2 - j];
    }
    dline[NTAP - 1] = (samp_t)0;              // shifted out before any MAC reads it
```

It csynthed cleanly, and it passed block 1 — because `zero_state` starts that block from zeros so it
never reads the carry. Only block 2's first samples were wrong.

{: .warning }
> Verify index algebra **before** synthesizing. Both bodies' indices were checked element-by-element
> against the golden in numpy — including unaligned block lengths and the tail offset — which costs
> seconds. Diagnosing the same class through csynth + RTL simulation costs minutes per hypothesis.

## Serial — one output per iteration

The lane index `k` is carried across iterations, so the stream is touched only every `LW`-th one:

```cpp
    int k = 0;                                 // lane index, carried across iterations
FIR: for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
        if (k == 0) fir_au::read_framed_stream_lane<MEM_DW>(s_in, ilane, LW, tl);
        samp_t x = ilane[k];
    SHIFT: for (int m = NTAP - 1; m > 0; --m) {
#pragma HLS UNROLL
            dline[m] = dline[m - 1];
        }
        dline[0] = x;
        acc_t acc = 0;
    MAC: for (int m = 0; m < NTAP; ++m) {
#pragma HLS UNROLL
            acc += (acc_t)(taps[m] * dline[m]);
        }
        olane[k] = (samp_t)acc;
        ++k;
        if (k == LW || i + 1 == n) {            // flush a full lane, or the final partial one
            fir_au::write_framed_stream_lane<MEM_DW>(olane, cmd_out, (i + 1 == n), k);
            k = 0;
        }
    }
```

That body has a **conditional dequeue and a conditional enqueue**, both gated by a loop-carried index —
which makes `II = 1` genuinely doubtful. It is not obvious, so it was measured rather than assumed:

> **Both bodies schedule at II = 1.** The conditionals cost **3 cycles of iteration latency** (12 vs 9),
> not initiation interval.

The tail is handled by the same `if`: `i + 1 == n` flushes a partial lane with `k` valid elements and
sets `TLAST`.

## Unrolled — one shared history, not `LW` copies

The naive vectorization gives each lane its own delay line, `dl[LW][NTAP]`, replicating every stored
sample `LW` times. That is unnecessary. **Consecutive outputs read the same history at a one-sample
offset**, so a single `dl[NTAP+LW-1]` serves all `LW` lanes by staggering where each window starts:

```
invariant after the beat starting at i:   dl[m] == x[i + LW-1 - m]
lane j output:  y[i+j] = Σ taps[k] · dl[LW-1-j + k]
```

```cpp
    samp_t dl[NTAP + LW - 1];                 // ONE history, LW staggered windows over it
#pragma HLS ARRAY_PARTITION variable=dl complete dim=1
FIRV: for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
        fir_au::read_framed_stream_lane<MEM_DW>(s_in, ilane, LW, tl);
    SH: for (int m = NTAP + LW - 2; m >= LW; --m) {
#pragma HLS UNROLL
            dl[m] = dl[m - LW];               // shift the whole history by a lane
        }
    INS: for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
            dl[LW - 1 - j] = ilane[j];
        }
    LANE: for (int j = 0; j < LW; ++j) {      // LW independent windows -> LW*NTAP multipliers
#pragma HLS UNROLL
            acc_t acc = 0;
        MAC: for (int m = 0; m < NTAP; ++m) {
#pragma HLS UNROLL
                acc += (acc_t)(taps[m] * dl[LW - 1 - j + m]);
            }
            olane[j] = (samp_t)acc;
        }
        const int nb = (n - i < LW) ? (n - i) : LW;
        fir_au::write_framed_stream_lane<MEM_DW>(olane, cmd_out, (i + LW >= n), nb);
    }
```

The saving is worth stating precisely: the shared history costs `LW-1` extra registers instead of
`(LW-1)·NTAP`. **Only the multipliers replicate** — which is the part vectorization is actually buying.
At `T = 32, LW = 4` that is 3 extra registers against 93.

### The tail, when `n` is not a multiple of `LW`

The final beat carries `LW - nb_last` padding samples, and `INS` writes them into the **low** `dl`
entries. So the valid history is offset, and the carry has to start `off` slots up:

```cpp
    const int off = LW - (n - (nw - 1) * LW);
SAVE: for (int j = 0; j < NTAP - 1; ++j) {
        carry[j] = dl[off + NTAP - 2 - j];
    }
```

`off == 0` when `n` divides evenly, so this collapses to the serial body's spelling. Aligned blocks are
the common case and would never have exposed the bug — which is why the numpy check ran `n = 13` as
well as `n = 16` and `n = 32`.

## Deserialization is generated, never hand-rolled

Both bodies use `fir_au::read_framed_stream_lane` / `write_framed_stream_lane`, where `fir_au` is an
alias for the sample element's generated array-utils namespace. Neither contains a `.range()`.

That is the rule, not a preference: the generated routines and the Python model's
`DataArray.serialize` are **one packing contract**, so the numpy golden and the Vitis kernel agree
bit-for-bit at any `LW`. See [array serialization](../../guide/vectorization/hls/arrayutils.md) — and
note that hand-rolled packing is *correct* at `LW = 1`, so this is a mistake that hides until a width
changes.

## What each one costs

Measured at `T = 32`, `MEM_DW = 32`, xc7z020:

| `W` | LW | serial DSP | unroll DSP | serial loop | unroll loop |
|---|---|---|---|---|---|
| 24 | 1 | 64 | 64 | identical | identical |
| 16 | 2 | **32** | 64 | 4097 cyc | **2049 cyc** |
| 8 | 4 | **17** | 64 | 4097 cyc | ~1025 cyc |

At `LW = 1` the two converge to the same design, which is a useful sanity check on the pair.

The DSP column is flat at 64 for the unrolled kernel and *falls* for the serial one, which looks
backwards until you notice both effects are the same one: at `W = 8` HLS packs two multiplies into one
DSP48. The unrolled kernel has 4× the multiplies at half the cost each, so it lands back on 64. See
[Resource modelling](./resources.md#dsp-a-prior-exact-with-nothing-fitted), where that cancellation
holds across the whole grid — and stops holding at `MEM_DW = 64`.

## Where to next

- [DUT codegen](./codegen_dut.md) — how `unroll_lane` selects a body, and where the storage comes from.
- [Resource modelling](./resources.md) — the full 24-point sweep these three rows are a corner of, and
  a model that predicts the DSP column exactly.
