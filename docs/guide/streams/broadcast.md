---
title: StreamBroadcast
parent: Stream Blocks
nav_order: 1
audience: python
api: [StreamBroadcast, StreamBroadcastStep]
summary: "StreamBroadcast: one framed stream in, n_out copies out -- every word written to every output, TLAST included, so the framing travels with the data and the block knows nothing about messages. The way to give two stages each a part of one stream: each reads its copy and skips what is not its own. Two rules a design must respect: the outputs move in lockstep (a full output stalls them all), and a consumer must keep consuming its own copy, never waiting on a sibling for something queued behind its own unread data. Its body is templated on each port's word type; how to use it in a composite and build it."
---

# StreamBroadcast

```python
from waveflow.streams import StreamBroadcast, StreamBroadcastStep

bcast = StreamBroadcast(name="bcast", sim=sim, n_out=2, bitwidth=64)
```

One framed input, `s_in`; `n_out` framed outputs, `m_out_0` .. `m_out_<n_out-1>`, all `bitwidth` wide.
Every word read is written to **every** output, TLAST included. The framing travels with the data, so
each output carries exactly the input's bursts: the block knows nothing about headers, messages or
commands.

## When to use it

When two stages each need a part of one stream. A receiver's equalizer is the typical case: one input
carries a command, the channel estimates and the symbols; the stage that computes the equalizer needs the
command and the estimates, the stage that applies it needs the command and the symbols.

```text
s_in --> [ StreamBroadcast(2) ] --m_out_0--> [ gain  ] --g--> [ apply ] --> m_out, m_status
                                --m_out_1-------------------> [ apply ]
```

Each consumer reads its copy and **skips** what is not its own: `gain` reads the command and the
estimates and discards the symbols; `apply` discards the estimates. So each consumer knows the input's
protocol, and each pays a read loop over the other's data -- cheap, since the broadcast moves one word a
cycle anyway. A skip stops at an early TLAST, like any read.

## Two rules

**The outputs move in lockstep.** A word is written to every output in the same firing, so if one
output's FIFO is full, every output waits. The slowest consumer paces the rest, within the depths of the
FIFOs between them.

**A consumer must keep consuming its own copy.** It may not wait on a sibling for something that only
comes after data still queued in its own FIFO. In the equalizer, `apply` must discard the estimates
**before** it waits for `g`. Waiting first would deadlock: once its FIFO filled with estimates, the
broadcast would stall, `gain` would never get the rest of the estimates, and `g` would never come. In the
right order there is nothing to wait for: `gain` has the estimates before `apply` needs `g`.

A deadlock at RTL looks like a hang -- or like success. Gate on counts: every output burst recorded, every
status written.

## In pysim

`run_iter` reads one burst (`s_in.get()`: a framed read is one burst) and writes it to each output in
order. The bursts each output carries are the RTL's, bit for bit and TLAST for TLAST.

## In HLS

The body, `stream_broadcast_task.h`, is copied into a build by `StreamBroadcastStep`:

```cpp
template <int W, typename TI, typename... TO>
static void stream_broadcast_task(hls::stream<TI>& s_in, hls::stream<TO>&... m_out);
```

One firing reads a word and writes it to every output. `W` comes from the block's `bitwidth`; `TI` and
each `TO` are the ports' **word types**, supplied by the generator where the block sits (`KernelTask.framed`):
`streamutils::axi4s_word<W>` on a top's port, `streamutils::framed_word<W>` on an internal FIFO. So the
same body is the top of the block's own harness, the first stage of a composite whose input is the top's
port, and a stage deep inside one. The number of outputs is the length of the pack: the generator binds
the ports one by one.

In a composite, add the step beside the design's other codegen steps:

```python
dag.add(StreamBroadcastStep(output_dir=INCLUDE_DIR))
```

## Its gate

`waveflow.streams.rtl` builds the block in its own harness -- a stream driver on `s_in`, a sink on each
output -- and runs the system flow: its `codegen` step (`StreamBlockCodegenStep`: the stream helpers, the
body by `StreamBroadcastStep`, the top), then `csynth`, `system_rtl`, `scenario`, `pysim`, `system_xsi`,
`compare`.
The gate: every output's recording at RTL equal to pysim's, and pysim's equal to the input's bursts.

```bash
python -m waveflow.streams.rtl C:/wfb/b2 --n-out 2 --bitwidth 32
```

## See also

- [Stream-only modules and adaptors](../patterns/stream_only.md) -- the rule this block serves.
- [Command-response](../patterns/command_response.md) -- the protocol its consumers usually parse.
