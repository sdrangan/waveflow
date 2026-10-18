---
title: The equalizer's stages
parent: Block equalizer
nav_order: 2
---

# The stages

Everything is in [`block_eq.py`](../../../examples/block_eq/block_eq.py): the schemas, the golden, the
scenarios, the two stages and the composite. The HLS bodies are in [`src/`](../../../examples/block_eq/src).

## Why two stages, and a broadcast

`g` costs a division, once a row; `z` costs four multiplies and two adds, once a symbol. In one loop the
division would sit on every symbol's path; as two stages each runs at its own rate. Both need the
command, `gain` needs the estimates and `apply` the symbols -- all on one input. So the framework's
[`StreamBroadcast`](../../guide/streams/broadcast.md) gives each a copy of the whole stream, and each
**skips** what is not its own.

## `gain`

```python
def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)
    nrows, m = int(hdr.nrows), int(hdr.m)
    est = (yield from self.s_in.get_array(Float32, 3 * nrows)).val     # fewer if it ended early
    ...                                                                 # g per row; the first N <= 0
    yield from self.m_g.write(GainHdr(tx_id=hdr.tx_id, nvalid=nvalid, code=code))
    if nvalid:
        yield from self.m_g.write_array(Float32, 2 * nvalid, array(Float32, g))
    sym = (yield from self.s_in.get_array(Float32, 2 * nrows * m)).val # KEEP CONSUMING: discard
    if code != StatusCode.OK or len(sym) < 2 * nrows * m:
        yield from _halt(self)
```

`gain` tells `apply` three things in `GainHdr`: which command, how many rows have a `g` (`nvalid`), and how
its part ended (`code`). That is **the command travelling with the data**: `apply`, the last stage, answers
for both, including an error only `gain` could see.

Then `gain` reads -- and drops -- its copy of the symbols. They are `apply`'s, but the broadcast writes both
copies in lockstep: an unread copy would fill, stall the broadcast, and starve `apply`.

## `apply`

```python
def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)
    yield from self.s_in.get_array(Float32, 3 * nrows)                  # KEEP CONSUMING, FIRST
    gh = yield from self.s_g.get_schema(GainHdr)                        # then wait on gain
    ...
```

The order is the point. `apply` discards its copy of the estimates **before** it waits for `gain`. Had it
waited first, its copy would fill with estimates, the broadcast would stall, `gain` would never get the
rest of the estimates, and `g` would never come: a deadlock that looks, at RTL, like a hang. This is the
broadcast's [keep-consuming rule](../../guide/streams/broadcast.md#two-rules).

Then `Resp`, the results for the rows that have a `g`, and the status.

## The HLS bodies: one body, two placements

Each stage's body takes its ports' **word types** as template parameters
([`KernelTask(framed=...)`](../../guide/streams/broadcast.md#in-hls)):

```cpp
template <int W, typename TI, typename TO>
static void eq_gain_task(hls::stream<TI>& s_in, hls::stream<TO>& m_g);

template <int W, typename TI, typename TG, typename TO, typename TS>
static void eq_apply_task(hls::stream<TI>& s_in, hls::stream<TG>& s_g,
                          hls::stream<TO>& m_out, hls::stream<TS>& m_status);
```

The generator supplies the types where the body sits. Inside the composite, `gain` reads and writes
`framed_word<64>` FIFOs, and `apply` reads `framed_word<64>` and writes the top's `axi4s_word<64>` ports;
as a stage alone, every port is the top's `axi4s_word<64>`. The generated composite:

```cpp
hls_thread_local hls::task t0(stream_broadcast_task<64, axi4s_word<64>, framed_word<64>, framed_word<64>>, s_in, est_gain, est_apply);
hls_thread_local hls::task t1(eq_gain_task<64, framed_word<64>, framed_word<64>>, est_gain, g);
hls_thread_local hls::task t2(eq_apply_task<64, framed_word<64>, framed_word<64>, axi4s_word<64>, axi4s_word<64>>, est_apply, g, m_out, m_status);
```

A body reads and writes through the framework's helpers that take either word type -- a schema with
`streamutils::read_msg` / `write_msg`, a word of float32s with `float32_array_utils::read_boundary_lane` /
`write_boundary_lane` -- so it never names one ([One body, wherever it
sits](../../guide/comp_codegen/freerunning_override.md#framed)).

Inside, the bodies are the [lane loop](../../guide/vectorization/hls/loop_optimization.md): a word per
iteration at II=1, every float operation its own statement (no fused multiply-add), and a `static bool
halted` under `#pragma HLS reset` for the fatal error.

## The composite

```python
self.bcast = StreamBroadcast(name=f"{self.name}_bcast", sim=self.sim, n_out=2, bitwidth=W, clk=self.clk)
self.gain = Gain(name=f"{self.name}_gain", sim=self.sim, clk=self.clk)
self.apply = Apply(name=f"{self.name}_apply", sim=self.sim, clk=self.clk)
...
link("est_gain", self.bcast.m_out_0, self.gain.s_in, 128)     # StreamIF(framed=True, depth=128)
link("est_apply", self.bcast.m_out_1, self.apply.s_in, 128)
link("g", self.gain.m_g, self.apply.s_g, 128)
self.boundary = ["s_in", "m_out", "m_status"]
```

Next, [The system flow](system_flow.md).
