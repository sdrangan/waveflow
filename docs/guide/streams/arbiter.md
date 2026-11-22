---
title: StreamArbiter
parent: Stream Blocks
nav_order: 2
audience: python
api: [StreamArbiter, StreamArbiterStep, wait_any]
summary: "StreamArbiter: n framed streams into one, a whole frame at a time -- the way several clients share one stream, and so one kernel behind it. A frame is frame_bursts TLAST bursts (1 for a message, 2 for a header and its payload); the policy is strict priority (input 0 first) or round-robin, fixed at build time; the winner's index goes out on m_sel, one word before each frame, so a StreamDemux after the kernel can return each frame to its client. Two rules: announce a frame only once it is all there, and read m_sel as the frames go by. Its interleave depends on timing, so its RTL gate is the arbiter-demux pair. Why it is written in HLS rather than taken from AMD (the AXI4-Stream Switch, the Vitis Utility Library's streamNToOne)."
---

# StreamArbiter

```python
from waveflow.streams import StreamArbiter, StreamArbiterStep

arb = StreamArbiter(name="arb", sim=sim, n_in=2, bitwidth=64, frame_bursts=2, policy="priority")
```

`n_in` framed inputs, `s_in_0` .. `s_in_<n_in-1>`; one framed output, `m_out`; and a side output, `m_sel`.
Each firing picks one input holding a frame, writes that input's index on `m_sel` (one word, TLAST set),
then copies the frame to `m_out` **whole**. Frames are never interleaved: once a frame starts, the block
copies it to its last word, whatever the other inputs hold. All ports are `bitwidth` wide.

| parameter | meaning |
|---|---|
| `n_in` | inputs, 2 to 16 |
| `bitwidth` | the data width of every port |
| `frame_bursts` | TLAST bursts a frame, 1 to 255: 1 for a self-contained message, 2 for a header then its payload (`Job` then its words) |
| `policy` | `"priority"`: the lowest-numbered input holding a frame wins, so input 0 is the most urgent; `"round_robin"`: the search starts one past the last winner, so an input holding a frame waits at most `n_in - 1` frames |

The block counts TLASTs and reads nothing else, so it knows no message format.

## When to use it

When several clients share one stream, and so one kernel behind it: new jobs and the kernel's own
feedback frames into one FFT, several channels' requests into one engine. The shape is

```text
s_in_0 --+                                     +--> m_out_0
s_in_1 --+--> [ StreamArbiter ] --m_out--> K --+--> [ StreamDemux ] --> m_out_1
              [               ] --m_sel---------------> s_sel
```

`m_sel` travels beside K, one word a frame, and a [`StreamDemux`](demux.md) reading it as `s_sel` returns each
of K's output frames to the client that sent it. That works for any K that keeps frames in order and one
frame out per frame in, including one that knows nothing of tags (an FFT framed by count). A kernel that
needs to know more than the input's index -- a job id, a pass number -- gets it from the design: a tag the
client puts in the frame's header, or a tag stream of the design's own beside K.

Replicating K is the alternative whenever K is small; share it when it is the expensive part.

## Two rules

**Announce a frame only when it is all there.** The block commits to a frame on its first word and then
waits for the rest, so an input whose frame is still being produced stalls every other input meanwhile. A
feedback path in particular buffers a frame and writes it whole (store and forward), so the arbiter never
commits to a frame that is still being computed.

**Read `m_sel` as the frames go by.** The block writes the index before each frame, so a full `m_sel` FIFO
stops it. Size the link carrying it for the frames K holds at once.

A deadlock looks like a hang -- or like success. Gate on counts: every frame out, every status written.

## In pysim

`run_iter` is one firing: it waits with `wait_any` until one of its inputs holds a burst, in the order this
firing searches them, takes the first, and writes its index on `m_sel`, then `frame_bursts` bursts on
`m_out`. `wait_any` (`waveflow.hw.interface`, also `FreeRunMod.wait_any`) is the pysim form of a body that
polls several inputs with `empty()`: the HLS task spins, a model cannot (a harness runs until no event is
left), so it waits instead. A body that polls waits only on the inputs it can act on: an input it would not
read this firing (a new job without a credit, say) makes `wait_any` return at once, forever.

```python
yield from self.wait_any(self.s_fb, self.s_in)
tag = yield from self.s_fb.get_schema_nb(Tag)     # feedback first
```

Which input wins depends on when frames arrive, so a run's interleave is a fact about its timing: pysim and
RTL agree per input, not on the order of frames on `m_out`.

## In HLS

The body, `stream_arbiter_task.h`, is copied into a build by `StreamArbiterStep`:

```cpp
template <int W, int NB, int RR, typename TO, typename TS, typename... TI>
static void stream_arbiter_task(hls::stream<TO>& m_out, hls::stream<TS>& m_sel, hls::stream<TI>&... s_in);
```

`W` is `bitwidth`, `NB` is `frame_bursts`, `RR` is the policy (0 priority, 1 round-robin). `TO`, `TS` and each
`TI` are the ports' word types, supplied by the generator where the block sits, as for
[`StreamBroadcast`](broadcast.md). The inputs are the pack, so they come last in the signature. One firing
tests every input with `empty()`; if none holds a word it does nothing and the task re-fires. The
round-robin pointer is a `static` with `#pragma HLS reset`.

Keep its copy loop's shape, the count of TLASTs tested at the head (`while (b < NB)`, an 8-bit count). Two
rewrites meant to shorten its path passed pysim and **hung at RTL** after an input's last frame (Vitis
2025.1): a `for (;;)` with a `break` after the read, and a `done` flag computed a burst ahead.

In a composite, add the step beside the design's other codegen steps:

```python
dag.add(StreamArbiterStep(output_dir=INCLUDE_DIR))
```

## Its gate

The arbiter has no RTL gate alone: its interleave depends on when the stand-ins deliver, so its recordings
need not equal pysim's. Its gate is `ArbiterDemuxPair` in `waveflow.streams.rtl` -- the arbiter's `m_out` and
`m_sel` into a demux -- whose every output carries one input's frames in order, whatever the interleave: the
frames whole, none lost, every index right. Under both policies; the policies themselves are checked in
pysim, on inputs held backlogged (`tests/streams/test_arbiter_demux.py`).

```bash
python -m waveflow.streams.rtl C:/wfb/pair --block pair --n 3 --frame-bursts 2 --policy priority
```

Measured (Vitis 2025.1, xc7z020 at 100 MHz, 3 inputs, 32 bits): every loop at II = 1; the pair, 1 frame
burst and round-robin, 7.26 ns estimated, 494 FF and 1000 LUT; 2 frame bursts and priority, 7.82 ns --
over the 7.3 ns the 2.7 ns uncertainty leaves -- 581 FF and 995 LUT.

**On the RFSoC at 250 MHz** (xczu48dr-ffvg1517-2-e, 4 ns; csynth Vitis HLS 2025.1, out-of-context place
and route Vivado 2025.1; 3 inputs, 32 bits, the pair): every case meets 4 ns after routing, and the
100 MHz estimate does not carry over.

| pair | csynth estimate | routed critical path | WNS | LUT / FF routed |
|---|---|---|---|---|
| priority, 1 burst | 2.655 ns | 2.609 ns | +1.391 ns | 519 / 766 |
| priority, 2 bursts | 2.655 ns | 2.613 ns | +1.387 ns | 537 / 883 |
| round-robin, 1 burst | 2.787 ns | 3.112 ns | **+0.888 ns** | 555 / 787 |
| round-robin, 2 bursts | 2.787 ns | 2.109 ns | +1.891 ns | 569 / 904 |

The schedule is the 100 MHz one (the same states, II and depths). csynth's slowest chains -- the
demux's index test, the round-robin pointer's update -- are not the routed worst paths: those end on
the register slices Vitis puts on the top's AXI-Stream ports and are 80-93% routing, so the spread
between equal designs (about 1 ns) is placement, not logic depth. Only 3 inputs were measured; the
search grows with `n_in` (`plans/shared_kernel_rfsoc_timing.md`).

## Why not AMD's

- The **AXI4-Stream Switch / Interconnect** (PG085) arbitrates on TLAST, a whole packet a grant, with fixed
  priority or round-robin. But it is RTL IP joining IPs in a block design: it cannot sit between the
  `hls::task`s of one HLS top. Between devices, on real AXI4-Stream, it is the right tool, unmodified.
- The **Vitis Utility Library's `streamNToOne`** (`xf::common::utils_hw`: round-robin, load balance, tag
  select) is HLS, but moves **one element** an input a turn -- frames from different inputs interleave --
  and has no notion of a packet or TLAST. It is a function that returns when every input has raised its end
  flag (`e_istrms`), not a free-running task. Read on its `main` branch, 2026-11-21
  (`utils/L1/include/xf_utils_hw/stream_n_to_one/`).

## See also

- [`StreamDemux`](demux.md) -- the return half.
- [Stream-only modules and adaptors](../systems/streams.md).
- [Command-response](../systems/command_response.md) -- the protocol a shared kernel's clients speak.
