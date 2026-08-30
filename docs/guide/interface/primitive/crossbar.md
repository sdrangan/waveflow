---
title: Crossbar Interfaces
parent: Primitive interfaces
grand_parent: Interfaces
nav_order: 6
audience: python
api: [CrossBarIF, CrossBarIFInput, CrossBarIFOutput]
summary: "The n-input x m-output switching fabric — CrossBarIF, its routing function, and a runnable 2x2 example. Split out of the stream page, which is about the point-to-point StreamIF and the four ways to move data over one."
---

# Crossbar Interfaces

A [`StreamIF`](./stream.md) connects exactly one master to one slave. When several producers must
reach several consumers over one fabric, `CrossBarIF` is the switching version: `nports_in` inputs,
`nports_out` outputs, and a routing function that decides which burst goes where.

Everything on the [stream page](./stream.md) about **what** you can move — raw words, an array, a
schema — applies here unchanged; only the topology differs.

`CrossBarIF` routes bursts from `nports_in` input ports to `nports_out` output ports via a configurable routing function.

## Classes

| Class | Role | Key parameters |
|---|---|---|
| `CrossBarIF` | Interface | `clk`, `bitwidth`, `latency_init`, `nports_in`, `nports_out`, `route_fn` |
| `CrossBarIFInput` | Input (master) endpoint | `bitwidth` |
| `CrossBarIFOutput` | Output (slave) endpoint | `bitwidth`, `rx_proc`, `queue_size` |

Endpoint names follow the pattern `in_0`, `in_1`, …, `out_0`, `out_1`, …

## Routing function

The `route_fn(words, port_in) -> port_out` callable maps each burst to an output port. If not provided, the default is `port_out = port_in % nports_out`.

```python
def route_by_first_word(words: Words, port_in: int) -> int:
    return int(words[0]) % nports_out
```

## Example: 2×2 crossbar

```python
from waveflow.hw.interface import CrossBarIF, CrossBarIFInput, CrossBarIFOutput

xbar = CrossBarIF(
    sim=sim,
    clk=clk,
    nports_in=2,
    nports_out=2,
    bitwidth=32,
    latency_init=2.0,
    route_fn=route_by_first_word,
)

xbar.bind("in_0",  src0.input_ep)
xbar.bind("in_1",  src1.input_ep)
xbar.bind("out_0", sink0.output_ep)
xbar.bind("out_1", sink1.output_ep)
```

The crossbar's `write(words, port_in)` is called internally by `CrossBarIFInput.write(words)` — callers only need the input endpoint's `write` method.

Each `CrossBarIFOutput` endpoint has the same `run_proc()` loop as `StreamIFSlave` and must be started before transfers are sent.

---

---

## How it lowers

**Internal only**, like [stream-of-blocks](./sob.md): a `CrossBarIF` has no boundary kind and never
becomes a port. It is the n × m stream fabric *inside* a design, and each of its edges lowers as the
ordinary stream it is.

- **HLS** — [Endpoint interfaces](../../comp_codegen/interface.md#stream-endpoints--axis) for the
  per-edge lowering; there is no crossbar object in the generated C++.
- **BFM / XSI — none.** Nothing here is a pin, so nothing here has a dual.
