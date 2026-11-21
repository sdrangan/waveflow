---
title: StreamDemux
parent: Stream Blocks
nav_order: 3
audience: python
api: [StreamDemux, StreamDemuxStep]
summary: "StreamDemux: one framed stream out to n, a whole frame at a time, to the output a side stream names (s_sel, one word a frame -- what a StreamArbiter's m_sel carries) or a field of the frame's first word names (route='header'). The return half of a shared kernel: arbiter -> K -> demux returns every frame to its client, for any K that keeps frames in order. An index naming no output drops the frame and counts it. The outputs do not move in lockstep: a full output stalls them all. Its body, its build step and its RTL gates."
---

# StreamDemux

```python
from waveflow.streams import StreamDemux, StreamDemuxStep

dmx = StreamDemux(name="dmx", sim=sim, n_out=2, bitwidth=64, frame_bursts=1)                  # by s_sel
hdr = StreamDemux(name="hdr", sim=sim, n_out=4, bitwidth=64, route="header", sel_lsb=56, sel_bits=2)
```

One framed input, `s_in`; `n_out` framed outputs, `m_out_0` .. `m_out_<n_out-1>`. Each firing takes one
frame -- `frame_bursts` TLAST bursts -- and writes it whole to one output. Which one is chosen one of two
ways:

| `route` | the index | ports |
|---|---|---|
| `"sel"` (default) | one word on `s_sel` per frame, read before the frame | `s_in`, `s_sel`, `m_out_*` |
| `"header"` | bits `sel_lsb` .. `sel_lsb + sel_bits - 1` of the frame's first word, which is forwarded with the rest | `s_in`, `m_out_*` |

`n_out` is 2 to 16, `frame_bursts` 1 to 255, `sel_bits` 1 to 16. An index of `n_out` or more names no output:
the frame is read and **dropped**, and `dropped` counts it in pysim (the HLS body drops it too). The block
reports nothing, so a design that cannot lose a frame keeps its indices in range.

## When to use it

As the return half of a shared kernel: a [`StreamArbiter`](arbiter.md) merges the clients' frames into K and
writes each frame's input on `m_sel`; the demux reads that as `s_sel` after K and sends each of K's output
frames back. That works for any K that keeps frames in order, one frame out per frame in.

Route by a header field when the frame already says where it goes, and nothing beside it does: a response
whose header carries its client's id, say.

## The rule

**The outputs do not move in lockstep, but a full one stalls them all.** The block writes a frame to one
output; while that output's FIFO is full it waits, and the frames behind it -- for every other output --
wait too. Each client reads its output as it comes; a client that waits on something else first can stall
its siblings.

## In pysim

`run_iter` is one frame: its index (an `s_sel` burst, or the first burst's field), then its bursts to that
output. Output order per output is the input's, so pysim and RTL record the same words on every output.

## In HLS

The bodies, both in `stream_demux_task.h`, are copied into a build by `StreamDemuxStep`:

```cpp
template <int W, int NB, typename TI, typename TS, typename... TO>
static void stream_demux_task(hls::stream<TI>& s_in, hls::stream<TS>& s_sel, hls::stream<TO>&... m_out);

template <int W, int NB, int LSB, int BITS, typename TI, typename... TO>
static void stream_demux_hdr_task(hls::stream<TI>& s_in, hls::stream<TO>&... m_out);
```

The outputs are the pack, last in the signature; the generator supplies every port's word type, as for
[`StreamBroadcast`](broadcast.md). One firing reads the index and the frame's first word, then copies the
rest, a word a cycle (II = 1).

## Its gates

`waveflow.streams.rtl` builds the demux in its own harness (a driver on each input, a sink on each output)
both ways, and the arbiter-demux pair; each output's recording at RTL equals pysim's, and none is empty.

```bash
python -m waveflow.streams.rtl C:/wfb/dmx --block demux --n 3 --frame-bursts 2
python -m waveflow.streams.rtl C:/wfb/dmxh --block demux_hdr --n 3
```

Measured (Vitis 2025.1, xc7z020 at 100 MHz, 3 outputs, 32 bits): by `s_sel` with 2-burst frames, 5.73 ns
estimated, 253 FF and 492 LUT; by a header field, 3.18 ns, 261 FF and 422 LUT.

## See also

- [`StreamArbiter`](arbiter.md) -- the merging half, and the pair's gate.
- [Stream-only modules and adaptors](../systems/streams.md).
