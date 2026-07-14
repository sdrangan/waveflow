---
title: Free-running components
parent: Hardware Components
nav_order: 4
audience: python
applies_to: [FreeRunComp]
api: [FreeRunComp, StreamIFSlave, StreamIFMaster, synthesizable]
summary: "FreeRunComp — the synthesizable HwComponent that runs continuously: you implement run_iter (one firing) and the base repeats it forever, lowering to a free-running ap_ctrl_none hls::task. Walked through a stateless Square kernel (y = x² over an n-vector) whose compute lives in a @synthesizable pure function using the array operators. Cross-iteration state is a work in progress."
---

# Free-running components

## What it is

A [`FreeRunComp`](../../../waveflow/hw/hw_freerun.py) is a synthesizable `HwComponent` that runs
**continuously** — a streaming datapath rather than a host-triggered one. You implement **`run_iter`**:
*one firing*, the work for a single job. The base repeats it forever (`run_proc` is a `while True` over
`run_iter`, the discrete-event stand-in for the hardware re-firing). It lowers to a free-running
**`ap_ctrl_none`** `hls::task` — a block with no control handshake that the runtime re-fires per job.
Contrast with a [host-activated](./hostactivated.md) kernel, which runs once per `ap_start`.

## A simple example — `Square`

`Square` squares an *n*-vector each firing — `y = x²`, element-wise:

```python
from dataclasses import dataclass
from typing import ClassVar

from waveflow.hw.dataschema import DataArray, FloatField
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen

Float32 = FloatField.specialize(bitwidth=32)

class Vec(DataArray):
    """The stream payload: an n-vector of Float32 (here n = 4)."""
    element_type = Float32
    static = True
    max_shape = (4,)


@dataclass
class Square(FreeRunComp):
    cpp_kernel_name: ClassVar[str | None] = "square"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in  = StreamIFSlave( name=f"{self.name}_x_in",  sim=self.sim, bitwidth=32)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.x_in)
        self.add_endpoint(self.y_out)

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.x_in.get(Vec)      # one n-vector
        y = self.square(x)
        yield from self.y_out.write(y)

    @synthesizable
    def square(self, x: Vec) -> Vec:
        return x * x                           # element-wise y = x², the array-operator idiom
```

Two things make this the **synthesizable** form, where the [moving-average](./overview.md) intro was
not:

- **`run_iter` is one firing.** It reads one payload, computes, writes one — no hand-rolled `while`
  loop. The looping is the runtime's job.
- **The compute is a `@synthesizable` pure function.** `square` takes its input as an *argument* and
  returns the result using the type-preserving [array operators](../vectorization/) (`x * x`), not raw
  NumPy. A `@synthesizable` method may read its arguments, endpoints, reg-maps, and `HwParam` values —
  but **not** mutable `self.X` state (the extractor rejects it). Keeping the math pure is what lets it
  lower to hardware.

## State comes later

`Square` is **stateless**: each firing depends only on its input. Components that must carry state
*across* firings — a moving average's `xprev`, a running accumulator — are a **work in progress**: the
generated form of cross-iteration state is not built yet (the extractor forbids reading mutable
`self.X` from `run_iter`). For now, keep such state in the simulation model and expect the synthesizable
story to firm up. This is why the intro's `MovingAvg` stays a sim-only example.

*How* a `FreeRunComp` lowers to an `hls::task` and is verified is the [realization flows](../flows/)
section.
