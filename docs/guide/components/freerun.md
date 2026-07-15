---
title: Free-running components
parent: Hardware Components
nav_order: 4
audience: python
applies_to: [FreeRunComp]
api: [FreeRunComp, StreamIFSlave, StreamIFMaster, synthesizable]
summary: "FreeRunComp — the synthesizable HwComponent that runs continuously: you implement run_iter (one firing) and the base repeats it forever; the intended target is a free-running ap_ctrl_none hls::task. Walked through the tested, stateless Square toy (y = x² over an n-vector) whose compute lives in a @synthesizable pure function using the array operators. Square is a pysim model in synthesizable form, not a generated kernel — auto-extraction of run_iter is a known gap. Cross-iteration state is a work in progress."
---

# Free-running components

## What it is

A [`FreeRunComp`](../../../waveflow/hw/hw_freerun.py) is a synthesizable `HwComponent` that runs
**continuously** — a streaming datapath rather than a host-triggered one. You implement **`run_iter`**:
*one firing*, the work for a single job. The base repeats it forever (`run_proc` is a `while True` over
`run_iter`, the discrete-event stand-in for the hardware re-firing). Its **intended** realization is a
free-running **`ap_ctrl_none`** `hls::task` — a block with no control handshake that the runtime
re-fires per job. Contrast with a [host-activated](./hostactivated.md) kernel, which runs once per
`ap_start`.

That target is what the class *declares* (`control_mode = FREE_RUNNING`), not yet what codegen emits —
see [what this example claims](#what-this-example-claims) below.

## A simple example — `Square`

[`examples/toy/toy.py`](../../../examples/toy/toy.py) squares an *n*-vector each firing — `y = x²`,
element-wise:

```python
Float32 = FloatField.specialize(bitwidth=32)


class Vec(DataArray):
    """The stream payload: an n-vector of Float32 (here n = 4)."""

    element_type = Float32
    static = True
    max_shape = (4,)


@dataclass
class Square(FreeRunComp):
    """y = x*x, element-wise over one Vec per firing."""

    cpp_kernel_name: ClassVar[str | None] = "square"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in = StreamIFSlave(name=f"{self.name}_x_in", sim=self.sim, bitwidth=32)
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

Two things make this the **synthesizable form**, where the [moving-average](./overview.md) intro was
not:

- **`run_iter` is one firing.** It reads one payload, computes, writes one — no hand-rolled `while`
  loop. The looping is the runtime's job.
- **The compute is a `@synthesizable` pure function.** `square` takes its input as an *argument* and
  returns the result using the type-preserving [array operators](../vectorization/) (`x * x`), not raw
  NumPy. A `@synthesizable` method may read its arguments, endpoints, reg-maps, and `HwParam` values —
  but **not** mutable `self.X` state (the extractor rejects it).

The array operators combine two `DataArray`s and preserve the element type. They do **not** accept a
scalar operand — `x * 0.5` and `0.5 * x` both raise `TypeError`; use `.val` for a raw NumPy escape
hatch, or wrap the scalar in a `DataArray`.

## What this example claims

It is **real, executed code**: [`tests/examples/test_toy.py`](../../../tests/examples/test_toy.py)
runs `Square` in the pysim and checks `y = x²`, so this page cannot silently drift from it.

It is **not** a generated kernel. No `FreeRunComp` is auto-extracted today, and `Square` is no
exception — `kernel_files_to_str(Square)` returns files, but **not** the ones described above:

- The **`square` body is not extracted.** `@synthesizable` marks a *hook boundary*: codegen emits a
  declaration plus a `// TODO: implement square` stub for a **hand-written** C++ impl (exactly the
  arrangement of the checked-in [`simp_fun_compute_impl.cpp`](../../../examples/regmap/simp_fun_compute_impl.cpp)).
  The `x * x` above is the **pysim golden**; it does not itself lower to C++.
- The generated top is **`ap_ctrl_hs`**, not the free-running `ap_ctrl_none` `hls::task` described
  above. The class declares `control_mode = FREE_RUNNING`, but codegen does not yet act on it.
The real free-running kernels (`MemRStream`/`MemWStream`) hand off *fixed hand-written* `hls::task`
bodies via `kernel_task()`; their `run_iter` is a pysim golden only. So the claim `Square` earns is
*"this is real code, it runs, and this page matches it"* — not *"this synthesizes"*. Both gaps are
pinned by a test, so they will fail loudly the day they close.

> A third gap used to live here: the hook namespace defaulted to the *kernel function's* name, which
> is ill-formed C++ (a namespace and a function cannot share a scope and a name). `Square` was the
> first component to take that default *with* a hook, which is how it was found. The default is now
> `<kernel>_impl`, so you no longer need to set `cpp_namespace` by hand — though you still can, and
> every existing kernel does.

## State comes later

`Square` is **stateless**: each firing depends only on its input. Components that must carry state
*across* firings — a moving average's `xprev`, a running accumulator — are a **work in progress**: the
generated form of cross-iteration state is not built yet (the extractor forbids reading mutable
`self.X` from `run_iter`). For now, keep such state in the simulation model and expect the synthesizable
story to firm up. This is why the intro's `MovingAvg` stays a sim-only example.

*How* a `FreeRunComp` lowers to an `hls::task` and is verified is the [realization flows](../flows/)
section.
