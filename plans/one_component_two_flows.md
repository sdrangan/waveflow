# Plan: one synthesizable component, two flows

## The proposal (user's, 2026-07-17)

There is no real distinction between a leaf and a composite. The flow is:

1. **DUT C++.** A component with a body -> free-running Vitis kernel written *from the body*. A
   component with sub-components -> free-running Vitis kernel written *from the structure*
   (sub-components called via `hls::task`, no body of its own).
2. **DUT synthesis.** The top from (1) runs through csynth -> a Verilog IP package.
3. **TB components.** A component -> an XSI class with `update()` methods expressing its behavior on
   its ports; sub-components become XSI class instantiations.
4. **TB top.** A component -> the XSI `main()`: instantiate the TB sub-classes, instantiate an XSI
   wrapper around each synthesized IP, add the sim framework and clock driver.

So merge `FreeRunComp` and `CompositeComp` into one class, with "no sub-components" as a special
case. That leaves **three components and two flows**:

- **Flow 1** — standalone host-activated Vitis kernel + sequential TB: `HostActivated` + `SeqTB`.
- **Flow 2** — concurrent Vitis kernel + concurrent XSI TB: the merged component, for both DUT and TB.

## Reality check: what of this exists

Steps 1, 2 and 4 match the code. **Step 3 does not exist at all** and is out of scope here (below).

`composite_gen.py` already generates step 4 for `mem_copy`: `tb_top_spec` walks the TB graph and
`render_tb_harness` emits the `main()`. Step 1 is `composite_top_spec` + `render_top`, and after
`types_not_tags` Stage 3 a leaf already walks through *that same generator* as the 1-task case.

## What blocks the merge: almost nothing

`FreeRunComp` vs `CompositeComp`, after Stage 3, differ in exactly one thing — **does it have a
body** — plus three fields that turn out not to matter:

| | `FreeRunComp` | `CompositeComp` | merged |
|---|---|---|---|
| body | `run_iter` (abstract) | forbidden (`__init_subclass__`) | present XOR children |
| `run_proc` | `while True: run_iter()` | none (passive; children own processes) | follows the body |
| `boundary` etc. | derived (Stage 3) | declared | unchanged — same walk |
| `control_mode` | `FREE_RUNNING` | `AUTO` | **delete: nothing reads it** |
| `potential_targets` | `{free_running_kernel}` | `{composite_kernel}` | `{composite_kernel, sequential_xsi_tb}` |

**The cost, stated honestly.** `CompositeComp`'s "must not define `run_iter`" is a *class* fact
today, enforced at class-definition time by `__init_subclass__`. Merged, "body XOR children" is an
*instance* fact — children arrive in `__post_init__`, so the check moves to after construction.
Later and weaker. It is the only thing the merge gives up, and it is worth it.

## Two findings that make the merge cheaper than it looks

**`control_mode` is dead.** Nothing reads it. `codegen_check`'s docstring claims `build/` reads
`cpp_kernel_name` / `control_mode` / `_is_testbench`; the function reads only `potential_targets`,
and `composite_gen` emits `ap_ctrl_none` unconditionally (`render_top`). The only readers are tests
asserting the declarations. So `FREE_RUNNING` vs `AUTO` — which looked like the question the merge
must answer — costs nothing, because it is the same shape `types_not_tags` just spent four stages
deleting: **a declared fact nothing dispatches on**.

`CompositeComp`'s docstring says "a composite's control mode *follows its boundary* — a regmap on the
boundary makes the top host-activated (`ap_ctrl_hs` + `s_axilite`)". **That mechanism does not
exist.** Same pathology as `hw_freerun`'s "state on self -> static locals" and `MemRStream`'s "bound
read": a docstring describing a design that was never wired.

**`MemCopyTB.potential_targets == {'composite_kernel'}`** — inherited from `CompositeComp`, and
wrong. A testbench does not lower to a synthesizable kernel; it lowers to `sequential_xsi_tb`. Not
a lie about *what it is* — an incomplete list.

## The target is the caller's choice, not a property of the source

*(This corrects a wrong turn: the first draft proposed deriving the target from the graph's contents
— "a graph containing `bfm_model()` nodes is a TB". That is inference, one message after four stages
of deleting inference. It is also less expressive than the truth.)*

**A single class can have several targets. Which one is decided by the codegen step you run, and
that step confirms it has what it needs.** The same component can legitimately be a DUT in one test
and a TB model in another, depending on how much of the system that test synthesizes.

`check(source, target)` is already exactly this:

1. unknown target -> typo
2. not a potential target for the kind -> the ClassVar declares which paths *exist*
3. declared but not implemented
4. **the rules** — run the REAL extraction, convert its raise into a verdict

So the merged class declares `potential_targets = {composite_kernel, sequential_xsi_tb}` and both
stay open. `MemCopyTB` asking for `composite_kernel` fails at **gate 4**, because its participants
have no body — not because anything inferred what it "is". `_resolve_target` already handles the
consequence: with several potential targets the caller must name one, and it already says so.

**The actual gap: gate 4 knows one generator.** It calls `extract_kernel` / `extract_testbench` (the
poly-style extractor). The composite path has no extraction — the top comes from the graph walk. So
gate 4 must dispatch on the requested target and run *that target's real generator*, discarding the
output:

- `composite_kernel` -> `composite_top_spec` + the task-body generator
- `sequential_xsi_tb` -> `tb_top_spec` + `render_tb_harness`
- `control_driven_kernel` / `sequential_vitis_tb` -> today's extractors (unchanged)

The discipline carries over verbatim, and it is the whole reason this is safe: **do not add a rule to
gate 4.** It must run the same code `generate` runs. A mirror would answer for rules that no longer
exist and miss rules that do.

## Vocabulary (this is what makes Stage 5 fall out instead of being a decision)

`free_running_kernel` and `composite_kernel` are one product with N=1 -> one name,
`composite_kernel`. `concurrent_systemc_tb` goes (Flow 3 was refuted — see
`plans/xsi_tb_codegen.md`). `ALL_TARGETS` goes 7 -> 5:

| Flow | DUT target | TB target |
|---|---|---|
| 1 · control-driven | `control_driven_kernel` | `sequential_vitis_tb` |
| 2 · concurrent | `composite_kernel` | `sequential_xsi_tb` |
| 4 · hardware | `bitstream` | — (host software) |

`docs/guide/flows/index.md` shares this vocabulary verbatim and **must change in the same commit**.

## Stages

Gated exactly as `types_not_tags` was, which is what makes it safe: **all four tops
(`mem_r_stream`, `mem_w_stream`, `mem_copy`, `interleaver_canon`) plus every generated header/`.tcl`
must come out byte-identical**; `-m xsi` stays at 8 passed; fast loop at the 6-failure baseline. A
refactor whose output moves is wrong.

**Stage 1 — delete `control_mode`.** Dead, and it is the field that would otherwise force a decision
during the merge. Drop the enum, the three declarations, the tests that assert them, and fix the two
docstrings that describe the mechanism it never had. *Independent of the merge; do it first, alone.*

**Stage 2 — merge the classes.** One class (name TBD: `HwComp`? keep `FreeRunComp`?). Body XOR
children as a post-construction invariant with the message `CompositeComp.__init_subclass__` has now.
`run_proc` follows the body. `CompositeComp` and `FreeRunComp` become aliases for one deprecation
cycle, or are deleted outright — solo repo, so probably deleted.

**Stage 3 — gate 4 dispatches on target.** `_check_extracts` -> per-target generator, output
discarded. This is what makes "the codegen step confirms it has what it needs" true rather than
aspirational, and it is what lets one class carry two targets safely.

**Stage 4 — vocabulary.** Collapse `free_running_kernel` into `composite_kernel`, delete
`concurrent_systemc_tb`, fix `MemCopyTB`'s targets, update `docs/guide/flows/index.md` in the same
commit.

## Not in scope

**Step 3 of the proposal — generating BFM classes from Python bodies.** Today `bfm_model()` *names* a
hand-written class from `waveflow/build/xsi/xsi_bfm.h` (369 lines: `Dut`, `FlatMemory`,
`AxiMmReadSlave`, `AxiMmWriteSlave`, `AxisMaster`, `AxisSlave`, `XsiSim` — every `update()` method and
the whole AXI protocol, written once per protocol) and says what to pass it. Nothing generates an XSI
class from a component's behavior.

That is a much larger piece, and the merge does not depend on it: the merged class keeps naming
library classes exactly as `bfm_model()` does now. Worth its own plan. The interesting question it
raises — whether a TB participant should be a `Component` at all, rather than a `SimObj` — is already
open in `stream_tb.py`'s docstring and `plans/xsi_tb_codegen.md`.

**The leaf XSI harness for `mem_r`/`mem_w`.** Blocked by `mem_stream_sim.py` building its harnesses
inside `run_read`/`run_write` *functions* rather than as a TB component graph — a function body is
code; only a graph is data, and only data can be walked. Orthogonal to the merge.
