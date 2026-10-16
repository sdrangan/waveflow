---
title: SSR FFT interfaces
parent: The SSR FFT
grand_parent: DSP Blocks
nav_order: 2
audience: python
api: [SsrFft, StreamIF, RadixWord, EdgeType, StreamOfBlocksIF]
summary: "SsrFft's ports: one RadixWord stream each way -- R complex samples a beat, the unit every internal edge carries, an ap_uint whose width is the edge's own (128 bits in, 216 out at L = 1024) -- or, with lanes=True, VitisFft's R-lane port group. The packing (the generated array utils, never by hand), how frames are delimited, how wide words travel through the pysim and the XSI BFMs (64-bit chunks), how to wire either boundary, and what the generated top looks like."
---

# Interfaces

## The boundary: one `RadixWord` each way

By default `SsrFft` has two ports:

| port | direction | endpoint | word |
|---|---|---|---|
| `s_in` | in | `StreamIFSlave` | one `RadixWord`: `R` input samples, `2·in_w·R` bits (128 at 16-bit input) |
| `m_out` | out | `StreamIFMaster` | one `RadixWord`: `R` output bins, `2·out_w·R` bits (216 at `L = 1024`) |

- **Layout.** Word `k` of a frame carries samples `kR .. kR + R − 1`, sample `kR + r` in lane `r`; lane
  `r` occupies bits `[2·W·r, 2·W·(r + 1))`, the real part in the low `W` bits. Output order is
  natural, so bin `X[k]` is lane `k % R` of word `k // R`. A frame is `L/R` beats.
- **Frames** are delimited by **count**: `L/R` beats. Back to back, a frame's first word follows the
  previous frame's last on the very next cycle; nothing marks the boundary.
- **The output is wider than the input**, by `log2 L + 1` bits a sample: read `fft.m_out.bitwidth`
  rather than computing it.

This is the unit every edge *inside* the FFT carries, so the module needs no adaptor at either end,
and a neighbour that produces or consumes `RadixWord`s -- a data converter streaming several samples a
beat on one wide AXI-Stream port, say -- connects to it directly.

## `RadixWord`

A **`RadixWord`** is a `DataArray` of `R` `ComplexField[FixedField(W, I)]` -- `R` complex samples, one
per lane (`waveflow.dsp.ssr_fft.types`).

- **The channel is an `ap_uint`.** By Waveflow convention a `StreamIF` carries raw words and the type
  lives at the endpoints. Each edge's `StreamIF` has `bitwidth = 2·W·R` for its own `W`: 128 bits
  through the transposer, then 152, 168, 184, 200 after the stages and 216 out at `L = 1024`.
  `edge_types(geo)` lists every edge with its format and width.
- **Unpacked, a word is AMD's `SuperSampleContainer<R, T>`.** `ComplexField` serializes as interleaved
  I/Q, the layout of `std::complex<ap_fixed<W, I>>`, so the vendor's arithmetic runs on the lanes
  directly.
- **Nothing packs by hand.** In C++ each task unpacks with the generated `<elem>_array_utils` lane
  routines (wrapped per edge in the generated configuration header); in Python the same schema packs
  and unpacks (`write_array` / `read_array`).
- **One block channel.** With `reorder="sob"`, the edge between the reorder's writer and reader is a
  `StreamOfBlocksIF` -- `hls::stream_of_blocks<ap_uint<W>[L/R], 2>` -- holding whole frames.

### Wide words, end to end

A `RadixWord` is wider than 64 bits, and every layer carries it the same way: as `k = ceil(W/64)`
little-endian `uint64` **chunks**, chunk 0 the low 64 bits.

| layer | how a `W`-bit word is held |
|---|---|
| pysim stream | an `(n, k)` `uint64` array (`Words`) |
| burst bundle on disk | `words.bin` holds the chunks beat after beat; `meta.json` says `word_bytes = 8k`; `bounds.bin` counts beats (`write_burst_bundle(..., word_chunks=k)`) |
| XSI BFMs | `AxisMaster` / `AxisSlave` take `chunks = k` and move `k` chunks a beat through the port; the generator passes it from the endpoint's width |

So a scenario written once in Python drives the pysim and the RTL with the same bytes, at any width.

## With `lanes=True`: `VitisFft`'s port group

`SsrFft(lanes=True)` presents exactly [`VitisFft`'s ports](../../vitis_l1/fft/interfaces.md) instead:

| port | direction | endpoint | word |
|---|---|---|---|
| `s_in_0 .. s_in_3` | in | `StreamIFSlave` | `2 * in_w` bits: one input sample |
| `m_out_0 .. m_out_3` | out | `StreamIFMaster` | `2 * out_w` bits: one output sample |

Lane `j` carries samples `j, j + R, j + 2R, ...`; the four lanes are parallel ports, so whatever feeds
or drains them must move all four at once. Two adaptor tasks join the lanes into the `RadixWord`
stream and split it again. The two modules are then interchangeable in a design, and share a
testbench, scenario files and golden -- which is how `SsrFft` was first checked against `VitisFft`.

## Wiring it

One `StreamIF` per port. From `waveflow/dsp/ssr_fft/testbench.py`, with the default boundary:

```python
self.dut = SsrFft(name="ssr_fft", sim=self.sim, clk=self.clk, L=self.length, ...)
drv = StreamDriver(name="drv_0", sim=self.sim, bitwidth=self.dut.s_in.bitwidth,
                   has_tlast=True, in_bundle="vectors/s_in", root=self.root)
i = StreamIF(name="in_if_0", sim=self.sim, clk=self.clk, bitwidth=self.dut.s_in.bitwidth)
i.bind(ep_name="master", endpoint=drv.stream_ep)
i.bind(ep_name="slave", endpoint=self.dut.s_in)
```

and the same on the output side with `m_out`'s width. With `lanes=True`, four of each, as in
`VitisFft`'s testbench. Inside a larger composite the FFT is one child like any other -- see
[Including it in your design](build.md).

## The generated top

`SsrFft` is a composite, so `composite_top_spec` derives the whole top from its graph: one
`hls_thread_local` channel per internal interface, one `hls::task` per child. At `L = 64` with the
ping-pong reorder:

```cpp
void ssr_fft(hls::stream<ap_uint<128> >& s_in, hls::stream<ap_uint<184> >& m_out) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream<ap_uint<128> > in_reg;
    hls_thread_local hls::stream<ap_uint<128> > tp0;
    ...
    hls_thread_local hls::stream<ap_uint<184> > rc;
    hls_thread_local hls::task t0(ssr_fft_L64_16_2_18_2_in_reg, s_in, in_reg);
    hls_thread_local hls::task t1(ssr_fft_L64_16_2_18_2_tp0, in_reg, tp0);
    ...
    hls_thread_local hls::task t8(ssr_fft_L64_16_2_18_2_rc, st2, rc);
    hls_thread_local hls::task t9(ssr_fft_L64_16_2_18_2_rp, rc, m_out);
}
```

Each channel's width is its edge's own and its name the producing task's; every task is a plain
function --
`ssr_fft_<config>_<task>` -- so its RTL instance is `<name>_U0`, which is what lets a trace name an
internal net without guessing.
