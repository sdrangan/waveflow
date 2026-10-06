---
title: SSR FFT interfaces
parent: The SSR FFT
grand_parent: DSP Blocks
nav_order: 2
audience: python
api: [SsrFft, StreamIF, RadixWord, EdgeType, StreamOfBlocksIF]
summary: "SsrFft's ports -- the same R-wide port group as VitisFft, one complex sample per word, sample n on lane n % R -- and the streams inside: one RadixWord (R complex samples) per beat, an ap_uint whose width is the edge's own, packed and unpacked only by the generated array utils. Why the boundary is lanes (the XSI BFMs move 64-bit words; the inside is 128-232 bits), how to wire the group, and what the generated top looks like."
---

# Interfaces

## The boundary: an `R`-wide port group

`SsrFft` exposes exactly [`VitisFft`'s port group](../../vitis_l1/fft/interfaces.md), so the two are
interchangeable in a design and share a testbench and a golden:

| port | direction | endpoint | word |
|---|---|---|---|
| `s_in_0 .. s_in_3` | in | `StreamIFSlave` | `2 * in_w` bits: one input sample |
| `m_out_0 .. m_out_3` | out | `StreamIFMaster` | `2 * out_w` bits: one output sample |

- **Lane layout.** Sample `n` of a frame travels on lane `n % R` at word `n // R`, on both sides.
  Output order is natural, so bin `X[k]` leaves on lane `k % R`. A frame is `L/R` words per lane.
- **Packing.** One complex sample per word, the real part in the low `W` bits -- `std::complex`'s
  member order. The output width grows with `L` (`in_w + log2 L + 1`): read `fft.out_fmt.W`.
- **Frames** are delimited by **count**: `L/R` words per lane. Back to back, a frame's first word
  follows the previous frame's last on the very next cycle; nothing marks the boundary.
- **Move the lanes together.** They are parallel ports: whatever feeds or drains them must move all
  four at once, or a frame costs `L` cycles instead of `L/R`.

Each port is available as a list (`fft.s_in[j]`) and as an attribute (`fft.s_in_0`), one object.

## Inside: `RadixWord` streams

Between the two lane adaptors every edge carries one **`RadixWord`** per beat -- `R` complex samples,
one per lane -- as a `DataArray` of `R` `ComplexField[FixedField(W, I)]`.

- **The channel is an `ap_uint`.** By Waveflow convention a `StreamIF` carries raw words and the type
  lives at the endpoints. Each edge's `StreamIF` has `bitwidth = 2·W·R` for its own `W`: 128 bits
  through the transposer, 152, 168, 184, 200 after the stages at `L = 1024`, 216 out. One `RadixWord`
  is exactly one beat.
- **Unpacked, a word is AMD's `SuperSampleContainer<R, T>`.** `ComplexField` serializes as interleaved
  I/Q, the layout of `std::complex<ap_fixed<W, I>>`, so the vendor's arithmetic runs on the lanes
  directly.
- **Nothing packs by hand.** In C++ each task reads an `ap_uint<W>` and unpacks it with the generated
  `<elem>_array_utils` lane routines (wrapped per edge in the generated configuration header); in the
  pysim the same schema packs and unpacks. `waveflow.dsp.ssr_fft.types.edge_types(geo)` lists every
  edge with its format and width.
- **One block channel.** With `reorder="sob"`, the edge between the reorder's writer and reader is a
  `StreamOfBlocksIF` -- `hls::stream_of_blocks<ap_uint<W>[L/R], 2>` -- holding whole frames.

**Why the boundary is lanes, not one wide port.** The XSI BFMs move 64-bit words; the inside of the
FFT is 128 to 232 bits wide. The lane adaptors are two tasks that join four sample streams into one
`RadixWord` stream and split it again -- and they make the module a drop-in replacement for
`VitisFft`, which a wider single port would not be.

## Wiring it

Exactly as for `VitisFft`: one `StreamIF` per lane, the input lanes at `2 * in_w` bits and the output
lanes at the module's output width. From `waveflow/dsp/ssr_fft/testbench.py`:

```python
self.dut = SsrFft(name="ssr_fft", sim=self.sim, clk=self.clk, L=self.length, ...)
in_bw, out_bw = 2 * int(self.in_w), 2 * int(self.dut.out_fmt.W)
for j in range(R):
    i = StreamIF(name=f"in_if_{j}", sim=self.sim, clk=self.clk, bitwidth=in_bw)
    i.bind(ep_name="master", endpoint=self.drivers[j].stream_ep)
    i.bind(ep_name="slave", endpoint=self.dut.s_in[j])
    self.add_if(i)
    o = StreamIF(name=f"out_if_{j}", sim=self.sim, clk=self.clk, bitwidth=out_bw)
    o.bind(ep_name="master", endpoint=self.dut.m_out[j])
    o.bind(ep_name="slave", endpoint=self.sinks[j].stream_ep)
    self.add_if(o)
```

Inside a larger composite, it is one child like any other: wire its lanes to the neighbouring modules
with internal `StreamIF`s and name any lanes that cross your boundary in `boundary` -- see
[Including it in your design](build.md).

## The generated top

`SsrFft` is a composite, so `composite_top_spec` derives the whole top from its graph: one
`hls_thread_local` channel per internal interface, one `hls::task` per child, each child's
`kernel_task()` naming a generated wrapper. At `L = 64` with the SOB reorder (abridged):

```cpp
void ssr_fft(hls::stream<ap_uint<32> >& s_in_0, ..., hls::stream<ap_uint<46> >& m_out_3) {
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream<ap_uint<128> > lanes_in;
    hls_thread_local hls::stream<ap_uint<128> > tp0;
    ...
    hls_thread_local hls::stream<ap_uint<184> > rc;
    hls_thread_local hls::stream_of_blocks<ap_uint<184>[16], 2> blk;
    hls_thread_local hls::stream<ap_uint<184> > rr;
    hls_thread_local hls::task t0(ssr_fft_L64_16_2_18_2_lanes_in, s_in_0, s_in_1, s_in_2, s_in_3, lanes_in);
    hls_thread_local hls::task t1(ssr_fft_L64_16_2_18_2_tp0, lanes_in, tp0);
    ...
    hls_thread_local hls::task t11(ssr_fft_L64_16_2_18_2_lanes_out, rr, m_out_0, m_out_1, m_out_2, m_out_3);
}
```

Each channel's width is the edge's own; the channel names are the producing task's. Every task is a
plain function -- `ssr_fft_<config>_<task>` -- so its RTL instance is `<name>_U0`, which is what lets
a trace or a timing reader name an internal net without guessing.
