---
title: The equalizer's stages
parent: Block equalizer
nav_order: 4
---

# The stages

The two stages and the composite are the last two sections of
[`block_eq.py`](../../../examples/block_eq/block_eq.py). Each stage is a
[`FreeRunMod`](../../guide/flows/concurrent_python.md) with framed stream ports, one firing per command,
like [the polynomial's kernel](../stream_kernel/kernel.md); the composite wires them behind the
framework's [`StreamBroadcast`](../../guide/streams/broadcast.md). Their HLS bodies are on
[The HLS bodies](hls.md).

## Why two stages {#why-two-stages}

The equalizer is two computations at different rates ([What an equalizer
does](theory.md#what-the-example-computes)):

| | once a | per command | the costly operation |
|---|---|---|---|
| `g = conj(h) / (\|h\|^2 + N)` | row | `nrows` | a division |
| `z = g r` | symbol | `nrows * m` | four multiplies, two adds |

The split is where the rate changes. In one loop over symbols, each row's division would sit on the path
of its first symbol; as two stages, each is a loop of its own -- `gain` a row an iteration, `apply` a
symbol an iteration, both at II=1 -- and they run concurrently: `gain` can work on the next command's
estimates while `apply` is still on this command's symbols, as far ahead as the FIFOs between them allow
([link depths](#link-depths)). What crosses the cut is small: one `GainHdr` and at most 64 `g` values a
command.

## One input, two consumers: the broadcast

Both stages need the command; `gain` needs the estimates and `apply` the symbols -- all on the one input.
`StreamBroadcast(2)` writes every word it reads to both outputs, TLAST included, so each stage gets a copy
of the whole stream and **skips** what is not its own: `gain` discards the symbols, `apply` the estimates.

The broadcast moves its outputs **in lockstep**: a word goes to both in the same firing, so a full output
stalls both. That imposes [the rule](../../guide/streams/broadcast.md#two-rules) both stages are written
around: **a consumer keeps consuming its own copy, and never waits on a sibling for something queued
behind its own unread data.**

- `gain` reads -- and drops -- its copy of the symbols. They are `apply`'s, but an unread copy would fill,
  stall the broadcast, and starve `apply` of the symbols it needs.
- `apply` discards its copy of the estimates **before** it waits for `g`. Waiting first, its copy would
  fill with estimates, the broadcast would stall, `gain` would never get the rest of the estimates, and
  `g` would never come.

In pysim neither violation hangs: a framed transfer there moves a whole burst at once, so a copy never
fills mid-burst. The rule is followed by construction, and a violation would show only at RTL -- as a
hang, or as a run that ends early and looks like success. The gates therefore compare every recorded
burst ([The system flow](system_flow.md#the-gates)).

## `gain` {#gain}

```python
def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)
    nrows, m = int(hdr.nrows), int(hdr.m)
    est = (yield from self.s_in.get_array(Float32, 3 * nrows)).val     # fewer if it ended early
    complete = len(est) // 3
    gr, gi = gain_golden(est[0::3][:complete], est[1::3][:complete], est[2::3][:complete])
    bad = np.flatnonzero(~(est[2::3][:complete] > 0))
    if len(bad):
        nvalid, code = int(bad[0]), StatusCode.BAD_NOISE
    elif complete < nrows:
        nvalid, code = complete, StatusCode.TLAST_EARLY
    else:
        nvalid, code = nrows, StatusCode.OK
    yield from self.m_g.write(GainHdr(tx_id=hdr.tx_id, nvalid=nvalid, code=code))
    if nvalid:
        yield from self.m_g.write_array(Float32, 2 * nvalid,
                                        array(Float32, _interleave(gr[:nvalid], gi[:nvalid])))
    # Keep consuming: the symbols are apply's, but this copy must be read, or the broadcast stalls.
    sym = (yield from self.s_in.get_array(Float32, 2 * nrows * m)).val
    if code != StatusCode.OK or len(sym) < 2 * nrows * m:
        yield from _halt(self)
```

Read the command and the estimates; compute `g` with the golden's own `gain_golden`; tell `apply`, in
`GainHdr`, which command, how many rows have a `g` (`nvalid`) and how `gain`'s part ended (`code`); send
the `g` values, real and imaginary interleaved, one row a word. `nvalid = 0` sends no `g` burst: a burst
cannot be empty. Then discard the symbols, and halt on any error.

`gain` does not answer anything itself. Its error -- a bad noise, estimates that end early -- travels in
`GainHdr`, and `apply`, the last stage, reports it: [the command travels with the
data](../../guide/systems/command_response.md).

## `apply` {#apply}

```python
def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)
    nrows, m = int(hdr.nrows), int(hdr.m)
    # Keep consuming, first: the estimates are gain's, but this copy must be read before waiting on
    # gain -- waiting first would stall the broadcast, and gain would never get the rest of them.
    yield from self.s_in.get_array(Float32, 3 * nrows)
    gh = yield from self.s_g.get_schema(GainHdr)
    nvalid = int(gh.nvalid)
    g = (yield from self.s_g.get_array(Float32, 2 * nvalid)).val if nvalid else np.zeros(0, f32)
    yield from self.m_out.write(Resp(tx_id=hdr.tx_id, nrows=nrows, m=m))
    sym = (yield from self.s_in.get_array(Float32, 2 * nrows * m)).val
    need, arrived = nvalid * m, len(sym) // 2
    k = min(need, arrived)
    if k:
        rows = np.arange(k) // m
        zr, zi = apply_golden(g[0::2][rows], g[1::2][rows], sym[0:2 * k:2], sym[1:2 * k:2])
        yield from self.m_out.write_array(Float32, 2 * k, array(Float32, _interleave(zr, zi)))
    if arrived < need:
        code = StatusCode.TLAST_EARLY
    else:
        code = StatusCode(int(gh.code))
    yield from self.m_status.write(Status(tx_id=hdr.tx_id, status=code))
    if code != StatusCode.OK:
        yield from _halt(self)
```

The order is the point: the estimates are discarded **before** `s_g` is read. Then the `Resp` header,
the results for the `k` symbols that both arrived and have a `g` (each symbol's row is `index // m`), and
the status: `TLAST_EARLY` if the symbols ran out before the rows that have a `g`, else `gain`'s code. This
is the protocol's [first cut in row-major order](protocol.md#errors-are-fatal): rows that `gain` cut stop
the results at `nvalid * m`; symbols that end earlier than that win.

`_halt` waits on an event nothing triggers: after an error the stage stops, as its RTL does.

## The composite

```python
self.bcast = StreamBroadcast(name=f"{self.name}_bcast", sim=self.sim, n_out=2, bitwidth=W, clk=self.clk)
self.gain = Gain(name=f"{self.name}_gain", sim=self.sim, clk=self.clk)
self.apply = Apply(name=f"{self.name}_apply", sim=self.sim, clk=self.clk)
for c in (self.bcast, self.gain, self.apply):
    self.add_comp(c)

def link(name, master, slave, depth):
    iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=W,
                     framed=True, depth=depth)
    iface.bind("master", master)
    iface.bind("slave", slave)
    self.add_if(iface)

link("est_gain", self.bcast.m_out_0, self.gain.s_in, 128)
link("est_apply", self.bcast.m_out_1, self.apply.s_in, 128)
link("g", self.gain.m_g, self.apply.s_g, 128)

self.boundary = ["s_in", "m_out", "m_status"]
self.s_in = self.bcast.s_in
self.m_out = self.apply.m_out
self.m_status = self.apply.m_status
```

Three children, three internal framed streams, and a boundary: the broadcast's input and `apply`'s two
outputs are the composite's ports ([Free-running composites](../../guide/comp_codegen/freerunning_composite.md)).
The composite has no behavior of its own; its children run.

### Link depths {#link-depths}

An internal link's `depth` is physical: the generated top declares each as an `hls::stream` with
`#pragma HLS STREAM depth=128`. Each is sized against what can pile up in it:

- **The two copies** (`est_gain`, `est_apply`): a command's estimates are at most `3 * 64` floats = 96
  words, plus the `CmdHdr`. A copy of 128 words holds them whole, so the broadcast can deliver the next
  command's estimates to `gain` while `apply` still has this command's symbols queued, rather than
  waiting on `apply` word for word.
- **The `g` link**: a `GainHdr` and at most 64 `g` words, 65 words. At 128 it holds one command's whole
  message, so `gain` finishes writing and goes on to discard its symbols without waiting for `apply` to
  read.

The depths set how far one stage can run ahead of the other; they are not what keeps the design out of
deadlock -- the keep-consuming order is. In fact a copy of 128 words would hide a wrong order in `apply`
for any command of up to 64 rows (its copy could hold all the estimates while it waited on `g`); the
rule is what makes the design correct at any depth.

Next, [The HLS bodies](hls.md): the same two stages in C++.
