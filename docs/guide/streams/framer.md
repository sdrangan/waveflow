---
title: StreamFramer
parent: Stream Blocks
nav_order: 4
audience: python
api: [StreamFramer, StreamFramerStep]
summary: "StreamFramer: a stream framed by count alone (no TLAST) into a framed one, TLAST every frame_words words, the words unchanged. Put it after a unit that frames by count -- SsrFft's L/R words a frame -- so a StreamDemux, or any stage that reads whole frames, can sit after it. A word a cycle, a cycle of latency, nothing in rate; it drains its input whenever its output has room, so a link into it is no loop hazard. Its body, its build step and its RTL gate."
---

# StreamFramer

```python
from waveflow.streams import StreamFramer, StreamFramerStep

frm = StreamFramer(name="frm", sim=sim, frame_words=16, bitwidth=128)   # after SsrFft(L=64), R = 4
```

One input, `s_in`: a plain word stream, no TLAST. One output, `m_out`: the same words, framed, TLAST on
every `frame_words`-th word. The block reads nothing but the count, so it knows no message format.

| parameter | meaning |
|---|---|
| `frame_words` | words a frame, at least 1: the count the unit before it frames by (`SsrFft`: `L/R`) |
| `bitwidth` | the data width of both ports |

## When to use it

After a unit whose output is framed by count alone. `SsrFft`'s output is `L/R` words a frame with no
TLAST, so nothing after it can route by frame: a [`StreamDemux`](demux.md) needs TLAST to know where a
frame ends. Without the framer, the stage after the FFT has to read every frame itself and decide where
it goes -- one stage doing the reduction, the feedback and the answers serially, which is where a shared
FFT's throughput goes (the stage after it, not the FFT, becomes the bottleneck). With it:

```text
... -> SsrFft --(L/R words, no TLAST)--> StreamFramer --(framed)--> StreamDemux --> feedback stage
                                                                               \--> answer stage
```

It is not needed when the stage after the unit reads every frame anyway and is fast enough: then that
stage counts the words itself.

## In pysim

`run_iter` is one frame out: it takes whole bursts until it holds `frame_words` words, then writes them.
Words past the frame are kept for the next, so a frame may arrive over several bursts and a burst may
hold several frames -- the block's own scenario has both.

## In HLS

`stream_framer_task.h`, copied into a build by `StreamFramerStep`:

```cpp
template <int W, int N, typename TO>
static void stream_framer_task(hls::stream<ap_uint<W> >& s_in, hls::stream<TO>& m_out);
```

`W` is `bitwidth`, `N` is `frame_words`, `TO` the output's word type, supplied by the generator where the
block sits, as for [`StreamBroadcast`](broadcast.md). One firing is one frame: `N` words, II = 1, TLAST on
the last. Nothing is decided, so the copy is the whole body.

It declares `drains_inputs`: it reads whenever its output has room, so a link into it on a cycle is not a
loop hazard by itself ([A cycle inside a composite](../systems/shared_kernels.md#a-cycle-inside-a-composite)).

## Its gate

`waveflow.streams.rtl` builds it in its own harness (a driver on the unframed input, a sink on the framed
output); the output's recording at RTL equals pysim's, TLAST for TLAST
(`tests/streams/test_framer.py`, `-m xsi`).

```bash
python -m waveflow.streams.rtl work/frm16 --block framer --frame-bursts 16
```

(`--frame-bursts` carries the frame length for this block.)

## See also

- [`StreamDemux`](demux.md) -- what it makes possible after a count-framed unit.
- [SSR FFT interfaces](../dsp/ssr_fft/interfaces.md) -- the FFT's count-framed output.
- [Sharing a kernel](../systems/shared_kernels.md).
