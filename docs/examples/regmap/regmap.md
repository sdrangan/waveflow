---
title: Understanding Vitis Register Maps
parent: Register mapped simple function
nav_order: 1
has_children: false
---

# Understanding Vitis Register Maps

This is the simplest accelerator in Waveflow: a kernel with **no data streams at
all**, controlled entirely through a register map over AXI-Lite. It exists to
introduce one idea in isolation — how a host CPU talks to an FPGA kernel through
named, addressable control registers — before later examples layer on streaming
and shared-memory data paths.

## The control plane: register maps and AXI-Lite

When a CPU drives an FPGA accelerator it needs to do a few small things: pass a
handful of scalar arguments, tell the kernel to start, check whether it has
finished, and read a scalar result back. None of this needs high bandwidth — it
needs *individually addressable*, low-latency access to a few values. That is
exactly what a **register map** over **AXI-Lite** provides.

**AXI-Lite** is the lightweight member of the AXI bus family. It is a
*memory-mapped* protocol: the host reads or writes one 32-bit word at a time at a
fixed byte *offset* within the slave's address range. There are no bursts and no
streaming — just simple, addressable register reads and writes. Because it is
cheap to implement and easy to reason about, AXI-Lite is the standard **control
interface** for FPGA kernels: every Vitis HLS kernel with scalar arguments
exposes them through an auto-generated AXI-Lite slave.

A **register map** is the layout of that slave — a small set of named scalar
fields, each at its own offset, that the host reads or writes individually. It is
the kernel's *control plane*.

> **This example is control-only, on purpose.** In a real accelerator, bulk data
> flows over higher-bandwidth interfaces — AXI-Stream (data that streams through)
> or AXI memory-mapped (data in shared DRAM). This `simp_fun` example has *only*
> AXI-Lite registers and no data bus at all, so you can see the control plane by
> itself. The later examples add the data interfaces:
> [stream_inband](../stream_inband/) (AXI-Stream) and
> [shared_mem](../shared_mem/) (AXI memory-mapped).

## The example: an affine function with a ReLU

The kernel computes one scalar function — an affine map followed by a clamp at
zero (a "ReLU"):

```python
y = max(0, a * x + b)
```

Three signed 32-bit inputs (`x`, `a`, `b`) and one signed 32-bit output (`y`).
That is the entire datapath. Everything interesting here is in *how the host gets
the inputs in, launches the kernel, and gets the result out* — i.e. the register
map.

## How Vitis generates the slave

When a Vitis HLS kernel marks its scalar arguments (and `return`) with
`#pragma HLS interface s_axilite`, the tool automatically builds an AXI-Lite slave
and assigns each argument a register offset. The generated kernel for this
example is:

```cpp
void simp_fun(
    ap_int<32>& x,
    ap_int<32>& a,
    ap_int<32>& b,
    ap_int<32>& y
) {
#pragma HLS INTERFACE s_axilite port=x      bundle=control
#pragma HLS INTERFACE s_axilite port=a      bundle=control
#pragma HLS INTERFACE s_axilite port=b      bundle=control
#pragma HLS INTERFACE s_axilite port=y      bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control
    y = simp_fun_impl::compute(x, a, b);   // max(0, a*x + b)
}
```

Each `s_axilite` port becomes a register in the `control` bundle (the AXI-Lite
slave). The `port=return` pragma is what adds the **control registers**
(`ap_start` / `ap_done`, below): it tells Vitis the kernel is launched and
monitored over that same slave.

In Waveflow you declare the matching register map in Python, and `VitisRegMap`
mirrors the layout Vitis documents for that slave:

```python
Int32 = IntField.specialize(bitwidth=32, signed=True)

self.regmap = VitisRegMap({
    "x": RegField(Int32, RegAccess.RW, description="Input operand"),
    "a": RegField(Int32, RegAccess.RW, description="Multiply coefficient"),
    "b": RegField(Int32, RegAccess.RW, description="Bias term"),
    "y": RegField(Int32, RegAccess.R,  description="relu(a*x + b)"),
})
```

Using `VitisRegMap` (rather than a plain `RegMap`) is what adds the Vitis control
block and places your fields where Vitis places its scalar arguments.

### Vitis-added control registers

Vitis prepends a fixed 16-byte control region to every `s_axilite`-controlled
kernel. The four control signals are **bits of a single word at `0x00`** — not
registers of their own:

| Offset | Register | Bit | Access | Role |
| ------ | -------- | --- | ------ | ---- |
| `0x00` | `ap_start` | 0 | RW / COH | Host writes `1` to launch the kernel. Clears on the `ap_ready` handshake. |
| `0x00` | `ap_done`  | 1 | R / COR  | Reads `1` once the kernel has finished and `y` is valid. Clears on read. |
| `0x00` | `ap_idle`  | 2 | R        | `1` while no invocation is in flight. |
| `0x00` | `ap_ready` | 3 | R / COR  | Kernel is ready for new inputs. Clears on read. |
| `0x00` | `auto_restart` | 7 | RW | Re-launch automatically on completion. |
| `0x00` | `interrupt` | 9 | R | Interrupt status. |
| `0x04` | `gier` | — | RW | Global Interrupt Enable Register. |
| `0x08` | `ier`  | — | RW | IP Interrupt Enable Register. |
| `0x0C` | `isr`  | — | R / TOW | IP Interrupt Status Register. |

Together `ap_start` / `ap_done` implement Vitis's **`ap_ctrl_hs`** ("handshake")
control protocol. The host uses `ap_done` to know when `y` is ready without
needing an interrupt line. (`COH` = clear on handshake, `COR` = clear on read,
`TOW` = toggle on write.)

### Application registers

These are the four fields you declare — the kernel's actual arguments and result.
Vitis places 32-bit scalar arguments from `0x10` on an **8-byte stride**: each one
gets a data word plus a control/reserved word beside it.

| Offset | Register | Schema | Access | Role |
| ------ | -------- | ------ | ------ | ---- |
| `0x10` | `x` | `Int32` | RW | Input operand — the value to apply the map to |
| `0x14` | —   | —       | —  | reserved (control word for `x`) |
| `0x18` | `a` | `Int32` | RW | Multiplicative coefficient |
| `0x1C` | —   | —       | —  | reserved |
| `0x20` | `b` | `Int32` | RW | Bias term |
| `0x24` | —   | —       | —  | reserved |
| `0x28` | `y` | `Int32` | R  | Result — `max(0, a*x + b)` |
| `0x2C` | —   | —       | —  | control word for `y` (`y_ap_vld`) |

The access mode encodes the host/kernel contract: `RW` (read-write) registers are
host *inputs* the kernel reads; `R` (read-only) registers are kernel *outputs*
the host reads back. The host never writes `y`.

> **What the model does and does not reproduce.** `VitisRegMap` mirrors the
> *layout* above, and models `ap_start`, `ap_done`, `ap_idle` and `ap_ready`.
> It does **not** model the clear-on-read (`COR`) semantics: the simulator's
> `ap_done` is cleared on the next `ap_start` rather than by the read itself, so
> a host may read it twice and see `1` both times. `gier`/`ier`/`isr` are plain
> storage — there is no interrupt line in the simulation — and `auto_restart` /
> `interrupt` are not modelled. See the
> [Register Maps guide](../../guide/interface/primitive/regmap.md) for the full reference.

## The execution model

Putting it together, one run of the kernel is a fixed sequence of AXI-Lite
transactions:

1. **Write the inputs.** Host writes `x`, `a`, `b` to their offsets (one AXI-Lite
   write each).
2. **Launch.** Host writes `1` to bit 0 of the control word (`0x00`).
3. **Kernel runs.** It reads `x`/`a`/`b`, computes `max(0, a*x + b)`, writes `y`,
   and sets `ap_done`.
4. **Poll for completion.** Host reads the control word (`0x00`) repeatedly until
   bit 1 (`ap_done`) reads `1`. (On real hardware you would usually wait on an
   interrupt instead; polling is the simple, pedagogical path.)
5. **Read the result.** Host reads `y` (`0x28`).

This start-then-poll handshake is the essence of the AXI-Lite control model.

### What the Python model abstracts away

In the simulation you do not hand-assemble those bus transactions. `BoundRegMap`
binds the register map to a host endpoint and exposes the same sequence as
ordinary typed calls — each one lowers to the AXI-Lite read or write at the right
offset:

```python
rm = self.regmap.bind_master(self.master, base_addr=self.base_addr)

yield from rm.set("x", case.x)      # AXI-Lite write -> 0x10
yield from rm.set("a", case.a)      #               -> 0x18
yield from rm.set("b", case.b)      #               -> 0x20
yield from rm.start()               # write 1 to bit 0 of 0x00 (ap_start)
ap_done = yield from rm.poll_end(   # read 0x00, test bit 1, until it reads 1
    interval=..., max_polls=...,
)
y = yield from rm.get("y")          # AXI-Lite read <- 0x28
```

Bit-packed fields cost no extra bus traffic: `rm.start()` is a single word write
composing `ap_start` into bit 0, and each `poll_end` read is a single word read
that extracts bit 1. You address fields by *name*, so the packing stays an
implementation detail of the layout.

### How the offsets stay honest

It is worth being precise about what keeps the Python model and the hardware
agreeing, because it is easy to overclaim here.

The offsets above are **not** shared with the kernel. Codegen emits no addresses
at all — the generated C++ just declares `s_axilite` pragmas, and **Vitis** picks
the offsets when it synthesises the slave. The Python offsets are used only
*inside* the simulation, where `BoundRegMap` writes `base + offset_of(name)` and
the slave decodes the same table. Both ends read one table, so the sim is
self-consistent by construction, and re-laying-out the map cannot change a single
byte of generated C++.

What that buys is a model that **mirrors** Vitis's documented layout — so the
addresses you read here are the addresses a real host driver would use. What it
does *not* buy is enforcement: nothing currently checks that the mirror stays in
sync, so a future Vitis release could move an offset and the model would drift
silently. The authoritative artifact is `control.h`, which Vitis writes next to
the generated RTL:

```
waveflow_simp_fun_proj/solution1/.autopilot/db/coregen/control.h
```

Teaching a build step to parse that file and diff it against `VitisRegMap` would
turn the mirror into a checked contract. That conformance test does not exist yet
— it is follow-on work.

---

The rest of this example follows the same accelerator through the full Waveflow
flow: [the Python model](python.md), [system simulation](pysim.md),
[sequential execution](seqtb.md), [generating the Vitis kernel](codegen.md),
and [validating the RTL](rtlsim.md).
