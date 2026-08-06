---
title: The module
parent: Vector multiply resource modeling
nav_order: 1
audience: python
applies_to: [FreeRunMod]
summary: "VecMult as a standalone free-running module: two stream ports, an in-band command carrying a transaction id and a runtime length, a response that echoes the id, two parameters whose difference is load-bearing — vlen is the compile-time bound the area is priced against, n is the runtime length that costs nothing — and one shared golden() so the twin check has something real to disagree with."
---

# The module

`VecMult` is a **standalone free-running module** — a single `FreeRunMod` with no sub-components. It
is never started and never returns; it consumes a job whenever one arrives and is paced by
back-pressure alone.

```python
@dataclass
class VecMult(FreeRunMod):
    """z = x * y, element-wise, over [cmd | x | y] arriving on one stream."""

    cpp_kernel_name: ClassVar[str | None] = "vec_mult"

    dwid: HwParam[int] = 64        # stream word width in bits
    vlen: HwParam[int] = 4096      # compile-time bound on the buffer
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
```

## The ports

Two, and the fact that there is only **one input** is the design's most consequential choice:

```python
self.s_in  = StreamIFSlave(name=f"{self.name}_s_in",  sim=self.sim, bitwidth=w, has_tlast=False)
self.z_out = StreamIFMaster(name=f"{self.name}_z_out", sim=self.sim, bitwidth=w, has_tlast=False)
for ep in (self.s_in, self.z_out):
    self.add_endpoint(ep)
```

`has_tlast=False` gives plain `hls::stream<ap_uint<W>>` ports. The command carries the length, so
there is no packet boundary left to signal.

## The protocol

```text
s_in:   [ cmd(tx_id, n) | x_0 .. x_{n-1} | y_0 .. y_{n-1} ]
z_out:  [ z_0 .. z_{n-1} | resp(tx_id) ]
```

The command and response are ordinary [`DataList`](../../guide/schema/python/fields.md) schemas, so
their word packing is generated rather than hand-rolled on either side:

```python
class VecCmd(DataList):
    include_filename: ClassVar[str | None] = "vec_cmd.h"
    elements = {
        "tx_id": {"schema": Word32, "description": "echoed unmodified on the response"},
        "n":     {"schema": Word32, "description": "sample count for this job (<= vlen)"},
    }


class VecResp(DataList):
    include_filename: ClassVar[str | None] = "vec_resp.h"
    elements = {
        "tx_id": {"schema": Word32, "description": "the command's tx_id, echoed unmodified"},
    }
```

### The sample type, and where the generated headers land

Both schemas above are built from two specialized `IntField`s, declared once at module level:

```python
SAMP_W = 16
INCLUDE_DIR = "include"

Samp   = IntField.specialize(bitwidth=SAMP_W, signed=True,  include_dir=INCLUDE_DIR)
Word32 = IntField.specialize(bitwidth=32,     signed=False, include_dir=INCLUDE_DIR)
```

`include_dir` is not cosmetic. Specializing `Samp` is what generates `int16_array_utils.h` — the
serializer the hand-written body calls for every word↔element conversion — and it has to land where
that body's `#include` will find it. Left at the default it lands at the example root instead, beside
the `.tcl` rather than beside the other headers.

### Why there is a response at all

A kernel that emits only `z` says nothing about *which* job finished. A caller with several requests
in flight cannot correlate a result with its command, and cannot tell a slow job from a lost one.
Echoing the transaction id makes completion **observable** rather than inferred from word counts.

{: .note }
> **Why a plain struct rather than a framed descriptor.** `fir_block` sends its descriptor on a
> `framed_word` channel, which carries an extra framing bit and lets intermediate blocks relay a
> payload they do not parse. That buys real robustness — but nothing relays `VecMult`'s stream, so it
> would add a framing bit per word and a second concept to an example whose subject is resources.
> Framing earns its keep in a pipeline; here it would only be ceremony.

## Two lengths, deliberately

| | | costs |
|---|---|---|
| `vlen` | `HwParam` — the **compile-time** bound on the buffer | **sets the BRAM** |
| `n` | a field in the command — the **runtime** length | nothing in area; changes the *shape* of the logic |

A design fed only short vectors still pays for the bound it was built with. Keeping the two separate
is what lets the resource model key on `vlen` alone, and it is asserted directly:

```python
def test_runtime_length_does_not_change_the_hardware():
    a = elaborate(VecMult, {"dwid": 64, "vlen": 1024}, name="a")
    b = elaborate(VecMult, {"dwid": 64, "vlen": 1024}, name="b")
    assert structure_signature(a) == structure_signature(b)
```

The knob everything else follows from is the **lane count**:

```text
LW = dwid // samp_w        # samples carried per stream word — 64/16 = 4
```

`samp_w = 16` is a module-level constant rather than a parameter, so the example has exactly two
knobs. That is deliberate: varying the sample width would change which *regime* the DSP rule is in
(a ≤8-bit multiply packs two per DSP, a >18-bit one splits across two), which is a second lesson and
is left to [`fir_block`](../firblock/).

## The body is hand-written

`VecMult` declares its kernel through `kernel_task()` rather than letting the extractor derive one:

```python
def kernel_task(self) -> KernelTask:
    return KernelTask("vec_mult_task", "vec_mult_task.h",
                      ("s_in", "z_out"),
                      template_args=(int(self.dwid), int(self.vlen)))
```

A two-phase load/stream body over a partitioned buffer is outside the
[extractor's vocabulary](../../guide/comp_codegen/extractor.md), so nothing could derive the function
name or its parameter order. `run_iter` stays as the **pysim golden**:

```python
def run_iter(self) -> ProcessGen[None]:
    cmd = yield from self.s_in.get(VecCmd)
    n = int(cmd.n)
    x = yield from self.s_in.get(Samp, count=n)
    y = yield from self.s_in.get(Samp, count=n)
    z = golden(np.asarray(x.val), np.asarray(y.val))
    yield from self.z_out.write(DataArray.specialize(Samp, max_shape=(n,), static=True)(z))
    yield from self.z_out.write(VecResp(tx_id=int(cmd.tx_id)))
```

The output array is specialized at **`n`**, the runtime length — not at `vlen`. In fact `vlen` never
appears in `run_iter` at all: the Python model has no buffer to size, because nothing forces it to
hold `x` while `y` arrives. That absence is worth noticing, since the buffer is the entire subject of
this example. **The BRAM is a fact about the implementation, not about the behaviour** — which is
exactly why a resource model has to be keyed on declared structure rather than on what the module
computes.

### The golden

`run_iter` does not compute the product itself. It calls a module-level function:

```python
def golden(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Element-wise product, wrapping in the sample's own width."""
    prod = np.asarray(x, dtype=np.int64) * np.asarray(y, dtype=np.int64)
    return ((prod + (1 << (SAMP_W - 1))) % (1 << SAMP_W) - (1 << (SAMP_W - 1))).astype(np.int64)
```

**One definition, called from two places** — `run_iter` above, and the test that checks the C++.
That is the point of factoring it out. If `run_iter` computed the product inline and the test
re-derived the expected answer, the test would be asserting that the model agrees with *itself*, and
would pass just as happily if both were wrong. Sharing one function leaves the C++ as the only thing
that can disagree.

Which it can, in one specific way: the arithmetic **wraps** in 16 bits rather than saturating, so
`vec_mult_task.h` has to truncate identically — see
[the multiply](./kernel.md#the-multiply). Two bodies for one behaviour is a liability unless
something checks them against each other, which is what the [testbench](./testbench.md) exists for.

`template_args` bakes both knobs, so the generated top instantiates
`vec_mult_task<64, 4096>` and the RTL entity is named `vec_mult_task_64_4096_s`. That name is what
[resource attribution](../../guide/resource/composite.md) matches on, derived rather than tabulated.

## Next

- [The kernel](./kernel.md) — the hand-written task body, and why it buffers.
