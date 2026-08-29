---
title: A module realized as Verilog
parent: Module Code Generation
nav_order: 5.5
audience: hls
applies_to: [HwModule]
api: [rtl_module, RtlModule, resolve_rtl_module, GenRtlStep, T2pBram, BramIFSlave]
summary: "The third realization hook. A module declares pre-written Verilog with rtl_module() and it is instantiated beside the generated kernel — never generated from Python. Covers why a memory shared between two tasks cannot live inside a Vitis kernel, the endpoint → C++ parameter → Vitis port-name chain, the rule that the read latency has exactly one source (the .v publishes it, the pragma is derived from it), the declared resource footprint, and the conformance gap nothing static can close."
---

# A module realized as Verilog

Most of a design is Python that Waveflow lowers. Some of it is not, and cannot be: a memory shared
by two concurrent accessors has no expression inside a Vitis kernel at all. `rtl_module()` is how a
module says *"my realization is this Verilog file, which already exists and has already been
simulated."*

## The third hook

Two realization hooks already existed. This is the third, and all three have the same shape — they
**declare** a pre-written artifact, and none of them extracts or generates one:

| hook | declares | lands as |
|---|---|---|
| [`kernel_task()`](freerunning_override.md) | "my pre-written `hls::task` body is *X*" | a task **inside** the generated top |
| `rtl_module()` | "my pre-written **Verilog** is *Z*" | a module **beside** the generated top |
| [`bfm_model()`](../custom_hooks/bfm_model.md) | "my pre-written C++ cycle model is *Y*" | an `XsiSimObj` **beside** the design |

The target is `rtl_module`, not `verilog`: the target vocabulary names *which realization*, not which
language (`composite_kernel`, `sequential_xsi_tb`), and the day this hook holds a vendor IP core
"verilog" would be the wrong word.

Like `xsi_bfm_model`, it is **not** a `potential_targets` entry, because which realization applies is
a property of the **build**, not of the class: the same memory is hand-written RTL inside a
synthesized design and a `FlatMemory` model inside an XSI testbench, with nothing about the memory
changed. So `check(mod, "rtl_module")` is answerable for any `HwModule`.

## Why a module ever needs this

Vitis HLS has no notion of memory shared between processes. Two structures look like they would work
and do not — both measured, both in [`plans/rtl_module.md`](../../../plans/rtl_module.md):

```
INFO:  [HLS 200-741] Implementing PIPO rx_buf_r_RAM_T2P_BRAM_1R1W using a single memory
ERROR: [HLS 200-976] Argument 'buf_r' failed dataflow checking:
                     Cannot read as well as write over function parameter.
```

The first is the dangerous one: a local array shared between two tasks **csynths**, and silently
becomes a synchronizing ping-pong channel whose handshake *stalls the writer*. The second is the
same objection stated out loud.

This is not an oversight. `DATAFLOW`'s promise is that the parallel result equals the sequential C
result, and a shared buffer with independent pointers has no sequential-C meaning — whether `buf[rd]`
sees the old value or the new one depends on *when*, which C does not express. The real division is
**who owns the correctness argument**: for a channel the tool owns it and enforces it with
handshakes; for `m_axi` and for this hook the designer owns it, and the tool does not interfere.

So the memory lives outside the kernel, in Verilog, and the kernel reaches it through `bram` ports.

## Declaring one

`T2pBram` ([`waveflow/hw/bram.py`](../../../waveflow/hw/bram.py)) is the first and, today, the only
consumer — a true-dual-port memory with one port written and one port read:

```python
class T2pBram(HwModule):
    dwidth: HwParam[int] = 16
    depth:  HwParam[int] = 1024

    def rtl_module(self):
        return RtlModule(
            module="bram_t2p",                       # the module name a wrapper instantiates
            files=("bram_t2p.v",),                   # the pre-written source
            ports={                                  # endpoint attr -> the file's port names
                "wr_port": {"addr": "a_addr", "en": "a_en", "din": "a_din",
                            "dout": "a_dout", "we": "a_we"},
                "rd_port": {"addr": "b_addr", "en": "b_en", "din": "b_din",
                            "dout": "b_dout", "we": "b_we"},
            },
            params=(("DW", 16), ("AW", 10)),         # overrides for the *instantiation*
            clock="clk")
```

Parameterizing is not generating. The `.v` is copied byte for byte by `GenRtlStep`; `DW`/`AW` ride on
the instantiation, which is what a Verilog parameter is for.

`check(T2pBram, "rtl_module")` then answers five things, and refuses by name rather than by
`KeyError`:

1. the hook is **declared** (by identity against the base method, never `hasattr`);
2. every endpoint's **kind has a Verilog port mapping** — today that table has exactly one row,
   `BramIFSlave`, and it grows one *verified* kind at a time;
3. every endpoint is in the port map with **every role** of its kind (an unmapped role is a wire
   nobody drives, which at RTL is a hang with no diagnostic);
4. the named **file exists** and declares a module of that name with those ports;
5. a kind that needs a read latency **gets one from the file** (below).

## The port-name chain is the contract

A wrapper joining the kernel to the memory can be written from Python only because both sides' port
names are known at generate time. The chain is:

```
Waveflow endpoint  ->  C++ parameter name  ->  Vitis RTL port names
   wr_port                  buf_w               buf_w_Addr_A, buf_w_EN_A, buf_w_Din_A,
                                                buf_w_Dout_A, buf_w_WEN_A, buf_w_Clk_A,
                                                buf_w_Rst_A   ... and the whole B pair
```

One C++ array parameter becomes **fourteen** RTL ports: Vitis emits an A/B pair of seven signals
whether or not the kernel uses both halves. This is a new row in the table
[`TopSpec.trace_manifest`](../../../waveflow/build/composite_gen.py) already holds for AXI-Stream and
`m_axi`, and it exists for the same reason: *Vitis picks only the `_U0` instance suffix; codegen owns every other name.* An AXIS port
keeps its own name, an `m_axi` port takes its **bundle's**, and a `bram` port takes its own name plus
a signal-and-half suffix.

`bram_port_signals()` derives them, and the gate is the witness rather than a restatement of the
rule: `plans/witness/t2p_bram/rx_top.v` is a hand-written wrapper that elaborated and ran, and its
kernel instantiation names all 28 nets of a two-interface design. The test compares against that
file, so it needs no toolchain.

## Latency comes from one declaration

The memory's read latency appears in two places that must agree: the kernel's
`#pragma HLS INTERFACE mode=bram ... latency=N` and the Verilog itself. **A mismatch does not fail.**
It shifts every read by one cycle and the design keeps running, which is why the witness's testbench
checks a *ramp* rather than a constant — a constant would pass.

If the two can be authored independently, they will eventually disagree. So they cannot be:

- the `.v` **publishes** the number — `localparam READ_LATENCY = 1;` — and `localparam` rather than
  `parameter` is the statement that it is a property of the implementation, not a knob;
- Python **reads** it (`rtl_read_latency`) and emits the pragma from it;
- there is **no latency field** anywhere in Python to set. `T2pBram.read_latency` is a property with
  no setter, so the only way to change the pragma is to change the Verilog it describes.

Two more traps live next door, both measured:

- **The C++ parameter must be a sized array**, `ap_uint<W> buf_w[DEPTH]`. `mode=bram` on an *unsized*
  pointer silently produces an `ap_vld` scalar port — no warning, no error, and the design elaborates
  against a memory that is not there. A generated pragma always needs a check that it took effect.
- **The design invariant lives in the Verilog.** `bram_t2p.v` `$error`s when the read port touches the
  address the write port is writing that cycle (for a circular buffer: *rd trails wr*). Nothing else
  would check it — if it fails, the data is whatever the BRAM's read-during-write mode happens to be
  and no tool says a word. A hand-written memory is *more* verifiable than an emulated one.

## The footprint is declared here

`T2pBram` states its own BRAM cost, from depth × width. That is not a stopgap: a memory's footprint
is **structural** — a 1024×16 true-dual-port buffer is one RAMB18 by geometry, and no tool run is
needed to know it. Only the rounding at the edges is tool-specific, so the declared number should
eventually be gated against a real synthesis rather than trusted forever.

It is also the only number available. `csynth` of the kernel reports **no BRAM at all**, because the
memory is outside it — a scope you cannot count is half the reason to want the wrapper. The general
rule: *structural blocks (memories, FIFOs) can declare their footprint; logic blocks cannot and need a
run.*

## The conformance obligation

`check(mod, "rtl_module") == (True, None)` means **resolvable**, not **correct**.

Like `bfm_model()`, this target is *resolved* rather than *derived*: `composite_kernel` runs the real
extractor and so answers with rules nobody restated, while this one performs a hook lookup, a file
read and a port-coverage check. It can say *"you named a module, the file exists, and its ports line
up with your endpoints."* It can never say *"your Python behaviour is realizable as this Verilog."*

That is the **third instance of one gap**: nothing checks Python against C++ for a `kernel_task()`
body, nothing checks Python against C++ for a `bfm_model()`, and nothing checks Python against Verilog
here. The answer is the same one, stated once and referenced three times — a
[byte-identical vector gate](../custom_hooks/bfm_model.md#conformance): run the same stimulus through
both and compare outputs, because the two artifacts are the design's two halves and only their
outputs can testify that they agree.

## Where the declared module ends up

This page is the *declaration*. Two more pieces turn it into a design, and both exist:

- **The wiring** — a [`BramIF`](../interface/bram.md) binds an accessor's port to a memory port, and
  `add_rtl_if` (deliberately **not** `add_if`) is what keeps the accessor's port a boundary port of
  the kernel.
- **The wrapper** — [`wrapper_gen`](../../../waveflow/build/wrapper_gen.py) emits a module
  instantiating the kernel plus its memories and joining them, and that module is what a simulator
  elaborates. See [Free-running composite](./freerunning_composite.md#when-the-composite-is-not-the-whole-design-the-wrapper).

[`examples/bram_access`](../../examples/bram_access/) is the worked design, gated at RTL
against the witness's own values.

`trace_manifest` derives net names in *the top's own scope*, so with a wrapper as the elaborated top
the **kernel's** internals sit one level deeper and are still out of reach. The first consumer to
trace a wrapped design — `examples/bram_access`, which detects read-during-write collisions in the
waveform because [the memory's `$error` cannot be heard](../interface/bram.md#the-error-fires-and-in-this-flow-nothing-can-hear-it)
— turned out **not** to need the prefix: what it reads are the wrapper's *own* wires, the ones
joining the kernel's `bram` ports to the memory, and those are exactly what a level-1 `$dumpvars` of
the wrapper captures. They are named by
[`bram_hazard_manifest`](../../../waveflow/build/wrapper_gen.py), the wrapper's counterpart to
`trace_manifest`. Reaching *inside* the kernel from a wrapped top is still unbuilt.

## See also

- [Overriding the generated task](freerunning_override.md) — the hook for a module realized *inside*
  the top.
- [Writing a BFM model](../custom_hooks/bfm_model.md) — the hook for a module realized *outside* the
  design, and the conformance gate this page shares.
- [Memory](../memory/) — the other storage categories, and which of them `csynth` counts.
