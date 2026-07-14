---
title: Defining a component
parent: Hardware Components
nav_order: 1
audience: python
applies_to: [HwComponent]
api: [HwComponent, add_endpoint, StreamIFSlave, StreamIFMaster]
summary: "Defining a HwComponent, by example: subclass HwComponent, declare its typed stream endpoints in __post_init__ with add_endpoint, and implement its behavior in run_proc — walked through a minimal moving-average filter that streams blocks of samples through. This intro version is a plain HwComponent for simulation only; the synthesizable form (a derived execution-model class with @synthesizable compute) comes on the later pages."
---
# Defining a component

A `HwComponent` is best shown by example. Consider a **moving-average filter** — a module with one
stream input and one stream output that emits, for each sample, the average of it and the sample before
it: `y[n] = ½·(x[n-1] + x[n])`. Samples arrive in fixed-size blocks, so the stream payload is a
[`DataArray`](../schema/) of `Float32`:

```python
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from waveflow.hw.dataschema import DataArray, FloatField
from waveflow.hw.hw_component import HwComponent
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.simulation.simobj import ProcessGen

Float32 = FloatField.specialize(bitwidth=32)

class Samples(DataArray):
    """The stream payload: a block of 8 Float32 samples."""
    element_type = Float32
    static = True
    max_shape = (8,)


@dataclass
class MovingAvg(HwComponent):
    cpp_kernel_name: ClassVar[str | None] = "moving_avg"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in  = StreamIFSlave( name=f"{self.name}_x_in",  sim=self.sim, bitwidth=32)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.x_in)
        self.add_endpoint(self.y_out)

    def run_proc(self) -> ProcessGen[None]:
        xprev = 0.0                                        # last sample of the previous block
        while True:
            x = yield from self.x_in.get(Samples)          # one block of samples
            xv = x.val                                     # the block as a NumPy vector
            xshift = np.concatenate(([xprev], xv[:-1]))    # x[n-1], carrying across the block edge
            y = 0.5 * (xshift + xv)                        # y[n] = ½·(x[n-1] + x[n]), no loop
            xprev = xv[-1]
            yield from self.y_out.write(Samples(y))
```

That is the whole component. It is defined by the three things this section is organized around — its
**endpoints**, **how it is wired** (by binding those endpoints to [interfaces](../interface/)), and
**what it does**. The rest of this page walks the parts.

> **This is a simulation model, not synthesizable form.** `MovingAvg` is a *plain* `HwComponent`, and its
> `run_proc` computes with NumPy — it runs in [simulation](../sim/) but is **not** shaped for HLS. A
> component becomes **synthesizable** two ways together: it subclasses one of the execution-model
> [kinds](./taxonomy.md) (`HostActivated` / `FreeRunComp` / `CompositeComp`), *and* its compute moves into
> an [`@synthesizable`](../../../waveflow/hw/synth.py) method written in the array-operator idiom rather
> than raw NumPy. [Free-running components](./freerun.md) shows a synthesizable free-running kernel in
> that form; giving *this* moving-average filter a synthesizable form additionally needs cross-iteration
> state (`xprev`), which is still being built — so it stays a sim-only example. This page is only about
> the *shape* every component shares — endpoints, wiring, behavior — which is the same either way.

## The class

`MovingAvg` subclasses [`HwComponent`](../../../waveflow/hw/hw_component.py) and is a dataclass;
`cpp_kernel_name` names the generated kernel. Call `super().__post_init__()` **first**. Runtime and
simulation-only values are plain fields; synthesis knobs use [`HwParam` / `HwConst`](./parameterization.md)
— for instance you could make the block size a `HwParam` so *one* class describes a 4-, 8-, or 16-sample
filter (that also parameterizes the `Samples` payload, which is why it is left fixed here — see
[Parameterization](./parameterization.md)).

## Declaring endpoints

A component's ports are **interface endpoints**, constructed in `__post_init__` and registered with
[`add_endpoint`](../../../waveflow/hw/component.py) (which records the endpoint and back-links it to the
component). `MovingAvg` declares two: a [`StreamIFSlave`](../interface/stream.md) it reads from and a
[`StreamIFMaster`](../interface/stream.md) it writes to, each carrying the `Samples`
[schema](../schema/). Which endpoint *types* exist (stream, `m_axi`, regmap, transfer), their
master/slave roles, and the transaction methods each offers are the [Interfaces](../interface/) section.

## Behavior: `run_proc`

A free-running component implements **`run_proc`** — a long-lived loop over its ports. Each iteration
`get`s one payload from the input stream, computes, and `write`s one to the output. State that persists
across iterations — here `xprev`, the last sample of the previous block (the "moving" part) — is carried
in a local declared before the loop. The `get` / `write` transaction methods belong to the endpoints;
their signatures are [Stream](../interface/stream.md).

*Which* lifecycle method you implement is what distinguishes the [kinds of component](./taxonomy.md):
`run_proc` for a free-running datapath like this, `on_start` for a regmap-launched one, and none at all
for a composite.

## Next: making it hardware

The [taxonomy](./taxonomy.md) is the map of the kinds; the [Free-running components](./freerun.md) page
shows a synthesizable free-running kernel (`Square`), and how any component lowers to a Vitis C++
kernel is [Component Code Generation](../comp_codegen/).
