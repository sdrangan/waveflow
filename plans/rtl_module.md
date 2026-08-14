# Plan: `rtl_module()` — a module realized as hand-written Verilog beside the kernel

**Status:** designed 2026-08-14. **S1, S2 and S3 landed 2026-08-14** (branches `rtl-module-s1`,
`rtl-module-s23`) — see the "What landed" blocks below.  **S4 not started**, and deliberately: its RX
design turns on whether `RxCmd` names a FUTURE window (no buffer needed) or a PAST one (buffer needed),
and those are different designs.  **Has a working witness** (see below) — every claim about what Vitis does
or refuses was measured, not recalled.

Split out of the RF arc, where `RfSampBuf` needs a circular capture buffer shared by two concurrent
accessors. It is not an RF feature: it is the **third realization hook**, and the same mechanism Flow 3
needs to instantiate vendor IP beside a generated kernel.

## Motivation: Vitis cannot put the memory inside the kernel

`plans/adc_model.md`'s `RfSampBuf` wants *"RX stream IF → RX buffer (two-port BRAM) → Data capture"* —
two free-running tasks sharing a random-access buffer. Four experiments settled what Vitis does with
that (all in `scratchpad/t2p*`, Vitis HLS 2025.1, `xczu48dr-ffvg1517-2-e`):

| # | construct | result |
|---|---|---|
| 1 | local `static ap_int<16> buf[1024]`, two tasks | **compiles — and silently means something else** |
| 2 | one top-level `bram` port, written by one task and read by the other | **hard error** |
| 3 | two sized `bram` array params, one direction each | **works, no gating** |
| 4 | #3 + hand-written TDP memory + wrapper, run in xsim | **PASS, ramp verified** |

**Experiment 1 is the dangerous one.** It csynths, and Vitis converts the array into a **PIPO dataflow
channel**:

```
INFO: [HLS 200-741] Implementing PIPO rx_buf_r_RAM_T2P_BRAM_1R1W using a single memory
```
```verilog
assign read_task_U0_ap_start     = buf_r_t_empty_n;   // reader gated on release
assign write_task_U0_ap_continue = buf_r_i_full_n;    // WRITER STALLS when full
```

So the code reads like a circular buffer while the RTL is a synchronized ping-pong — and the writer
stalls, which is the one thing a converter-facing stage may never do.

**Experiment 2** names the real rule:

```
ERROR: [HLS 200-976] Argument 'buf_r' failed dataflow checking:
Cannot read as well as write over function parameter.
```

The objection is **dataflow channel** semantics, not arbitration. Vitis's model has no notion of shared
memory between processes: any array crossing between them is a channel, and a channel is
single-producer/single-consumer with a handshake. That is not an oversight — DATAFLOW's promise is that
the parallel result equals the sequential C result, and a shared buffer with independent pointers has
no sequential-C meaning at all. Whether `buf[rd]` sees the old or new value depends on *when*, which C
does not express.

The division is about **who owns the correctness argument**: for a channel the tool owns it (and
enforces it with handshakes); for `m_axi` and for this plan the designer owns it, and the tool does not
interfere. A local array is inside the tool's analysis scope by construction, so it gets channel
treatment. There is no third option inside a kernel.

## The witness

Experiment 3 gives the shape and experiment 4 proves it runs:

```verilog
output [31:0] buf_w_Addr_A;  output buf_w_EN_A;  output [15:0] buf_w_Din_A;  output [1:0] buf_w_WEN_A;
input  [15:0] buf_r_Dout_A;  ...
assign write_task_U0_ap_start = 1'b1;   assign write_task_U0_ap_continue = 1'b1;
assign read_task_U0_ap_start  = 1'b1;   assign read_task_U0_ap_continue  = 1'b1;
```

Real BRAM interfaces, and **both tasks free-running with no gating** — no PIPO, no `full_n`/`empty_n`.
Then, against a hand-written TDP memory and a wrapper, in xsim:

```
T2P-BRAM EXPERIMENT: PASS (5 words, ramp verified)
```

Task A wrote `buf[i] = i+100` for 256 samples through one port; task B, addressed independently through
the other, returned `100, 101, 107, 355, 228` for addresses `0, 1, 7, 255, 128`. **Values, not
plumbing** — a latency mismatch would have shifted the ramp and failed.

## The model

`rtl_module()` is the third member of a family that already has two proven members:

| hook | declares | lands as |
|---|---|---|
| `kernel_task()` | "my pre-written `hls::task` body is *X*" | a task **inside** the generated top |
| `bfm_model()` | "my pre-written C++ cycle model is *Y*" | an `XsiSimObj` **beside** the top |
| **`rtl_module()`** | "my pre-written **Verilog** is *Z*" | an RTL module **instantiated beside** the top |

All three declare a pre-written artifact; none extracts one. The target is **`rtl_module`**, not
`verilog`: the vocabulary names *which realization*, not which language (`composite_kernel`,
`sequential_xsi_tb`), and the day this holds a vendor IP core "verilog" is the wrong word.

**The wrapper is the design scope.** A module containing the kernel *and* its memory is an
elaboratable boundary, which is the first boundary a resource estimate can be defined against. csynth
of the kernel alone reports **no BRAM at all** — the memory is invisible to it. That is not a side
effect of this plan; it is half the reason to want it, and it lands on
`plans/resource_model.md`'s territory.

## Stages

### S1 — the hook, the target, and the first module

`rtl_module()` on `HwModule`, `RTL_MODULE` in `codegen_targets.py`, and a `GenRtlStep`. Deliberately
narrow: a module may declare it only when

1. **every endpoint has a defined Verilog port mapping** — initially `BramIFSlave` alone; and
2. **a pre-written Verilog file (or files) exists** for the module with exactly those ports.

Plus `T2pBram` itself as the first and only consumer.

**Three things that must be explicit, not implementation detail:**

- **The port-name chain is the contract.** Waveflow endpoint → C++ parameter name → Vitis port names
  (`buf_w` → `buf_w_Addr_A`, `_EN_A`, `_Din_A`, `_Dout_A`, `_WEN_A`, and the whole B pair). This is the
  same problem `TopSpec.trace_manifest` already solved for AXIS and `m_axi` — *"Vitis picks only the
  `_U0` suffix; codegen owns everything else."* A new row in an existing table, not a new mechanism.
- **Latency comes from ONE declaration.** `#pragma HLS INTERFACE ... latency=N` and the Verilog's read
  latency must agree, and a mismatch is a **silently shifted ramp** — the highest-risk detail the
  experiment surfaced. If the pragma and the `.v` can be authored independently, they will
  desynchronize. Emit both from one number.
- **Resource footprint is declared here.** `T2pBram` states its own BRAM/URAM cost from depth × width.
  Without it the wrapper is a scope you cannot count, which is half the point.

**Keep the read-during-write assertion.** The witness's `bram_t2p.v` `$error`s when port B reads the
address port A is writing — the design invariant (`rd` trails `wr`), checked where nothing else would.
A hand-written memory is *more* verifiable than an emulated one, and that is worth saying out loud.

**Gate:** `check(T2pBram, "rtl_module")` True; a module with a non-mappable endpoint refused by name;
the emitted pragma latency and the Verilog's latency provably equal.

#### What landed (2026-08-14)

| piece | where |
|---|---|
| the hook | `HwModule.rtl_module()` (base raises; declaration is the override, by `declares_hook`) |
| the target | `RTL_MODULE` in `codegen_targets.py` — in `ALL_TARGETS`, `IMPLEMENTED_TARGETS`, `REALIZATION_HOOKS`, and **`CUT_INDEPENDENT_TARGETS`** |
| the rules | `build/rtl_gen.py`: `RtlModule`, `PortMapping`, `rtl_port_mapping`, `rtl_read_latency`, `verilog_module_ports`, `resolve_rtl_module` |
| the step | `build/rtl_steps.py::GenRtlStep` — resolves at construction, copies with `write_bytes` |
| the port names | `composite_gen.py::bram_port_signals` + `_bram_port` + a `"bram"` row in `_boundary_trace` |
| the module | `hw/bram.py`: `BramIFSlave`, `T2pBram`, `ramb18_count` |
| the artifact | `build/rtl/bram_t2p.v` (package data), copied from the witness |
| docs | `guide/comp_codegen/rtl_module.md`; rows added to `guide/flows/modules.md` + `guide/flows/index.md` |
| tests | `tests/build/test_rtl_module.py` (28), `tests/build/test_gen_rtl_step.py` (2) |

**Deviations from the plan as written, all deliberate:**

1. **`RTL_MODULE` is cut-independent.**  The plan did not say which side of `potential_targets` it
   falls on.  It is in `CUT_INDEPENDENT_TARGETS` with `xsi_bfm_model`, by the same argument: the same
   memory is hand-written RTL in a synthesized design and a `FlatMemory` BFM in an XSI testbench, with
   nothing about the memory changed.  A `potential_targets` entry would freeze a per-build role into a
   class fact.  It is also what makes `check(T2pBram, "rtl_module")` answerable at all — `T2pBram` is a
   plain `HwModule`, so its `potential_targets` is empty and gate 2 would otherwise refuse.

2. **The latency's single source is the `.v`, not Python.**  The plan said "emit both halves from one
   number" without saying where the number lives.  Putting it in Python would have left the Verilog
   free to disagree; putting it in the Verilog and *deriving* the pragma leaves Python with nothing to
   disagree *with*.  So the artifact publishes `localparam READ_LATENCY = 1;` and
   `rtl_read_latency()` reads it.  `localparam` rather than `parameter` is the statement that it is a
   property of the implementation, not a knob an instantiation may turn.  `T2pBram.read_latency` is a
   property with no setter; the test that proves it asserts both directions (changing the file changes
   the pragma; the module cannot be told a different number).

3. **The artifact differs from the witness by exactly one insertion** — 7 lines: 6 of comment plus
   `localparam READ_LATENCY = 1;`, CRLF like the rest of the file.  Everything else, including the
   `$error` read-during-write assertion, is byte-identical.  `test_shipped_memory_is_the_witness_plus_
   the_published_latency` proves it with `difflib`: one opcode, `insert`, and every inserted line is a
   comment, a blank, or that `localparam`.  The witness was **not** modified.

4. **The pragma emitter landed, but nothing in a build emits it yet.**  `_bram_port()` exists beside
   `_axis_port`/`_maxi_port` and is unit-tested (sized array, never a pointer — the `ap_vld` trap; and
   `latency=` from the file).  No kernel carries a `bram` port until S4, so the *authoritative* "the
   pragma took effect" check — a csynth port list — is deferred to where a kernel actually has one.
   Stated rather than skipped: the static half is gated, the toolchain half is not yet.

5. **A one-row mapping table, as planned, plus a fourth check the plan did not list:** the declared
   Verilog is parsed for its module header and every mapped port name must be a port that module
   declares.  Cheap, and it catches the class of error the port map exists to make impossible.

6. **`T2pBram` has no pysim behaviour.**  The plan's open question ("a plain `HwModule` whose
   `run_proc` is a numpy array?") stays open: with no `BramIF` to drive it there is nothing to model,
   and guessing which endpoint carries the access latency before the wiring exists is how the wrong
   answer gets committed.  S2 answers it.

7. **`addr_bits` refuses a non-power-of-two depth** rather than rounding up.  The Verilog indexes
   `mem[addr[AW-1:0]]`, so any other depth aliases high addresses onto low ones — silently, which is
   the same failure class as the latency mismatch.

### S2 — wiring, which is probably cheaper than it looks

A `BramIF` is **not an internal interface of the Vitis composite**: one end is inside the kernel, one
end is outside. So by the existing rule — *"a child endpoint not bound to one of the composite's
internal interfaces **is** a boundary port"* (`hw_freerun.py::boundary`) — the BRAM endpoint becomes a
boundary port **automatically**, plumbed out by machinery that already runs. Register the `BramIF` on
the *system* graph, never as an `add_if` of the Vitis composite.

What is genuinely new is the **wrapper emitter**: a module instantiating the kernel plus the memories
and joining them. The witness's version is 49 lines and entirely mechanical, because both sides' port
names are known at generate time.

**Check before scoping this:** does `derive_boundary` thread an unbound endpoint up through *N* levels
of composite? `mem_copy` is one level and has never had to. If nesting already works, S2 is mostly the
wrapper; if it does not, that is a **general gap this plan merely surfaced**, and it should be fixed as
one rather than worked around here.

**Naming:** keep csynth's own name for the kernel (`rf_samp_buf.v`) and call the wrapper
`rf_samp_buf_top.v`. One artifact keeps the name it has; the new one is visibly the outer layer.

**Gate:** a two-module fixture emits a wrapper that elaborates; every existing design byte-identical
(none declares the hook).

#### What landed (S2, 2026-08-14)

**`derive_boundary` does NOT thread through nesting — measured first, as instructed.**  A two-level
fixture (`Outer` -> `Inner` -> `Leaf`) raises *"names 2 port(s) … but the graph has 0 unwired child
endpoint(s)"*: the walk reads each child's own `endpoints` registry, and a composite child's is
empty.  **S2 does not need it** — the tasks are direct children of the composite, one level — so this
is recorded as a general gap rather than worked around.  Whoever nests a composite inside a composite
will meet it; it is not a BRAM problem and must not be fixed with a BRAM special case.

| piece | where |
|---|---|
| the accessor endpoint | `hw/bram.py::BramIFMaster` — a sized `mode=bram` array in C++, 14 RTL ports |
| the wire | `hw/bram.py::BramIF` — checks geometry + direction at bind; carries `read_latency` |
| the registries | `HwModule.add_rtl_mod` / `add_rtl_if` (+ lazy `rtl_mods` / `rtl_ifs`) |
| the wrapper | `build/wrapper_gen.py`: `WrapperSpec`, `MemInst`, `wrapper_spec`, `render_wrapper` |
| the step | `build/rtl_steps.py::GenWrapperStep` |
| the elaborated top | `TopSpec.rtl_top` / `.pin_ports` / `.elab_top`; `composite_gen.wrapper_name` |
| pysim | `BramIFMaster.mem_read/mem_write` (untimed) + `T2pBram.storage/store/load` |
| tests | `tests/build/test_wrapper_gen.py` (18) |

**Deviations, both deliberate:**

1. **The `BramIF` is registered on the composite, not on the system graph.**  The plan said *"never as
   an `add_if` of the Vitis composite … register it on the system graph"*.  It is registered on the
   composite through `add_rtl_if` — a registry that is **not** `add_if` — which honours the
   instruction's *purpose* (the accessor's port stays a boundary port, with zero changes to
   `derive_boundary`) while keeping the memory in the same elaborated object as the kernel.  That is
   what preserves S1's latency contract: `elaborate(BramToy)` alone can reach the memory's published
   `READ_LATENCY`.  On the system graph the kernel would elaborate with an **unbound** bram port, and
   the only way to emit its pragma would be a second latency authored in Python — the exact drift S1
   removed.  The composite is then the *design scope* (kernel + memory), which is what the plan
   already says a wrapper is.

2. **A Verilog-keyword guard the plan did not anticipate.**  The instance name comes from the Python
   attribute, and the first design called its memory `self.buf` — a Verilog **primitive gate**.  The
   emitter refuses by name with the fix, rather than letting `xvlog` fail on a syntax error that
   mentions no Python.

**One trap, for the record:** `rtl_mods`/`rtl_ifs` as dataclass *fields* moved the structure signature
— and therefore the calibration key — of every module in the repo, and two `fir_block` resource
numbers in the docs went stale within one test run.  They are created lazily now (the `add_state`
precedent).  Third time this trap has bitten; see `plans/harmonize_calib.md`.

### S3 — XSI, the cheapest of the four

Two changes: the `.f` gains the memory and wrapper files, and the **xelab top becomes the wrapper**.
Note the witness needed *all four* generated files (`rx.v`, `rx_read_task.v`, `rx_write_task.v`,
`rx_regslice_both.v`) — a `.f` naming only the top does not elaborate.

**The BFM is untouched.** The memory is internal to the elaborated design, so the testbench still sees
only AXIS and `m_axi`. That is what makes this step small.

**One wrinkle:** `trace_manifest` derives net names in *"the top's own scope"*. With the wrapper as top,
the kernel's internals sit one level deeper, so trace and timing consumers need a scope prefix.

**Gate:** a design with a `T2pBram` runs under XSI with a recorded cycle count; the four existing cycle
gates unmoved.

#### What landed (S3, 2026-08-14) — and the acceptance gate

`examples/bram_toy` is the witness's design expressed in Waveflow, and it **reproduces the witness's
values through real RTL**: addresses `0, 1, 7, 255, 128` returned `100, 101, 107, 355, 228`.  Not
shifted by one, so the latency chain — `localparam READ_LATENCY = 1` → `latency=1` in the generated
C++ → the memory's actual behaviour — holds end to end.  **New cycle gate: 529** (time to the last
answer; most of it is the design's own sequencing).

S3 was as small as predicted.  Three changes, no BFM work:

* `render_rtl_f(top, root, extra=(...))` appends the memory and the wrapper after csynth's own files
  (all five of them — a `.f` naming only the top does not elaborate);
* the runner is invoked with the **wrapper** as `TOP`, so the `.f`, the snapshot and the shared
  library are named for it while csynth's project keeps the kernel's name;
* `render_ports_h` emits `spec.elab_top` and skips bram ports, and `tb_top_spec` skips them too —
  they are not pins on the elaborated design, so there is nothing for a model to bind and no dual to
  look up.

**The gate S1 deferred is closed.**  The generated kernel declares all 28 bram ports, exactly the
names `bram_port_signals()` derived without ever seeing this RTL, with no `ap_vld` degradation — read
off `bram_toy.v`, not off an exit code.  A second test asserts both tasks are
`ap_start`/`ap_continue` `= 1'b1`, i.e. **not** the PIPO structure experiment 1 produced.

**One thing the plan did not foresee: sequencing.**  The witness's `tb.v` drove 256 samples and *then*
the addresses; a concurrent BFM harness cannot (both `AxisMaster`s push from cycle 0), so a read of
address 255 would land hundreds of cycles early.  The ordering moved into the **design** — the writer
emits one "buffer ready" token on an ordinary internal stream, the reader waits for it once — which is
also exactly the invariant the memory asserts, and the gate checks that `$error` never fired.

The wrinkle the plan flagged (`trace_manifest` derives names in *"the top's own scope"*, so a wrapped
kernel's internals sit one level deeper) is **real and untouched**: nothing in `bram_toy` traces or
times, so no consumer needed the scope prefix yet.  Whoever traces a wrapped design first will need
it.

### S4 — `RfSampBuf`

The consumer. `plans/adc_model.md` staging item 3, now with a buffer that is expressible.

## Docs

| page | status | what it says |
|---|---|---|
| `guide/comp_codegen/rtl_module.md` | **written (S1)** | The third hook, beside `kernel_task()` and `bfm_model()`. The port-name contract, the latency single-source rule, and the conformance obligation. Also carries a "what is not here yet" section naming the wrapper, the wiring and XSI. |
| `guide/flows/modules.md` | **edited (S1)** | The hook table is three rows, and the cut paragraph distinguishes "beside it in the same design" from "outside the design". |
| `guide/flows/index.md` | **edited (S1)** | "Two targets that are not flows" — `xsi_bfm_model` and `rtl_module`. |
| `guide/interface/bram.md` | **new** | `BramIF` and why a memory shared between processes cannot live inside a kernel — with the PIPO and dataflow-check evidence, because "Vitis won't let you" is unconvincing without it. |
| `guide/comp_codegen/freerunning_composite.md` | edit | The wrapper as the **design scope**, and that csynth of the kernel alone does not count memory outside it. |
| `guide/flows/modules.md` | edit | One row: a module realized as RTL beside the kernel. |

**The conformance obligation is the third instance of a known one.** Nothing checks that the Python
model and the Verilog agree — exactly as nothing checks Python against C++ for `bfm_model()`. Same
answer, stated once and referenced twice: a byte-identical vector gate.

### `guide/memory` — the reorganisation this arc earned

The section says storage is *"three objects, split by where the storage lives and who is responsible
for it."*  The axis is right; the list is short, and `BramMod` makes the gap legible. Restate it as
**six categories ordered by the scope of sharing**, with the organising rule stated up front:

> **The scope of sharing determines the category — not the size, and not the lifetime.**

| # | category | mechanism | who picks the storage class |
|---|---|---|---|
| 1 | local temporaries, one module | plain Python / plain C++ | Vitis, from the body |
| 2 | persistent, one module | `HwState` | Vitis + directives |
| 3 | **between modules, inside the top** | **`BramMod` + `BramIF`** (this plan) | **the designer — it is hand-written RTL** |
| 4 | outside the top | AXI-MM (`MemoryMod`, `MemMgr`) | the platform |
| 5 | channel storage | `StreamIF.depth` | Vitis, from the pragma |
| 6 | block handoff | `stream_of_blocks` | Vitis — **implicitly, if you share an array between tasks** |

5 and 6 are additions, and both bit this arc. A FIFO **is** memory and is the storage most designs have
most of, yet `StreamIF.depth` lives under `interface/` — which is why nobody noticed that a boundary
port's declared depth is silently discarded. And 6 belongs here because **Vitis creates one whether or
not you asked**: sharing a local array between two tasks yields a PIPO with a stalling handshake
(experiment 1). A reader should meet that in the memory guide, not in a netlist.

Category 1 needs one sentence readers will otherwise get wrong: **the Python model is not a storage
spec.** A numpy array in `run_iter` need not correspond to anything in the RTL; only functional
behaviour is contracted. `RfSampIngress` is the worked example — a burst in pysim, a word relay in
hardware.

Two cross-cutting tables the section lacks, both earned by this arc:

- **what each backend models** — FIFO depth (pysim honours the declaration; RTL only for internal
  channels), AXI-MM (pysim has `BusCalib` timing *and* crossbar contention; XSI has an
  **un-arbitrated** `FlatMemory`), BRAM (both faithful — deterministic latency). Note this is not
  one-sided: **pysim is the better memory-system model, XSI the better fabric model.**
- **what `csynth` counts** — categories 1, 2, 5, 6 yes; 3 and 4 **no**. That is the resource story,
  and it is the table `plans/resource_model.md` needs.

`Region` gets a mention as a cross-cutting *access view* (element coordinates over word storage), not a
seventh category.

**Timing:** the reorganisation and categories 5–6 are earned **now** — the reorg is what makes the
category-3 gap legible. The category-3 page waits until S1 lands, same discipline that has paid off
three times in this arc.

## Verification

- The four XSI cycle gates unmoved through S1–S3; a new one recorded in S3.
- The scratchpad witness (`t2p/`) is the acceptance shape for S3: kernel + memory + wrapper, ramp
  verified. Promote it into a real example rather than re-deriving it.
- **`csynth` OK is not evidence** — experiment 1 csynths and is wrong. Everything here is gated on xsim.

## Not in scope

- **Generating Verilog from Python.** The anti-goal, same as `xsi_tb_codegen.md` Stage 0 and
  `behavioral_edges.md`: the artifact is *declared*, never extracted. A generator would be re-deriving
  verified code.
- **Endpoints other than `BramIFSlave`.** The hook is general; the mapping table starts with one row.
- **IPI / `bitstream`.** This is the mechanism Flow 3 will want for vendor IP, proven cheaply in xsim
  first. Do not design for IPI here.

## Open questions

- Does `derive_boundary` handle nested composites? **Answered: NO** — measured in S2 with a two-level
  fixture; it reads each child's own `endpoints` registry, and a composite child's is empty.  S2 did
  not need it (the tasks are direct children), so it stands as a **general gap**, not a BRAM one.
- Where does the wrapper's resource number come from? **Settled enough to build on** — see
  "Resource accounting" below. Declared for S1; the open part is only when a measured run gates it.
- ~~Does a `T2pBram` need a pysim model of its own?~~ **Answered in S2**: a numpy array on the module,
  accessed untimed through the endpoint.  "Which endpoint carries the access latency" turned out to be
  the wrong question — neither does; the memory publishes it and both ports read it from there.
- One memory, two ports, and Vitis emits an **A/B pair per interface** — four physical ports for one
  memory, of which the witness wires the A half of each. Is that always the right choice, or should the
  B halves ever be used (two accesses per cycle per side)?

## Resource accounting: where a per-module number can come from

Three sources, and they are available at different stages with different fidelity:

| stage | granularity | fidelity | sees a hand-written module? |
|---|---|---|---|
| **HLS `csynth`** | per generated sub-module (the report's Detail section) | *estimates* — reliable for BRAM/DSP, unreliable for LUT/FF | **no** — it is outside the kernel |
| **Vivado `synth_design`** | `report_utilization -hierarchical` → **per instance** | real, pre-place | yes |
| **Vivado implementation** | same, post-route | final | yes |

So a per-Verilog-module number **is** obtainable, but only from Vivado synthesis onward — and only if
the hierarchy survives. Optimisation crosses module boundaries by default; `KEEP_HIERARCHY` or
`-flatten_hierarchy none` preserves it *for reporting* at some QoR cost. That trade has to be made
deliberately, because a design synthesised for accurate per-module reporting is not the design you
ship.

**For `T2pBram` specifically, hardwiring is not a stopgap — it is the correct method.** A memory's
footprint is *structural*: depth × width maps to a primitive count by geometry (RAMB18 = 18 Kb,
RAMB36 = 36 Kb, with width-dependent aspect ratios and TDP-vs-SDP differences). No tool run is needed
to know that a 1024×16 true-dual-port buffer is one RAMB18. The rounding rules are tool-specific, so
the declared number should eventually be **gated against a real synthesis** rather than trusted
forever.

That is the same two-tier shape the calibration work already uses — a cheap derived value, an
authoritative measured one, and a regression guard between them. `plans/resource_model.md` should
inherit the pattern rather than invent a second one.

The general rule the taxonomy implies: **structural blocks (memories, FIFOs) can declare their
footprint; logic blocks cannot and need a run.** That is the same line as "who owns the correctness
argument" one paragraph up — the designer owns what is derivable, the tool owns what is not.

## Notes carried in

- **A silently-ignored pragma is a real failure mode here.** `mode=bram` on an *unsized* pointer
  parameter silently produced an `ap_vld` scalar port — no warning. Same family as Vitis ignoring
  `depth=` on a top-level argument (`plans/adc_model.md`). Any generated pragma in this plan needs a
  check that it took effect.
- **`m_axi` remains the alternative**, and it is proven (`mem_copy`). It costs off-chip bandwidth and an
  un-arbitrated XSI memory model; this plan costs a wrapper and a hand-written `.v`. For a buffer
  running at the sample rate, on-chip with deterministic latency is the better trade — but the choice
  should be stated, not assumed.
