# Plan: the XSI testbench — library first, then `sequential_xsi_tb`

## Context

Flow 2's DUT half now generates: a composite's `ap_ctrl_none` top comes from the component graph
(`composite_top_spec`), and as of `e833a13` its task **bodies** come from `run_iter`
(`task_files_to_str`). The TB half does not. `examples/interleaver/xsi/mem_copy_bfm_tb.cpp` is 290
lines, ~230 of them one `main()`; the four XSI TBs total 956 lines. They are hand-written, and asking
a user to write one is asking them to hand-implement an AXI4 slave.

**The measurement that decides the approach (2026-07-16).** The AXI read/write FSM in
`interleaver_canon_bfm_tb.cpp` is **line-for-line identical** to `mem_copy_bfm_tb.cpp`'s — same five
states (`AR_IDLE`/`R_SEND`, `AW_IDLE`/`W_RECV`/`B_RESP`), same `wstrb` read-modify-write, same `rlast`
arithmetic — differing only by a `g0_`/`g1_` prefix because that design has two bundles. It is
copy-paste-and-rename, four times. What `mem_copy_bfm_tb.cpp` is actually made of:

| Part | ~lines | Nature | Wants to be |
|---|---|---|---|
| AXI read/write FSMs, `driveAll`, cycle loop, sampling | ~90 | design-independent | **library** (duplicated x4) |
| XSI open, reset, timeout, zero-pinning of undriven inputs | ~40 | boilerplate | **library** |
| Port handles (`P_cmd_data` .. `P_bready`) | ~31 | derivable from the boundary | generated |
| `s_done` accumulate + `xfer_msg[0]` echo check | ~17 | schema-aware | generated |
| Scenario (`N`, `NUM_CMDS`, `SRC_W[]`, `DST_W[]`, `known_word`) | ~18 | per-test | generated |
| `cmd_words` packing (`src \| dst<<32`) | ~7 | **`CopyCmd.serialize()`, re-written by hand in C++** | generated |
| Golden check (dst region == src region) | ~22 | per-test | generated |

~45% library, ~35% generated, ~20% boilerplate.

**Why the host-activated TB was easy and this is not.** `SeqTB.main()` is a *sequential program that
calls the DUT through a driver*; Vitis csim supplies the protocol underneath. There is an abstraction
boundary, so lowering `main()` is a small job. For a free-running DUT under XSI **there is no
abstraction** — the TB *is* the protocol peer, cycle by cycle. The difficulty is not that XSI is
harder to generate; it is that we are generating into a missing layer.

**The move: do not generate the protocol. Build the abstraction, and the monster stops existing.**
Emitting ~90 lines of AXI FSM per design would produce the same code every time, in the language
where it is hardest to review, to solve what one library solves once. With a BFM library the residue
is a sequential program against an API — the shape we already generate well:

```cpp
XsiSim sim("xsim.dir/mem_copy/xsimk.dll");
FlatMemory mem(8192);
AxiMmReadSlave  gmem0(sim, "m_axi_gmem0", mem);
AxiMmWriteSlave gmem1(sim, "m_axi_gmem1", mem);
AxisMaster s_cmd(sim, "s_cmd");
AxisSlave  s_done(sim, "s_done");
for (auto& j : jobs) s_cmd.push(j.serialize());
sim.run_until([&]{ return s_done.count() == NUM_CMDS; });
```

## Stage 0 — measure (DONE 2026-07-16)

The table above. Recorded here because it is the evidence for "library, not codegen"; if a later
stage is tempted to emit protocol, re-read it.

## Stage 1 — extract the BFM library from the four working TBs — **DONE (`709bb1e`, 2026-07-16)**

All four TBs run on `xsi/bfm/xsi_bfm.h`; every gate held from a fully clean rebuild.
`956 -> 495` lines across the four, `+347` shared, and **zero** protocol constructs left in any TB.
Stage 2 (thin to scenario + golden) fell out with it — that is all the TBs now contain.

What the extraction taught, which designing the API would not have:
- `mem_w` drains on the **B response**, not the word count → `AxiMmWriteSlave::saw_b()`. "All the
  data went out" is not "the write completed"; a TB draining on `w_count` alone can stop before the
  last burst is acknowledged.
- `interleaver_canon` records **per-job done cycles** (that print *is* the throughput measurement),
  so the sink models words while the TB derives timing from its beat count.
- The `sample`/`update`/`drive` split is load-bearing, not stylistic: a beat is decided from values
  sampled *before* the rising edge and applied *after* it.



**Pure refactor. Zero behaviour change.** Factor `mem_r` / `mem_w` / `mem_copy` /
`interleaver_canon` TBs onto a shared BFM in `examples/interleaver/xsi/bfm/` (location revisited in
Stage 6):

- `XsiSim` — open/close, clock phases, reset, `run_until(pred, max_cycles)`, timeout reporting.
- `FlatMemory` — the `std::vector<uint64_t>` arena + `wstrb` RMW.
- `AxiMmReadSlave` / `AxiMmWriteSlave` — one per bundle, constructed with a port prefix
  (`m_axi_gmem0`), owning the FSM that is currently inlined and renamed per design.
- `AxisMaster` / `AxisSlave` — `push(words)` / `count()` / `words()`.
- Zero-pinning: drive every TB-driven input not otherwise claimed to 0, by enumeration.

**Why first, and why not "design the API".** The four TBs are working witnesses; extracting from them
*discovers* the API instead of guessing it. We guessed an API once on this codebase — `CodegenSource`,
designed against a presumed `HwParam` surface — and it was wrong and got reverted
(`plans/codegen_source_options.md`). Do not repeat that here. The API is whatever falls out of making
four real TBs share code.

**Gate — the recorded baseline.** Automated as `pytest -m xsi` (`06032cd`); numbers **re-recorded
2026-07-16** when the reporting bug below was fixed:

| TB | result | **cycles** (to last completion) | drain tail |
|---|---|---|---|
| `mem_r_stream` | PASSED, collected=128 | **158** | 256 |
| `mem_w_stream` | PASSED, w_count=128 | **176** | 256 |
| `mem_copy` | PASSED, done=16, w_count=2048, job_fails=0 | **2835** | 512 |
| `interleaver_canon` | PASSED, done=8/8, n=256 nj=8 | **3469** | 512 |

**The originally-recorded 414 / 432 / 3347 were wrong** — not the designs, the *measurement*. Three
of the four TBs printed `cyc` (work **+** a fixed drain tail) under the label `cycles=`, while
`interleaver_canon` printed time-to-last-done. So the two small kernels were reporting numbers that
were *majority tail*, and any of them would have shifted if someone touched a testbench constant.
All four now report time-to-last-completion with `(tail=N)` shown separately.

**What the numbers say, now that they say anything:** `mem_copy` is 2835/16 = **~177 cyc/job**
against **~176** for ONE write on its own. The reads hide *entirely* behind the writes — per-job cost
is `max(read, write) = 176`, not `read + write = 334`. That ~1.9x is the free-running pipeline, and
it is exactly what Stage 5 must not destroy.

A cycle count that moves is a real behaviour change — a regression or an improvement — and both want
a human look. Exact, never a bound.

**Watch:** `xsi/rtl_<top>.f` lists RTL files explicitly, is **hand-maintained** (nothing generates it —
only `run.bat` reads it), and a stale `.f` plus a cached `xsimk.dll` can fake a PASS
(`project-mem-stream-phase2-gate2`). It is also RTL-module-name-sensitive: swapping in the generated
`mem_seq_task.h` renames `..._s_r_xfer_msg_RAM_...` to `..._s_mr_xfer_msg_RAM_...` (the body's local
is `mr`, not `r`), which the `.f` names explicitly. Regenerate before believing any result here.

## Stage 2 — thin each TB to scenario + golden

With the library in place each TB should be ~20-30 lines: build the arena, push commands, run until
done, check. Anything that will not thin is a finding — it is either genuinely per-design (keep it
visible) or a gap in the library (fix the library).

**Gate:** same as Stage 1. Bit-exact, same cycle counts.

## Stage 3 — derive the port binding from `TopSpec` — **DONE (`5c64537`, 2026-07-16)**

`render_ports_h(spec)` emits `<top>_ports.h`; all four TBs bind through it, and not one hand-written
port name or pin-low list remains. Gates unchanged (then 414/432/3347/3469; re-recorded below). Pinned by
`tests/build/test_ports_header.py`, including the drift claim itself and the never-pin-what-you-drive
condition. The enabling fix was in the **spec**, not the renderer: `ExtPort` kept only rendered
strings, so `(name, kind, bundle)` was lost — `TopSpec` could be *rendered* but not *asked*. It now
carries the triple, and `xsi_prefix` encodes the asymmetry (AXIS keeps its own name; `m_axi` is named
after its **bundle**). `ZERO_PORTS` is derived as the complement of what the BFM drives.

One real difference the gate adjudicated: the derived set pins `m_axi_gmem1_BRESP`, which mem_copy's
hand list omitted while interleaver's had it — the two hand lists had drifted from *each other*.
`3347` unchanged, so it is inert, and the inconsistency is gone. Exactly the class of bug this stage
exists to make impossible.

**Still open — the other half of drift:** `rtl_<top>.f` remains hand-maintained. A `.f` generator was
validated this session (it reproduced the tracked file byte-for-byte before being trusted on a
renamed-RAM case) but is not wired into any build step. Until it is, a renamed RTL module plus a
cached `xsimk.dll` can still fake a PASS.

### Original notes


`P_cmd_data = d.port("s_cmd_TDATA")` is not information — it is `("s_cmd", axis_in)` from the
boundary spec plus Vitis's mechanical naming (`<port>_TDATA`, `m_axi_<bundle>_ARVALID`). The same
`TopSpec` that emitted the top's `#pragma HLS INTERFACE` lines determines every port name.

**Derive the TB's binding from that one spec** and the TB cannot drift from the kernel by
construction: one spec, two consumers. Cross-check each derived name through `get_port_number` at
startup and **fail loudly** if absent — which also converts the stale-`.f` false-PASS from a silent
wrong answer into an error.

**Not** by parsing RTL or reports: those are downstream artifacts, and reading them back would make
the TB depend on the thing it is supposed to test.

## Stage 4 — scenario and golden from the Python — **PARTLY DONE (`1f976b9`, 2026-07-16)**

**Done — the schema-drift half.** `mem_copy`'s command words are now the *output* of the real
`CopyCmd.serialize()`, emitted into `xsi/mem_copy_vectors.h` (`render_vectors_h`, plain C++ no HLS
types); `DONE_WORDS` is `MemComplete.nwords_per_inst(width)`. The scenario lives in `mem_copy.py` as
`XSI_*`. Two toolchain-free tests run in the fast loop: the committed header must match what the
schema produces now (verified to fail), and `CMD_WORDS` must *be* `serialize()`'s output (so a
hand-rolled packer cannot come back).

**The constraint that decided the design:** an XSI TB is host-compiled by mingw g++ against the xsim
headers only, and `copy_cmd.h` needs `ap_int.h`/`hls_stream.h`. So a TB *cannot call the schema* —
which is why the duplication existed. Emitting serialize()'s output removes the second
implementation; generating a plain-C++ packer would have re-created it.

**Not done — the pattern half.** `known_word` is still stated in both `mem_copy_sim.py` and the TB.
Unifying means emitting the whole arena image as data. Deliberately deferred: a drifted *pattern*
still tests a copy, whereas a drifted *packing rule* sends malformed commands and makes the test
meaningless — that was the one worth removing. `interleaver_canon`'s scenario (`Pidx`/`Xval`/`fbits`)
is untouched for the same reason.

### Original notes


- `cmd_words` packing is `CopyCmd.serialize(word_bw=64)` re-implemented by hand in C++. Use the
  schema. This is the same silent-drift class as hand-written task headers drifting from their schema.
- `DONE_WORDS = 5` is `MemComplete.nwords_per_inst(64)`.
- `SRC_W[]`/`DST_W[]`/`known_word()`/the golden check are the pysim harness's `run_copy(jobs=...)`
  restated in C++. One statement of a test, two backends (pysim, XSI) — the same relationship
  `mem_copy_sim.py` already has to the golden.

## Stage 5 — the TB is a `CompositeComp` (**premise replaced 2026-07-16**)

**The original premise was wrong, and the open question is answered — negatively.** Stage 5 used to
ask: is `sequential_xsi_tb` just `sequential_vitis_tb`'s extraction with a different emitter backend?
**No.** A `SeqTB.main()` is a sequential program against a **DUT-as-C++-function** — `yield from
dut.run_once_sim(x, a, b)` lowers to a direct `simp_fun(x,a,b,y)` call, and csim *calls* the kernel.
Under XSI the DUT is **elaborated RTL behind a dll**; there is no function to call. "Blocking" means a
call in one and *run cycles until a condition* in the other. Same rules, different semantics — a new
path, not a retarget.

Worse, the obvious lowering is actively harmful. Lower `write(); get(); write(); get()` per job and
you serialise the pipeline: correct memcpy, bit-exact golden, every test green — and ~334 cyc/job
instead of ~177, silently destroying the thing the design exists to demonstrate.

### The framing that replaces it (user's, and it dissolves the problem)

**Run a fixed N cycles with no early termination.** Then there is no sequencing to schedule, because
nothing blocks — and the pipelining survives *by construction*, for the same reason it survives today:
the source never waits. The TB stops being a program and becomes **a concurrent network of
components**, which is the abstraction we already have:

- `AxiMasterSource` / `AxiSlaveSink` / `MemEmulation` — participants, each with ports.
- `TBTop` — a `CompositeComp` holding those three **plus the DUT**, wired by interfaces.
- The generated `main()` — instantiate the participants, loop N cycles, step each one.

Two build steps: `CodegenXSI(c)` per participant, `CodegenXSITB(c: CompositeComp, ncycles: int)`.

**It already exists, one abstraction short.** `mem_copy_sim.py::run_copy()` *is* `TBTop`:
`MemComponent` (= MemEmulation), `CmdDriver` (= AxiMasterSource), `WordSink` (= AxiSlaveSink), the
`MemCopy` DUT, and their `StreamIF`/`AXIMMCrossBarIF` bindings. It simply is not declared as a
`CompositeComp`, so `composite_top_spec` cannot walk it. Declaring it is not new machinery — it is
naming something already written, and it makes one object generate **both** the pysim run and the XSI
TB. That is "one statement, two backends" arriving as a consequence rather than a feature.

### The refinement that shrinks it

**Do not *lower* the BFM models — *map* them.** `AxisMaster`'s FSM is already written and cycle-exact;
extracting an equivalent from Python would be re-deriving verified code. A participant declares which
library model it is, exactly as `kernel_task()` declares which hand-written `hls::task` body a
component uses:

```python
def kernel_task(self):  return KernelTask("mem_seq_task", "mem_seq_task.h", (...), ...)   # existing
def bfm_model(self):    return BfmModel("AxisMaster", ("stream_ep",), ctor_args=("cmd_words",))  # proposed
```

So `CodegenXSI` is a **resolver**, not an extractor — the same shape as `composite_top_spec`: walk the
graph, resolve each participant's ports to RTL prefixes (that is `render_ports_h`, already built),
emit constructions. No new extraction rules, no cycle-FSM lowering. That is the difference between a
large project and a medium one.

**File I/O finishes Stage 4.** If the source reads a file and the sink dumps one, the C++ does **no
checking at all** — the golden moves to Python comparing files, where it already lives. That also
kills the last duplication Stage 4 left (`known_word`, stated in both `mem_copy_sim.py` and the TB).

### Early termination is an ADD-ON, deliberately staged second

Fixed N is **correct on its own** — it just wastes wall-clock. So build it first and settle the risky
question (does the pipelining survive?) before the convenient one. Then add termination as a
participant the scheduler polls: a class holding whatever it needs to decide, exposing a predicate the
loop checks each cycle.

`XsiSim.run_until(pred, max_cycles, drain)` was in this plan's own Stage 1 sketch and **got dropped
during the extraction** — each TB's loop had its own tangled drain-and-measure logic, so no common
shape presented itself. It is the seam this reopens:

```cpp
sim.run(N);                                  // base case: fixed N, nothing blocks
sim.run_until(pred, max_cycles, drain);      // add-on: stop early, then drain K more
```

**Declare the condition; do not lower a predicate.** The same "map, don't lower" rule as `bfm_model()`.
An arbitrary Python lambda over participants would need real extraction; a *declaration* from a small
vocabulary is data:

```python
self.stop_when = ExpectWords(self.done_sink, njobs * DONE_WORDS)   # -> `s_done.count() >= 80`
```

That also drags today's magic drain constants (512 / 256, different per TB, unexplained) into the open
as an explicit parameter of the stop declaration.

**The trap, learned the hard way (`8405415`).** Termination and measurement must stay strictly
separate: **the terminator decides when to stop; the SINK reports when it completed.** Those were
entangled in three of the four hand-written TBs — the loop counter (stop time, including a fixed drain
tail) was printed as `cycles=`, so `mem_r`'s headline number was 62% tail and `mem_copy`'s was inflated
by 512. If a terminator's drain leaks back into the reported number, 3347 returns. Whatever `run_until`
looks like, time-to-completion comes from the participant that observed the completion.

### The emitter's shape (settled in discussion 2026-07-16)

**The interface owns its lowering — and this is the seam that already exists.** `composite_gen`'s own
docstring says *"an edge declares how it lowers"*, and `StreamEdge.decl()` / `SobEdge.decl()` already
do exactly that for the kernel target. The XSI emitter is that same seam with a second target hung off
it, not new machinery:

```
walk sub-components  -> one XSI object each
walk interfaces      -> ask the edge to emit its own connection code
```

- **"Does the edge need a class of its own?" collapses into the emit method.** An `xsi_decl()` that
  returns `""` needed no class. One method, not two.
- **The crossbar is what proves edge-owned lowering over participant-owned.** One edge emits **two**
  objects sharing a third:
  ```cpp
  AxiMmReadSlave  gmem0(dut, "m_axi_gmem0", mem);
  AxiMmWriteSlave gmem1(dut, "m_axi_gmem1", mem);   // same mem
  ```
  If those were *participants*, the pysim graph and the XSI graph would have different nodes and
  "one statement, two backends" breaks on the first example. Because the **edge** owns it, one graph
  serves both.

**Why AXIS lowers to nothing and m_axi lowers to a whole FSM** — not arbitrary, and worth keeping:

- **AXIS is point-to-point and both ends exist.** TB is master, DUT is slave (or vice versa); the RTL
  implements its half of the handshake. Wires suffice.
- **m_axi has no slave in the RTL at all.** The kernel is the *master* — it raises `ARVALID` and
  expects `RDATA` back, and **nothing answers**. The TB is not connecting two peers, it is *supplying
  a missing peer*. So `AxiMmReadSlave` is not a channel, it is **the other endpoint** — which in pysim
  is `MemComponent.s_mm`, a real component's port. Hence one big FSM vs one string.

### Grounded in the generated RTL, not theory (checked 2026-07-16)

| connection | RTL |
|---|---|
| boundary AXIS (`s_cmd`) | **wires** — `input [63:0] s_cmd_TDATA;` straight into the task: `.s_cmd_TDATA(s_cmd_TDATA)` |
| internal `hls::stream` (`mr_cmd`) | **a FIFO module** — `mem_copy_fifo_w64_d2_S mr_cmd_U(...)`, real logic and depth |
| AXI interconnect / crossbar | **a real module** (decode, arbitration, routing) — but see below: the XSI TB has none |

So the two emitters differ for a *physical* reason: the kernel emitter declares
`hls_thread_local hls::stream<...> mr_cmd;` because there genuinely is an object in the hardware; the
TB emitter emits a port binding because at the pin boundary there genuinely is not. The graph already
encodes both (`StreamEdge.decl()` for internal, `_boundary_port()` for pins).

### A modelling discrepancy this surfaced — pysim and XSI model DIFFERENT systems

`AXIMMCrossBarIF` models contention: *"bus contention, occupancy timing, and duplex are properties of
the interconnect"*, with a calibrated per-direction occupancy span and a half-duplex slave that
"re-couples them onto one shared resource". **The XSI TB has no crossbar** — two independent BFM
slaves at full rate that happen to share a `std::vector`.

| | memory model |
|---|---|
| pysim (`AXIMMCrossBarIF`, 2 masters -> 1 slave) | one port, arbitrated, contended |
| XSI (two BFM slaves, one array) | two independent ports, each full rate |

Consequences, both real:
- **2835 is a KERNEL number, not a system one.** It says "this kernel *can* overlap read and write
  given independent full-rate ports" — `max(read, write)`, not `read+write`. Point both bundles at one
  arbitrated DDR and the overlap shrinks. Defensible: csynth emits two separate `m_axi` bundles, so
  the kernel really does have two ports; whether the *platform* merges them is Flow-4/IPI territory,
  and this is exactly the two-level split in [[project-two-level-calibration]] (bus = platform
  property, characterise once; only compute is per-accelerator).
- **pysim and XSI should be EXPECTED to disagree on timing**, and that is the model, not a bug.
  Nothing compares them today, so nobody would notice which is which. Worth knowing before someone
  asks "why doesn't the LT model match the RTL?".

### Corrections to this plan's own earlier claims

- **`composite_top_spec` is NOT type-gated — it is fully duck-typed** (`id(ep)` identity +
  `getattr(sub, attr)`; no `isinstance`, no `.endpoints` registry). So `SimObj` participants
  (`CmdDriver`/`WordSink`/`MemComponent`) would walk **today**, given a `bfm_model()` and named
  endpoint attrs. An earlier draft called the participant kind a blocker; it is **hygiene**
  (checkability, the taxonomy), not capability. A *should*, not a *can't*.
- **Corrected step order:** prototype the walk with duck-typing FIRST; decide the participant kind
  after, once the walk has shown what it actually needs from a participant. Same reason as Stage 1
  (`saw_b()` fell out of a witness; nobody would have designed it) and the reason `CodegenSource`
  failed — it was designed against a presumed surface and reverted.

### Deliberately deferred

- **Channel-as-a-class** (an `AxiStream` object both ends `bind()` to, mirroring the Python
  `Interface`). Rejected *for now* on one ground: **the binding is known at generate time** — the graph
  already says `driver.stream_ep -(cmd_if)-> copier.s_cmd -> boundary "s_cmd"`, so the emitter
  resolves it in Python and the C++ need not re-represent a compile-time fact at runtime (same
  argument as `render_ports_h`). It **earns itself** the moment an edge needs *behaviour*:
  instrumentation (log/count/inject backpressure), or a model->model channel (a monitor feeding a
  scoreboard) where there is no RTL between and a real queue must exist. **Cheap to reverse** — it is
  purely emitter output shape; the Python graph is identical either way.
- **Which object emits what** (do the m_axi slave models belong to the `mem` participant or the
  `xbar` edge?). Do **not** settle on paper. Write the walk for `mem_copy` and let it tell you.

### Open questions

- **What *kind* is a TB participant?** Not `FreeRunComp` — that means "lowers to an `hls::task`", and
  `AxisMaster` is not synthesizable. A new kind with `potential_targets = {xsi_bfm_model}`. This is
  precisely the `(class x target)` axis `CodegenPath.kind` was built for ([[codegen_check_family]]:
  "one component lowering to `hls::task` **and** SystemC"). **Not a blocker** — see the correction
  above.
- **How is N chosen?** A parameter works first. Better: the **LT timing model predicts cycles**, so
  `N = predicted x margin` closes a loop — the transaction-level model sizes the RTL sim, and a wild
  miss is itself a finding. Measurement survives either way: the sink records *when* words arrived, so
  time-to-completion is still observable; only the loop bound becomes fixed. Once early termination
  lands, `max_cycles` plays the same role as the timeout backstop.
- **Is a terminator a participant or a predicate?** It watches *other participants* (the sink's word
  count), not the DUT's RTL — so its "interface" is not an `Interface` in the pysim sense (a
  transactional channel). It may be a monitor over the graph rather than a node in it. Worth settling
  when the graph walk is written, not before.

## Stage 5b — the flows collapse (**consequence of the above**)

**Flow 3's premise is refuted by what Stage 1 built.** `docs/guide/flows/freerun_conc.md` justifies the
SystemC flow as lifting "the single-threaded-BFM limitation — independent streams get independent,
concurrent drivers". But the BFM is not single-threaded in the sense that matters: `s_cmd` / `s_done` /
`gmem0` / `gmem1` are **four independent agents**, each sampling its own handshakes and advancing its
own FSM every cycle. The single `for(;;)` is a **scheduler, not a serialiser**. Flow 3 was designed to
solve a limitation the implementation does not have.

The flows index already names the real axis, one line above its own table: *is the DUT control-driven
(which Vitis **can** co-simulate) or free-running (which it **cannot** — so it drops to RTL)?* That is
the whole distinction. "Sequential vs concurrent TB" was an artifact of assuming a BFM must be one
sequential program.

**So there are two flows, not four:**

| Flow | DUT | TB |
|---|---|---|
| **A** | control-driven kernel (`ap_ctrl_hs` + `s_axilite`) | sequential Vitis TB (csim / cosim) — the vendor drives it |
| **B** | free-running composite (`ap_ctrl_none`) | concurrent XSI TB — we drive it, because Vitis cannot |

(Flow 4 / bitstream is **deployment, not a TB flow** — it belongs on a different axis, not in this
table.)

**What dropping SystemC costs, stated so it is a decision and not a hope.** `SC_THREAD` lets an agent
*block* — write `send; wait; decide; send` sequentially and let a scheduler interleave. Our models
cannot block; they are FSMs. For library models that is free (written once). It bites only for a
**custom sequential agent at RTL level**, which has never come up — and pysim already has coroutines
(`yield from`) where they are ergonomic, so the loss is confined to that one case. If it ever
appears: hand-write that FSM, or add a coroutine shim then. Against that: no SystemC library, no
`xsc`/`xelab` harness, and one fewer flow to document and maintain
([[reference-systemc-xsim-windows-xsi]] proved the mechanism works — proven is not the same as needed).

**Vocabulary consequences** (`waveflow/hw/codegen_targets.py`, `docs/guide/flows/`), to do with the
Stage 5 work, not before it:
- `concurrent_systemc_tb` — **delete**.
- `sequential_xsi_tb` — **rename**; "sequential" is what mis-framed this stage. It is `xsi_tb` (single-
  threaded C++ loop, concurrent model network).
- `free_running_kernel` vs `composite_kernel` — likely **merge**: `mem_stream_gen.top_spec_for` already
  says a standalone kernel is "the 1-task degenerate case" and uses the same generator. Two names, one
  product. Worth confirming before acting.
- Deleting a target name is not cosmetic: `check()` rejects unknown names, so the vocabulary is load-
  bearing. Do it with the code, not ahead of it.

## Stage 6 — homes (deferred until after Stages 1-2, deliberately)

**Decision (2026-07-16): `mem_copy` stays in `examples/interleaver/` until Stage 1-2 are done, then
the two get pulled apart** — at which point what each actually needs is *visible* rather than guessed.
Stage 1 wants the four TBs co-located and building against one `xsi/` workspace (shared
`xsi_loader.cpp/h`, `xsi_shared_lib.h`, `run.bat`, `xsim.dir`), because extraction from all four is
what discovers the API. Moving `mem_copy`'s TB out first would fight that and invent a directory
structure Stage 1 would then rebuild.

**Done:** `composite_gen` promoted to `waveflow/build/composite_gen.py` (`7b1c617`) — it was framework
parked in an example, and `waveflow/build/hwgen.py` + `waveflow/hw/hw_composite.py` referenced it from
their docstrings (a layering inversion). This was the genuine prerequisite and is independently
valuable.

**Still shared, surfaced by attempting the move — promote as Stage 6, informed by Stages 1-2:**

- The BFM library itself (Stage 1's output) is framework.
- `mem_stream_sim`'s `CmdDriver` / `WordSink`: `mem_copy_sim.py` imports them, so the pysim helpers
  are shared infrastructure too (~30 lines, easy).
- The `xsi/` workspace: `xsi_loader.cpp/h`, `xsi_shared_lib.h`, `run.bat`, and the common `xsim.dir`.
  All four TBs build from it. This is the one Stage 1 restructures.
- Hand-written hook `.cpp` bodies have no home: `gen/` is regenerated and would clobber them. This
  blocks *adopting* the generated task body, independent of anything in this plan.
- Rename scheme (`project-example-rename-scheme`): `mem_copy` vs `memcpy`.

## Depends on

- Nothing in Stages 1-2 — they are a refactor of code that already works.
- Stage 3 needs `TopSpec` reachable from the TB generator (Stage 6's promotion makes this clean, but
  is not required to prototype).
- Stage 5 needs Stages 1-4.

## Not in scope

- **Generating AXI protocol code.** The anti-goal of this plan. See Stage 0.
- Vitis C/RTL cosim: it refuses `ap_ctrl_none`. XSI is the only RTL path for a free-running DUT.
- SystemC / `concurrent_systemc_tb` (Flow 3). The BFM library is plausibly shared with it; do not
  design for that until Flow 2 works.
- `free_running_kernel` (a leaf as its own top). Different target; not needed for composites.

## Verification

- **Stages 1-2:** all four XSI TBs PASS with identical cycle counts, from a regenerated `.f`. This is
  the whole safety net — four working witnesses held bit-exact through a refactor.
- **Stage 3:** every derived port name resolves via `get_port_number`; a deliberately wrong name must
  fail loudly rather than silently skip (the current `if (p >= 0)` zero-pinning idiom hides typos).
- **Stages 4-5:** a generated TB reproduces the hand-written TB's result on `mem_copy` — same jobs,
  same cycle count, same PASS. `mem_copy` is the right first target: it has both an m_axi read and an
  m_axi write bundle plus two AXIS ports, so it exercises every BFM piece, and it already has a
  working TB to diff against.

## Notes carried in

- **csynth OK is not evidence of correctness.** Nested-struct-by-value silently DCEs a kernel while
  csynth reports success (`reference-hls-hook-csynth-gotchas`). Every claim in this plan is gated on
  XSI, not csynth.
- The generated task body (`e833a13`) is csynth-verified equivalent to the hand-written one — same
  RTL module set, identical latency/II/LUT/FF — but has **never been run**. XSI via this plan is what
  would prove it functionally.
