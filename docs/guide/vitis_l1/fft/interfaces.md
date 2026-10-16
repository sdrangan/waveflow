---
title: FFT interfaces
parent: The Vitis FFT
grand_parent: Vitis L1 Blocks
nav_order: 2
audience: python
api: [VitisFft, StreamIF]
summary: "VitisFft's ports: an R-wide AXI-Stream port group, R slaves in and R masters out, one complex sample per word with the real part in the low bits. The lane layout (sample n on lane n % R), the word widths (the output is wider than the input), how frames are delimited, how to wire the group with StreamIFs in Python, and the C++ signature the generated top calls."
---

# Interfaces

## An `R`-wide port group

The vendor DUT is

```cpp
void fft_top(hls::stream<T_in> p_in[R], hls::stream<T_out> p_out[R]);
```

so `VitisFft` exposes an **`R`-wide AXI-Stream port group** -- `R` slaves in, `R` masters out -- not
one port carrying `R` samples per word:

| port | direction | endpoint | word |
|---|---|---|---|
| `s_in_0 .. s_in_3` | in | `StreamIFSlave` | `2 * in_w` bits: one input sample |
| `m_out_0 .. m_out_3` | out | `StreamIFMaster` | `2 * out_w` bits: one output sample |

Each is available two ways, one object: as a list (`fft.s_in[j]`, `fft.m_out[j]`) for loops, and as
an attribute (`fft.s_in_0`, ...) because `KernelTask` signatures and `BfmModel` port lists name
endpoints by attribute.

## Lanes and words

- **Lane layout.** Sample `n` of a frame travels on lane `n % R` at word `n // R`, on both sides: lane
  `j` carries samples `j, j+R, j+2R, ...`. Output order is natural, so `X[k]` leaves on lane `k % R`.
  A frame is `L/R` words per lane.
- **Packing.** One complex sample per word, **real part in the low `W` bits**, imaginary above --
  `std::complex`'s member order, which is what the C++ body unpacks. Each half is a two's-complement
  stored `ap_fixed` integer.
- **Widths.** The input word is `2 * in_w` bits; the output word is `2 * out_w`, and `out_w` grows with
  `L` (`in_w + log2 L + 1`: 21 bits at `L = 16`, 27 at 1024, so a 54-bit output word). Read it off
  `fft.out_fmt.W` rather than computing it.
- **Frames** are delimited by **count**: the hardware takes exactly `L/R` words per lane per frame.
  The Python endpoints are declared `has_tlast=True`, so a pysim burst *is* a frame, but the generated
  top's ports are plain `ap_uint` streams with no TLAST pin -- the vendor core needs none.

`waveflow.vitis_l1.hw` packs and unpacks with `_pack_complex(re, im, W)` /
`_unpack_complex(words, W)`, the same convention, vectorized over a lane's words.

## Wiring it

Bind each lane with its own `StreamIF`, the input lanes at `2 * in_w` bits and the output lanes at the
module's own output width. From the testbench graph (`waveflow/vitis_l1/testbench.py`), four drivers to
the FFT to four sinks:

```python
self.dut = VitisFft(name="vitis_fft", sim=self.sim, clk=self.clk, L=self.length, ...)
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

**Move the lanes concurrently.** The four lanes are parallel ports, so whatever drives or drains them
must move them at the same time -- `VitisFft` itself reads and writes all four in parallel processes.
A producer that fills lane 0, then lane 1, ... makes a frame cost `L` cycles instead of `L/R`, and the
block's interval becomes unobservable behind it (measured: an interval of 8 read as 16).

Inside a larger composite, the FFT is one child like any other: wire its lanes to the neighbouring
modules with internal `StreamIF`s (their widths come from the interfaces), and expose any lanes that
cross the composite's boundary as attributes named in `boundary` -- see
[Including it in your design](build.md).

## What the generated top calls

`kernel_task()` hands the generator the body and its arguments; the top instantiates one
`hls::task` per `VitisFft`:

```cpp
template <int L, int IN_W, int IN_I, int TW_W, int TW_I, int SCALING, int ORDER, int OUT_W>
void vitis_fft_task(hls::stream<ap_uint<2 * IN_W> >& s_in_0, ..., hls::stream<ap_uint<2 * IN_W> >& s_in_3,
                    hls::stream<ap_uint<2 * OUT_W> >& m_out_0, ..., hls::stream<ap_uint<2 * OUT_W> >& m_out_3);

// in the generated top, for L = 16:
hls_thread_local hls::task t0(vitis_fft_task<16, 16, 2, 18, 2, 0, 0, 21>,
                              s_in_0, s_in_1, s_in_2, s_in_3, m_out_0, m_out_1, m_out_2, m_out_3);
```

The body is an adapter: `R` pump processes turn the raw words into `std::complex<ap_fixed>` streams
(the vendor core wants an *array* of streams, which separate stream objects cannot form), the vendor
core runs, and `R` pumps pack the result back. `OUT_W` is `static_assert`ed against the vendor's own
output type, so a wrong derived width is a compile error, not a wrong answer.
