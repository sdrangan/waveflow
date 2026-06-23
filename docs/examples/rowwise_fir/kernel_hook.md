---
title: Writing the dataflow hook
parent: Rowwise FIR
nav_order: 5
has_children: false
audience: hls
api: [synthesizable]
summary: "The hand-written fir_dataflow.tpp: the whole load-compute-store pipeline as three HLS functions (load_row / compute_row / store_row) wired in a per-row #pragma HLS DATAFLOW region over a partitioned-BRAM ping-pong and an hls::stream FIFO, each at II=1. The interface pragmas live in the generated m_axi top; the core is a reusable fir_accel_core."
---

# Writing the dataflow hook

The synthesizable side of the [load-compute-store dataflow](./dataflow.md) is one hand-written kernel,
[`fir_dataflow.tpp`](../../../examples/rowwise_fir/fir_dataflow.tpp), attached to the `dataflow` method
with `@synthesizable`. It is the [**Dataflow** custom-hook pattern](../../guide/custom_hooks/dataflow.md)
— the hook owns the *whole* pipeline, not just the compute.

## Three functions in a `#pragma HLS DATAFLOW` region

The core is one row loop whose body is a DATAFLOW region wiring three sub-functions through two on-chip
channels — a partitioned BRAM (windowed) and a FIFO (sequential):

```cpp
static void fir_accel_core(const real_t* X, real_t* Y, const real_t* h, int n_rows, int n_cols) {
    const int out_len = n_cols - T + 1;
ROW:
    for (int r = 0; r < n_rows; ++r) {
#pragma HLS DATAFLOW
        real_t xbuf[NCOL_MAX];                                  // load -> compute (ping-pong)
#pragma HLS ARRAY_PARTITION variable=xbuf cyclic factor=8 dim=1 //   partitioned for the T-tap window
        real_t htbuf[T];
#pragma HLS ARRAY_PARTITION variable=htbuf complete dim=1
        hls::stream<real_t> yfifo;                             // compute -> store (FIFO)
#pragma HLS STREAM variable=yfifo depth=64

        fir_load_row(X, h, r * n_cols, n_cols, xbuf, htbuf);   // burst-read one row + taps
        fir_compute_row(htbuf, xbuf, n_cols, yfifo);           // FIR at II=1 from BRAM only
        fir_store_row(Y, r * out_len, out_len, yfifo);         // burst-write one output row
    }
}
```

- **`#pragma HLS DATAFLOW`** runs the three functions concurrently across iterations — load(r+1) while
  compute(r) while store(r−1).
- **`xbuf` declared inside the loop** ⇒ HLS gives the region a depth-2 ping-pong (`N_PINGPONG = 2`): one
  copy filled by load, the other read by compute.
- **`ARRAY_PARTITION cyclic factor=8`** on `xbuf` lets the `T = 8`-tap window read in parallel each
  cycle; `htbuf` is `complete`-partitioned (the taps are all read at once).

The compute sustains one output per cycle, with the tap loop fully unrolled, accumulating **in the
golden's order** so csim/cosim are bit-exact:

```cpp
static void fir_compute_row(const real_t htbuf[T], real_t xbuf[NCOL_MAX], int n_cols,
                            hls::stream<real_t>& yfifo) {
COMPUTE:
    for (int j = 0; j < n_cols - T + 1; ++j) {
#pragma HLS PIPELINE II=1
        real_t acc = 0;
        for (int t = 0; t < T; ++t) {
#pragma HLS UNROLL
            acc += htbuf[t] * xbuf[j + (T - 1) - t];           // left-to-right == fir_golden
        }
        yfifo.write(acc);
    }
}
```

`fir_load_row` and `fir_store_row` are simple `II=1` burst loops over `X[base + j]` / `Y[obase + j]`,
which HLS coalesces into AXI bursts.

## Interface pragmas live in the generated top

The hook core takes plain pointers — **no** `m_axi` / `s_axilite` pragmas — so it is reusable and the
interface is a codegen concern. The generated top
([`fir_build.py`](../../../examples/rowwise_fir/fir_build.py) `render_top`) wraps it:

```cpp
// generated gen/fir.cpp
void fir(real_t* gmem, int x_off, int y_off, int h_off, int n_rows, int n_cols) {
#pragma HLS INTERFACE m_axi port=gmem bundle=gmem ...
#pragma HLS INTERFACE s_axilite port=x_off ...
    fir_dataflow::fir_accel_core(gmem + x_off, gmem + y_off, gmem + h_off, n_rows, n_cols);
}
```

A **single full-duplex `m_axi` bundle** suffices — AXI read and write channels are independent, so the
X-read and Y-write never contend (proven in the Phase-1 [sandbox](../../../examples/rowwise_fir/sandbox/)).
Offsets are **element indices**, matching the Python `Region`.

## Validated against one golden

The `@synthesizable dataflow` method's Python body is never run in the sim (the
[three stage processes](./pymodel.md) model its timing), but the kernel is checked against the same
[`execute` golden](./fir.md#the-golden) in [C and RTL simulation](./rtlsim.md) — bit-for-bit, because
the tap order matches.

## See also

- [Dataflow custom hook](../../guide/custom_hooks/dataflow.md) — the pattern (when/why), in the hooks guide.
- [The load-compute-store dataflow](./dataflow.md) — the sim-side timing model this kernel realizes.
- [`fir_dataflow.tpp`](../../../examples/rowwise_fir/fir_dataflow.tpp) — the full kernel source.
