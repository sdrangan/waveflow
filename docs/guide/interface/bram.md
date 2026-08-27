---
title: BRAM — memory between modules
parent: Interfaces
nav_order: 3.5
audience: python
applies_to: [BramIF, BramIFMaster, BramIFSlave, T2pBram]
summary: "BramIF connects a kernel task to an on-chip memory that lives OUTSIDE the kernel, as hand-written Verilog joined by a generated wrapper. Explains why a memory shared between two tasks cannot live inside a Vitis kernel — with the PIPO and dataflow-check evidence — and why a BramIF is registered with add_rtl_if rather than add_if, which is what keeps the accessor's port a boundary port."
---

# BRAM — memory between modules

A `BramIF` connects one kernel task to one port of an on-chip memory. It is the only interface in
this section whose far end is **not** inside the generated kernel: the memory is hand-written Verilog
instantiated beside it, and a generated wrapper joins the two.

That is not a design preference. It is the only structure Vitis leaves available.

## Why a shared memory cannot live inside a kernel

Two `hls::task` bodies sharing a buffer is the natural way to write a capture buffer, a scoreboard, a
reorder queue. Two constructs look like they would express it. Both were measured — Vitis HLS 2025.1,
`xczu48dr-ffvg1517-2-e`; the full experiment log is in
[`plans/rtl_module.md`](../../../plans/rtl_module.md).

### A local array shared between two tasks — **compiles, and means something else**

```
INFO: [HLS 200-741] Implementing PIPO rx_buf_r_RAM_T2P_BRAM_1R1W using a single memory
```

```verilog
assign read_task_U0_ap_start     = buf_r_t_empty_n;   // reader gated on release
assign write_task_U0_ap_continue = buf_r_i_full_n;    // WRITER STALLS when full
```

This is the dangerous one, and it is why "it csynthed" is not evidence of anything. The C++ reads
like a circular buffer; the RTL is a synchronized ping-pong. The writer **stalls** when the consumer
has not released a buffer — and a stage facing a converter, an ADC, or anything else that cannot wait
may never stall.

### One array written by one task and read by **another** — **hard error**

```
ERROR: [HLS 200-976] Argument 'buf_r' failed dataflow checking:
                     Cannot read as well as write over function parameter.
```

Read that message for what it says. It is the **dataflow checker** objecting to an argument crossing
two task bodies — one process writing what another reads. It is *not* a prohibition on a
bidirectional `mode=bram` port: one task reading and writing one port is accepted, and measured to
be. See [`access="readwrite"`](#accessreadwrite-and-the-storage_type-that-follows) below.

### Why the tool is not being obtuse

The objection is **dataflow channel** semantics, not arbitration. `DATAFLOW`'s promise is that the
parallel result equals the sequential C result — and a shared buffer with independent pointers has no
sequential-C meaning at all. Whether `buf[rd]` sees the old value or the new one depends on *when*,
which C does not express.

So the real division is **who owns the correctness argument**. For a channel, the tool owns it and
enforces it with handshakes. For `m_axi`, and for this interface, the *designer* owns it and the tool
does not interfere. A local array is inside the tool's analysis scope by construction, so it always
gets channel treatment. There is no third option *inside* a kernel — which is what puts the memory
outside one.

## The shape

```python
W16 = word_element(16)                                              # a memory of raw 16-bit words
self.buf_w = BramIFMaster(element_type=W16, nelem=1024, access="write")   # the accessor task
self.mem   = T2pBram(element_type=W16, nelem=1024)                        # the memory module
...
self.add_rtl_mod(self.mem)          # realized as hand-written Verilog beside the kernel
w_if = BramIF(name="bufw_if", sim=self.sim)
w_if.bind(ep_name="master", endpoint=self.wr.buf_w)
w_if.bind(ep_name="slave",  endpoint=self.mem.wr_port)
self.add_rtl_if(w_if)               # a WRAPPER WIRE, not an internal channel
```

* **`BramIFMaster`** is the accessor's end — a kernel task's window onto storage it does not own. In
  C++ it is a *sized array parameter* (`ap_uint<16> buf_w[1024]`) carrying
  `#pragma HLS INTERFACE mode=bram`; in RTL it is **fourteen** ports, an A/B pair of seven signals.
* **`BramIFSlave`** is the memory's end. One endpoint is one *port* of the memory; a `T2pBram` has
  two.
* **`element_type` + `nelem`** is the whole declaration, and everything in bits follows from it:
  `bitwidth` is a property (`element_type().get_bitwidth()`), the wrapper's byte-address shift is
  `log2(bitwidth/8)`, and the pysim memory is `np.zeros(nelem, dtype=<the element's dtype>)` — so a
  `Float32` memory holds float32s rather than a packing of them. A port in RTL is `addr` + `din` /
  `dout` + `we` / `en`: uniform width, address-indexed. That *is* an array, so an element plus a
  count is exactly what the port can express, and one address holds one element — `nelem` is also
  the memory's depth. Use `word_element(N)` when the contents really are raw words.
* **An element that is not a power-of-two byte count is refused where it is declared**, not at
  wrapper generation. Vitis scales a `mode=bram` address in *bytes* by a **shift**, so a 14-bit
  element (the RFdc dense-14 sample) has no expressible scaling — pack it into a word first. Both
  ends check it in `__post_init__`, citing the emitter (`_bram_addr_shift`) that owns the rule.
* **`access`** (`"read"` / `"write"` / `"readwrite"`) is declared on both ends, and the two must be
  **identical** — they are two statements of one fact, not a permission and a use. It decides the
  port's `storage_type`; see below.
* **Vector access is Case 2** ([the three access cases](overview.md#the-three-access-cases)):
  `read_pipelined(element_type, count, addr) -> (data, tstart)` and
  `write_pipelined(data, addr, t_start)`. The model has no free parameters — throughput is II=1, one
  element per cycle per port; the read's fill is the memory's published `READ_LATENCY`, reached
  through the bound `BramIF` from the Verilog `localparam`, paid once per transfer rather than per
  element; and `t_start` is the same anchoring every other endpoint uses. `mem_read` / `mem_write`
  stay for scalar access.
* **The bind also checks the element**, not only the extent. Two 32-bit ports that disagree about
  whether those bits are a float or a word line up at every address and return a correctly-shaped
  wrong number forever — the quieter half of the aliasing class the size check already catches.

## `access="readwrite"`, and the `storage_type` that follows {#accessreadwrite-and-the-storage_type-that-follows}

The memory was never the restriction. `bram_t2p.v` is **symmetric** — both ports carry `din`, `we`
*and* `dout`, which is what *true* dual-port means, as against *simple* dual-port (one write, one
read):

```verilog
if (a_en) begin  if (|a_we) mem[a_addr] <= a_din;  a_dout <= mem[a_addr];  end
if (b_en) begin  if (|b_we) mem[b_addr] <= b_din;  b_dout <= mem[b_addr];  end
```

So a port that reads *and* writes is a real option, and `access="readwrite"` is how it is declared.
It is not free, and the price is not where a first guess puts it:

> **The wrapper wires ONE physical memory port per declared `bram` port, so the pragma must forbid
> Vitis from using two.**

That invariant used to hold **by accident of direction** — a unidirectional port needs one access
per cycle, so Vitis only ever used the `_A` half. A read-write port breaks the accident, and it
breaks it silently. Measured (Vitis HLS 2025.1, one task, one port, an in-place `buf[i] = buf[i]*3+1`
loop):

| `storage_type` | compute II | write II | the `_B` half of the pair | wrapper-safe |
|---|---|---|---|---|
| `ram_1wnr` | 1 | 1 | **DRIVEN** — live `Addr_B`, `EN_B`, `WEN_B` | **NO** |
| `ram_1p` | **2** | 1 | not declared at all | yes |

Under `ram_1wnr` Vitis reaches II=1 by **reading on port B while writing on port A** — and the
wrapper wired only the A halves, so those reads reach a dangling port. X or stale data, a clean
`csynth`, nothing visible until RTL.

So `storage_type` is **derived from `access`**, never a constant in the emitter:

```
access="read" | "write"   ->  storage_type=ram_1wnr    (1 physical port, II=1)
access="readwrite"        ->  storage_type=ram_1p      (pins Vitis to 1 port, II=2 in place)
```

`ram_1p` is *structurally* safe rather than safe-by-convention: it does not declare the `_B` half at
all, so no wrapper can mis-wire it. `tests/build/test_bram_readwrite_vitis.py` asserts exactly that
against the emitted Verilog, with a unidirectional port synthesized alongside as the control — so
"no `_B` signals" is evidence of `ram_1p` rather than of an argument that was optimized away.

**The lesson is the mechanism, not the number.** "In-place is II=2" is false in general. "The wrapper
gives you one physical port, so the pragma pins Vitis to one, so read-modify-write costs two cycles
per element" is true and explains itself.

One restriction remains, and it is a *tooling* one rather than a hardware one: on a `T2pBram` only
**port A** may write. The `$error` below is written one-sided — *A writes while B touches the same
address* — so a writing port B would be invisible to the design's only real check. `T2pBram` refuses
it at construction rather than letting it go wrong.

## `add_rtl_if`, not `add_if` — and that is the whole mechanism

A [`StreamIF`](./stream.md) registered with `add_if` is an **internal channel**: it lowers to an
`hls::stream` inside the generated top, and both its endpoints leave the top's boundary. A `BramIF`
is not that. One end is inside the kernel and the other is outside it, so the accessor's end **must
stay a boundary port** and the join must happen one level up.

Because `add_rtl_if` is a different registry, `derive_boundary` — which reads the `add_if` one —
never sees a `BramIF` at all. The accessor's port is therefore an unbound child endpoint, and the
existing rule (*a child endpoint not bound to an internal interface **is** a boundary port*) makes it
a port of the kernel with **no change to that walk**. Putting a `BramIF` in `add_if` would instead
make the kernel's memory ports vanish into a FIFO that does not exist.

The memory itself is registered with `add_rtl_mod` for the mirror-image reason: it is not a task, so
no walk that emits `hls::task`s should ever meet it.

## What the two backends model

| | pysim | RTL |
|---|---|---|
| storage | a numpy array on the memory module | the hand-written `.v` |
| access | **untimed** — a plain method call | one cycle, from the memory's published `READ_LATENCY` |
| ordering | whatever the graph does | the same, and the memory `$error`s if the reader touches the address being written — **but see below: in this flow nothing can hear it** |

The access is untimed in pysim on purpose: a BRAM answer is deterministic, unarbitrated and
one cycle, so a discrete-event model of it would add a timestep and no fidelity. Contrast
[AXI-MM](./aximm.md), where the bus, the arbitration and the burst *are* the point of having a model.

**The correctness argument is yours.** `bram_t2p.v` `$error`s when port B reads the address port A is
writing that cycle — for a circular buffer, *rd trails wr*. Nothing else would check it: if it fails,
the data is whatever the BRAM's read-during-write mode happens to be, and no tool says a word.

## The `$error` fires, and in this flow nothing can hear it

This page used to end the section above with *"a hand-written memory is more verifiable than an
emulated one"*. The assertion is real and it does fire — but **you will not see it**, and a page that
implies otherwise is promising protection that does not exist.

Measured on Vivado 2025.1 (`xelab -dll` plus the C++ loader, which is the XSI flow this repo runs):
RTL text output is **discarded**. `$display` from an `always` block reaches neither stdout nor a
file, an `initial $display` at time zero does not either, and a non-null
`s_xsi_setup_info::logFileName` produces no log — although it does change the kernel's invocation
from `-nolog` to `-log <name>`, visible in `xsim.dir/<top>/xsimkernel.log`. Only an `$fwrite` to a
file the Verilog opens itself works, which is what proves the RTL really is executing the code that
would have printed.

The cost was concrete: five shipped XSI gates asserted `"read-during-write collision" not in out`,
a string that could never appear, and each read as positive evidence. All five have been removed.

**So check the condition, not the message.** A traced run (`run.bat <top> <tb> trace`) dumps
`<top>_trace.vcd`, and the memory's address, enable and write-enable wires are declared in the
*wrapper's* own scope — exactly what a level-1 `$dumpvars` captures:

```python
from waveflow.build.wrapper_gen import bram_hazard_manifest
from waveflow.utils.bram_trace import find_read_during_write

hazards = find_read_during_write("bram_simple_top_trace.vcd", bram_hazard_manifest(comp, spec))
assert not hazards            # ...but see the next paragraph
```

`bram_hazard_manifest` names which net carries each term rather than matching by substring, for the
reason [the trace manifest](../comp_codegen/rtl_module.md) exists at all: codegen chose
those names, so binding is exact and a name that has moved fails loudly.

**An empty scan is not a passing gate on its own.** No collisions is what a correct design looks
like, and *also* what a renamed net, a dump that never ran, or a scan bound to the wrong scope look
like. Pair it with a scenario that deliberately collides and assert that one is *not* empty —
`tests/examples/test_bram_simple_xsi.py` does exactly this, and
`examples/bram_simple`'s `collision_scenario()` is the deliberate half.

**Address overlap alone will not produce a collision.** Two `II=1` sweeps over the same range are
parallel lines in (cycle, address): they never meet unless they happen to start in the same cycle.
Making them meet needs a relative phase that *moves* — which is why `collision_scenario()` gives the
writer and the reader command lengths that differ by one word.

*The durable fix is neither a print nor a trace scan but a sticky `collision` output on the memory,
carried through the wrapper and readable in both backends by construction. That is a `BramIF`
interface change and is [tracked in `plans/rtl_module.md`](../../../plans/rtl_module.md), not done.*

## The addressing convention {#the-addressing-convention}

This binds anyone who uses a `BramIF` at all, and until 2026-08-24 one half of it was wrong in every
BRAM design in the tree.

**Vitis addresses a `mode=bram` port in bytes.** The generated task RTL literally contains

```verilog
assign buf_r_Addr_A_local = buf_r_Addr_A_orig << 32'd3;   // a 64-bit array
```

`<< 1` for a 16-bit array, `<< 2` for 32, `<< 3` for 64 — one shift per `log2(bytes per word)`. A
word-addressed memory like `T2pBram`, which indexes `mem[addr[AW-1:0]]`, needs that undone, and
**the wrapper is where the two conventions meet**:

```verilog
bram_t2p #(.DW(64), .AW(10)) mem (
    .a_addr(buf_w_addr_a >> 3),
    .a_we(|buf_w_we_a),
    ...
```

**The WEN is a byte-enable vector, not an enable.** Vitis drives one bit per *byte* of the word — 8
bits at 64, 2 at 16 — and the memory takes a single write enable, so the wrapper reduces it with `|`.
Before the fix that wire was hard-coded two bits wide, which is correct only for a 16-bit word; at 64
the kernel's 8-bit WEN was being truncated into it. `xelab` reported a bit-length warning and nothing
read it.

The shift is derived once, in
[`_bram_addr_shift`](https://github.com/sdrangan/waveflow/tree/main/waveflow/build/wrapper_gen.py),
and a width whose scaling is not a shift is **refused rather than guessed** — because the failure mode
of guessing is an address wrong by a factor, aliasing high words onto low ones with no tool saying
anything.

### Why nobody noticed

**The scaling is consistent.** A design that writes and reads through the same scaled address
round-trips perfectly — right up to the point where its memory wraps. Only `depth / (W/8)` distinct
words are reachable; everything above that aliases onto a live word, silently.

So a design that never addresses past that point is green whether or not the wrapper undoes anything.
The retired `bram_toy` filled 256 of 1024 words at **16 bits** — byte addresses 0…510, no wrap — and
stayed green straight through the defect. It took a *wider* design to expose it:
`examples/rf_shot_buf` at 64 bits wrote 256 words into 1024 and got the second half of its shot back
twice.

| word width | shift | words reachable in a 1024-word memory |
|---|---|---|
| 16 bits | `>> 1` | 512 |
| 32 bits | `>> 2` | 256 |
| 64 bits | `>> 3` | **128** |

**Choose a gated geometry that wraps.** If your example addresses fewer words than `depth / (W/8)`,
it is not testing this convention — it is only testing that it is self-consistent.
[`examples/bram_simple`](../../examples/bram_simple/) is gated at 64 bits with 256 of 1024 words for
exactly that reason: word 128 onward aliases immediately if anything here is wrong.

### The guard is a measurement, not a belief

`test_the_wrapper_undoes_the_shift_vitis_actually_emits` greps the RTL Vitis actually produced for
the shift it emitted and checks the wrapper against **that** number. If Vitis ever changes the
convention, the test fails with the two numbers side by side rather than a design quietly
mis-addressing its memory again.

**And a range check will not save you.** A `(pointer, count)` bounds check — like the one
`examples/bram_simple` performs — is in **words**, the caller's units. The scaling defect lives
*below* it, in the wrapper: a command reading words 0…255 of a 1024-word memory passes the range
check and still aliases. Two different failures, two different guards.

## Sequencing belongs in the design

[`examples/bram_simple`](../../examples/bram_simple/) is the worked example, and it makes the point by
having had to solve it. Its writer emits one token on an ordinary internal stream after its first
completed command; its reader waits for that token once, then serves commands. The witness this
example reproduces got the same ordering from its *testbench* (drive all 256 samples, then the
addresses) — which a concurrent BFM harness cannot do, because both drivers push from cycle 0. If
your reader must not overtake your writer, say so with a channel; do not rely on a testbench's
habits.

## See also

- [A module realized as Verilog](../comp_codegen/rtl_module.md) — the `rtl_module()` hook the memory
  declares, the port-name chain, and the latency single-source rule.
- [Free-running composite](../comp_codegen/freerunning_composite.md) — where the wrapper fits, and
  what `csynth` does *not* count.
- [Shared memory between two modules](../../examples/bram_simple/) — the worked example: two tasks,
  one memory, the wrapper, and the hazard scan that replaced the unheard `$error`.
- [Memory](../memory/) — the other storage categories, and which of them the tool chooses for you.
