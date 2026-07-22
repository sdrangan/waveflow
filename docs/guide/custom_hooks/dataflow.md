---
title: Dataflow — load, compute, store pipeline
parent: Custom Hooks
nav_order: 5
audience: hls
api: [synthesizable, read_array, write_array]
summary: "The dataflow hook pattern: the hook owns the WHOLE load-compute-store pipeline — three HLS functions wired in a per-row #pragma HLS DATAFLOW region over a partitioned-BRAM ping-pong (load→compute) and an hls::stream FIFO (compute→store), each stage at II=1. Used when a sliding window forces a resident, randomly-addressable row buffer (you cannot stream it). Walked through examples/rowwise_fir."
---

# Dataflow — load, compute, store pipeline

> **Status — not the preferred concurrency model.** This page documents Waveflow's *initial* approach
> to load-compute-store: a **single hand-written kernel** that owns the whole pipeline via one
> `#pragma HLS DATAFLOW` region. The current direction models concurrency as a **generated multi-task
> network** of composed sub-components (the `hls::task` interleaver / `MemRStream`+`MemWStream`+SOBIF
> path — see [Concurrency](../concurrency/)). The single-kernel technique here remains valid for one
> self-contained accelerator, but it is being superseded as *the* concurrency story and will be
> replaced/retired over time.

The [block](./block.md) pattern lets codegen move the data and leaves the hook as pure compute over
a materialized array. The **dataflow** pattern is the opposite extreme: the hook owns the **entire
load-compute-store pipeline** — the load burst, the compute, *and* the store burst — wired together
in a `#pragma HLS DATAFLOW` region so the three stages overlap.

Reach for it when the computation **cannot be a pure stream**: a sliding window needs the input
*resident and randomly addressable*, so you can't process beats as they arrive ([stream](./stream.md))
and you don't want one serial load-then-compute-then-store ([block](./block.md)). You want the load
of the next block overlapping the compute of the current one — the hardware realization of the
[double-buffered timing model](../concurrency/python/sob.md).

The worked example is [`examples/rowwise_fir`](../../../examples/rowwise_fir/fir.py) — a real-valued
FIR per matrix row, `Y[i,j] = Σ_t h[t]·X[i, j−t]`. The sliding `j−t` window is exactly what forces a
resident row buffer.

## The shape: three functions + one `#pragma HLS DATAFLOW`

The hook is the whole kernel core. It is one row loop whose body is a DATAFLOW region wiring three
sub-functions — load, compute, store — that hand off through on-chip channels:

```cpp
// fir_dataflow.tpp — the load-compute-store core (the @synthesizable hook)
static void fir_accel_core(const real_t* X, real_t* Y, const real_t* h,
                           int n_rows, int n_cols) {
    const int out_len = n_cols - T + 1;
ROW:
    for (int r = 0; r < n_rows; ++r) {
#pragma HLS DATAFLOW
        real_t xbuf[NCOL_MAX];                                  // load -> compute channel
#pragma HLS ARRAY_PARTITION variable=xbuf cyclic factor=8 dim=1 //   partitioned for the T-tap window
        real_t htbuf[T];
#pragma HLS ARRAY_PARTITION variable=htbuf complete dim=1
        hls::stream<real_t> yfifo;                             // compute -> store channel
#pragma HLS STREAM variable=yfifo depth=64

        fir_load_row(X, h, r * n_cols, n_cols, xbuf, htbuf);   // burst-read one row + taps
        fir_compute_row(htbuf, xbuf, n_cols, yfifo);           // FIR at II=1 from BRAM only
        fir_store_row(Y, r * out_len, out_len, yfifo);         // burst-write one output row
    }
}
```

Three things make this a *pipeline* rather than three serial calls:

1. **`#pragma HLS DATAFLOW`** lets the three functions run concurrently on successive iterations — so
   while compute(r) runs, load(r+1) is already filling the next buffer.
2. **The buffers are double-buffered (ping-pong).** `xbuf` is declared *inside* the row loop, so HLS
   gives the DATAFLOW region a depth-2 PIPO (`N_PINGPONG = 2`): load writes one copy while compute
   reads the other. This is the synthesizable counterpart of the depth-2 `simpy.Store` in the
   [double-buffered timing model](../concurrency/python/sob.md).
3. **Two channel kinds, by access pattern.** Load→compute is a **partitioned BRAM** because the FIR
   window needs *random* access (`xbuf[j + (T−1) − t]`); compute→store is an **`hls::stream` FIFO**
   because the outputs are produced and consumed strictly in order.

Each stage carries `#pragma HLS PIPELINE II=1`, so the load and store coalesce into AXI bursts and
the compute sustains one output per cycle:

```cpp
static void fir_compute_row(const real_t htbuf[T], real_t xbuf[NCOL_MAX],
                            int n_cols, hls::stream<real_t>& yfifo) {
    const int out_len = n_cols - T + 1;
COMPUTE:
    for (int j = 0; j < out_len; ++j) {
#pragma HLS PIPELINE II=1
        real_t acc = 0;
        for (int t = 0; t < T; ++t) {
#pragma HLS UNROLL
            acc += htbuf[t] * xbuf[j + (T - 1) - t];           // T-tap window, fully unrolled
        }
        yfifo.write(acc);
    }
}
```

(The tap accumulation is left-to-right in `t` to match the [golden](../../../examples/rowwise_fir/fir_golden.py)
order bit-for-bit, so csim/cosim are exact, not tolerance-based.)

## Interface pragmas live in the generated top, not the hook

The hook body is **pure kernel** — it takes plain pointers and has *no* `m_axi` / `s_axilite`
pragmas. Those belong to the generated top, so the same core is reusable and the interface is a
codegen concern:

```cpp
// generated gen/fir.cpp — the m_axi top calls the hook core
void fir(real_t* gmem, int x_off, int y_off, int h_off, int n_rows, int n_cols) {
#pragma HLS INTERFACE m_axi port=gmem bundle=gmem ...
#pragma HLS INTERFACE s_axilite port=x_off ...
    fir_dataflow::fir_accel_core(gmem + x_off, gmem + y_off, gmem + h_off, n_rows, n_cols);
}
```

The top is emitted by [`fir_build.py`](../../../examples/rowwise_fir/fir_build.py)`render_top` and
addresses memory in **element coordinates** (`gmem + x_off`, where the offsets are element indices,
not bytes — the [element-coordinate](./reference.md) convention). A single full-duplex `m_axi`
bundle suffices: AXI read and write channels are independent, so the X-read and Y-write never
contend (validated in the [Phase 1 sandbox](../../../examples/rowwise_fir/sandbox/)).

## When to use it

Reach for the dataflow pattern when:

- the computation needs **windowed / random access** to the input (a sliding window, a stencil) — so
  it cannot be a pure [stream](./stream.md) and the row/tile must be resident;
- you want **load(n+1) ∥ compute(n) ∥ store(n−1)** overlap rather than the serial latency of a
  [block](./block.md) hook;
- the load→compute and compute→store channels have *different* access patterns (random vs.
  sequential), so they map to a partitioned BRAM and a FIFO respectively.

It is the most involved pattern — the hook is the whole kernel — so reach for [block](./block.md)
first if a resident-then-compute is enough, and [stream](./stream.md) if the math can run as data
arrives.

## The sim side

This page is the **synthesizable** half. The matching **simulation** timing model — three concurrent
SimPy processes (loader / compute / storer) producing the `max(load, compute, store)` timeline, and
how that model is cosim-calibrated — is [Double-buffered processing](../concurrency/python/sob.md).
The two are the two faces of the same load-compute-store accelerator.

## See also

- [Double-buffered processing](../concurrency/python/sob.md) — the sim-side timing model this hook realizes, with the matrix-LT FIR worked example and its calibration.
- [Block — load, compute, store](./block.md) — the simpler pattern (codegen moves the data; the hook is pure compute).
- [Stream — process as you read](./stream.md) — when the math can run as data arrives, no resident buffer.
- [Kernel transfer reference](./reference.md) — the in-kernel transfer calls and the element-coordinate convention.
- [Rowwise FIR example](../../examples/rowwise_fir/) — the full worked walkthrough; this hook is its [Writing the dataflow hook](../../examples/rowwise_fir/kernel_hook.md) page.
- [`examples/rowwise_fir`](../../../examples/rowwise_fir/fir.py) — the source: the hook ([`fir_dataflow.tpp`](../../../examples/rowwise_fir/fir_dataflow.tpp)), the generated top, and the bit-exact golden.
- [Concurrency](../concurrency/) — the canonical multi-task (`hls::task`) LCS direction.
