---
title: The kernel
parent: Vector Multiply (resource modelling)
nav_order: 2
audience: hls
summary: "The hand-written vec_mult_task.h: why a shared input port forces a buffer (and why two separate streams would not), why the buffer is firing-local rather than declared state, why sustaining II=1 forces a cyclic ARRAY_PARTITION, how the ragged final beat is handled, and why the product wraps rather than saturates. This is the file every resource number on the example traces back to."
---

# The kernel

`vec_mult_task.h` is hand-written and declared through
[`kernel_task()`](../../guide/comp_codegen/freerunning_override.md). It is worth reading closely
because **every resource number in this example traces back to two lines of it** — one buffer
declaration and one pragma.

```cpp
template <int DWID, int VLEN>
static void vec_mult_task(hls::stream<ap_uint<DWID> >& s_in,
                          hls::stream<ap_uint<DWID> >& z_out) {
    typedef vm_au::value_type samp_t;
    const int LW = vm_au::lane_capacity<DWID>();

    VecCmd cmd;
    cmd.read_stream<DWID>(s_in);
    const int n = (int)cmd.n;

    samp_t buf[VLEN];
#pragma HLS ARRAY_PARTITION variable=buf cyclic factor=LW dim=1

    samp_t xlane[LW], ylane[LW], zlane[LW];
#pragma HLS ARRAY_PARTITION variable=xlane complete dim=1
    ...
}
```

`buf` is partitioned **cyclic**; the three lane arrays are partitioned **complete**. The difference
is what each one is for: `buf` is storage that must serve `LW` accesses a cycle, so it stays a memory
split into banks, while `xlane`/`ylane`/`zlane` are `LW`-element staging registers written and read
whole every beat — completely partitioning them makes them registers rather than a memory at all.

{: .note }
> **`buf` is firing-local, not state.** It is filled in `LOAD` and fully consumed in `MULT`, so
> nothing has to survive to the next firing — which is why it is an ordinary local and **not** an
> [`add_state`](../firblock/state.md) declaration. `fir_block`'s taps are the contrasting case: they
> carry across firings and must be declared, because the generator has to give them a lifetime the
> C++ scope does not. Here the whole buffer dies with the job, and its cost is still paid — that is
> the point the [resource model](./resource_model.md) makes.

## Why it buffers

`x` and `y` share one port, so they arrive **sequentially**. The kernel has to hold `x` while `y`
streams past:

```text
LOAD:  read ceil(n/LW) words of x into buf
MULT:  read ceil(n/LW) words of y, multiply against buf, write z
```

{: .warning }
> **The plausible reason is the wrong one.** You might think a buffer is needed because two stream
> reads cannot issue in the same iteration. They can. With two *separate* input streams, `x_in` and
> `y_in` are distinct FIFOs with independent ports and their reads schedule in the same beat —
> measured, that design pipelines at **II=1 and uses zero BRAM**.
>
> A shared port is what makes storage unavoidable. The buffer is a consequence of the **interface**,
> not of the arithmetic and not of the read count. That distinction is the reason a resource model
> has to be keyed on structure rather than on what a kernel appears to compute.

## Why it partitions

The `MULT` beat consumes `LW` samples of `buf` at once. An unpartitioned array is a two-port memory,
so more than two lanes would serialize and II would rise above 1.

```cpp
#pragma HLS ARRAY_PARTITION variable=buf cyclic factor=LW dim=1
```

`cyclic factor=LW` puts element `i` in bank `i % LW`. Because lane `j` of word `i` is element
`i*LW + j`, **lane `j` always reads bank `j`** — `LW` conflict-free accesses per cycle.

That single pragma converts a *throughput* requirement into a *memory* cost, and the cost is a
[ceiling rather than a ratio](./sweep.md#the-two-bram-regimes): each of the `LW` banks is `VLEN/LW`
deep, and a bank shallower than one block still occupies a whole one.

Note **`VLEN`, not `n`**. The buffer is sized by the compile-time bound, so the area is paid whatever
length actually arrives.

## The ragged final beat

`n` need not be a multiple of `LW`, so the last beat carries fewer lanes:

```cpp
LOAD:
    for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
        const int nlane = (n - i < LW) ? (n - i) : LW;   // ragged final beat
        vm_au::read_stream_lane<DWID>(s_in, xlane, nlane);
        for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
            if (j < nlane) buf[i + j] = xlane[j];
        }
    }
```

Two things to notice, because they are where a vectorized body goes wrong:

- The inner loop runs to `LW`, not `nlane`, and *guards* the write. `LW` is a compile-time constant,
  so the loop unrolls; a runtime bound would not.
- `read_stream_lane(..., nlane)` moves a runtime number of elements. That is the generated serializer
  doing the partial-word extraction — never a hand-rolled `.range()`, per
  [array utils](../../guide/vectorization/hls/arrayutils.md). The bug it prevents hides at `LW=1`.

This is also where the example's LUT cost comes from. A **runtime** lane count at runtime positions is
a variable-position mux — a crossbar — and that is why LUT grows as `LW²` rather than linearly. See
[the model page](./resource_model.md#structure-form-dictionary).

The [testbench](./testbench.md) checks `n ∈ {1, 7, 63, 64, 65, 253}` for exactly this reason: a
full-length run never reaches the partial beat.

## The multiply

`MULT` has the same beat structure as `LOAD` — same ragged `nlane`, same inner loop run to the
compile-time `LW` and guarded rather than bounded. What is new is the arithmetic, and one line of it
is where the twin can silently drift:

```cpp
MULT:
    for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=VLEN
        const int nlane = (n - i < LW) ? (n - i) : LW;
        vm_au::read_stream_lane<DWID>(s_in, ylane, nlane);
        for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
            ap_int<2 * 16> p = (ap_int<2 * 16>)buf[i + j] * (ap_int<2 * 16>)ylane[j];
            zlane[j] = (samp_t)p;
        }
        vm_au::write_stream_lane<DWID>(zlane, z_out, nlane);
    }
```

This is the loop the [DSP rule](./resource_model.md#the-three-laws) prices: `LW` multiplies per beat,
16-bit operands, one DSP each. It reads `buf` `LW` samples at a time — the access the `cyclic`
partition exists to make conflict-free — and writes through the generated `write_stream_lane`, the
mirror of the read on the way in.

{: .warning }
> **The product wraps; it does not saturate.** The multiply widens to `ap_int<32>` and the result is
> cast straight back to `samp_t` (`ap_int<16>`), discarding the high half. That is not carelessness
> about overflow — it is what makes the C++ agree with the Python golden, which is numpy `int16`
> arithmetic and wraps. A saturating `ap_fixed` here would be the *more careful* choice and the
> *wrong* one: every product that overflowed would differ from Python, and only for operands large
> enough to reach the corner, so a short test would never see it. The
> [csim twin check](./testbench.md) is what holds the two definitions together.

`LOOP_TRIPCOUNT max=VLEN` is for the synthesis **report**, not the hardware. `n` is a runtime value,
so without it HLS cannot bound the latency it prints. It is a deliberate over-estimate — the loop
actually runs `ceil(n/LW)` times — which is safe precisely because the pragma never reaches the
generated logic.

## The response

```cpp
    VecResp resp;
    resp.tx_id = cmd.tx_id;
    resp.write_stream<DWID>(z_out);
```

Same id in, same id out — the transaction closes.

## Why hand-written

Every in-band framer and stateful compute body in this tree is hand-written, for the same reason:
constructing a descriptor, driving a partitioned buffer across two loop phases, and handling a runtime
partial beat are all outside the [extractor's](../../guide/comp_codegen/extractor.md) fixed
vocabulary. Nothing could derive the function name or the parameter order, so `kernel_task()` states
them and `run_iter` remains the golden.

That leaves two bodies for one behaviour, which is a liability until something checks them against
each other — see [Testbench](./testbench.md).

## Next

- [Testbench](./testbench.md) — the pysim golden and the csim twin check.
