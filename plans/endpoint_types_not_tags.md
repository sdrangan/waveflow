# Plan: lowering is a function of the endpoint's TYPE, not a tag beside it

## Context

Codegen needs one fact it cannot get from an endpoint: **which direction an `m_axi` master uses**.
`MemRStream.m_mem` and `MemWStream.m_mem` are the *same class* — plain `MMIFMaster` — yet lower
differently:

```cpp
const ap_uint<64>* m_mem      // MemRStream: read-only -> const + #pragma HLS stable, AR/R only
      ap_uint<64>* m_mem      // MemWStream: written   -> plain pointer, AW/W/B
```

Direction is not a property of the endpoint's *type*; it is a property of **how the component uses
it**. The component knows (its `run_iter` only ever calls read methods on `m_mem`). Nothing writes
that down where codegen can see it. So the fact gets supplied from the side — **three different
times, three different ways**:

| symptom | where | what it restates |
|---|---|---|
| `top_spec_for`'s hardcoded table | `mem_stream_gen.py` | `if comp_class is MemRStream: _maxi_port(..., const=True)` |
| `boundary`'s `kind` string | every `CompositeComp` | `("m_in", self.rstream.m_mem, "maxi_read", "gmem0")` |
| `as_dir('R')` / `@port_read` | `interface.py` | a capability view that narrows an endpoint at bind time |

**These are one bug, not three.** Each is a side-channel for a missing declaration.

## The decision: types, not tags

The endpoint's **type** declares its direction; codegen dispatches on the type; nothing infers,
sniffs, or is told separately.

```
MMIFReadMaster   -> maxi_read    const T* + #pragma HLS stable, AR/R only
MMIFWriteMaster  -> maxi_write   T*, AW/W/B
MMIFMaster (R+W) -> legal, just unspecialised: T*, all channels
```

**This is a decision this codebase has already made twice**, and `as_dir` is inconsistent with it:

- `hw_freerun.py`: *"Because the execution model is **declared by the class**
  (`control_mode = FREE_RUNNING`, `_kernel_method = 'run_iter'`), codegen never has to **infer**
  free-running-ness by finding a `while` loop at the extracted root."*
- `codegen_check_family`: *"class states the **kind**, `check()` states the **fitness**."*

A tag is invisible to `check()`. A type is not.

**The evidence that `as_dir` is the wrong shape:** it has **no production users**. `git grep as_dir`
finds only `tests/hw/test_endpoint_capability.py`. Meanwhile `MemRStream`'s docstring *claims*
`m_mem` is **"bound read"** and its comment says *"bound 'R' … the generated pointer is const (the
`@port_read` capability)"*. The author wanted it, documented it as done, and did not do it. A
mechanism built, documented, tested, and then declined by the person who most wanted it is a design
smell — not an oversight. (Compare `hw_freerun`'s "state on self -> static locals": also documented,
also never wired.)

**The generalisation is the real argument** (user's): with a tag mechanism you add a tag per protocol
nuance, forever — today `as_dir` for AXI direction, tomorrow something else for the next endpoint too
general to lower. With types you add a type, and it either has a lowering or it does not.

**Correction to a tempting framing:** this is *not* "cannot generate, so disallow". A read+write
`m_axi` lowers fine (non-`const`, all channels). It is "**the type under-specifies, so the lowering
must come from somewhere else**" — which is the same disease, milder, and the same cure.

## What falls out (why this is worth more than it looks)

1. **`boundary` shrinks or disappears.** It currently states four things; three are already knowable:
   *name* (the attribute), *endpoint* (the value), *kind* (the type, after this change). Only
   **bundle** remains — and that is genuinely not a property of the endpoint (see below).
2. **`top_spec_for`'s table dies.** A leaf can then declare its own ports, and
   `composite_top_spec` walks it — **proven**: giving `MemRStream` a `boundary` +
   `ordered_subcomps=[self]` + `internal_edges=[]` produces a `TopSpec` *identical* to the
   hand-written one (ports, pragmas and tasks all match).
3. **`free_running_kernel` merges into `composite_kernel`.** `top_spec_for`'s own docstring calls a
   standalone kernel *"the 1-task degenerate case"* — it cannot **be** that case while its ports are
   hand-listed. One product, one name (`plans/xsi_tb_codegen.md` Stage 5b).
4. **`tb_top_spec` walks a leaf DUT**, so `mem_r`/`mem_w` can get generated XSI harnesses.
5. **A stray write becomes an `AttributeError` because the method is not there** — which is what
   `as_dir` was faking dynamically.

**One fix, four payoffs.** They are all the same fix.

## Bundle: the one thing that is NOT the endpoint's

`gmem0`/`gmem1` is an **allocation decision by the assembler**, not a fact about the port.
`MemWStream.m_mem` is `gmem0` standalone and `gmem1` inside `MemCopy` — same endpoint, different
bundle. So either the parent assigns bundles (policy: distinct `m_axi` ports get `gmem0`, `gmem1`, …
in declaration order — which is exactly what today's hand-written values already are), or the
endpoint carries a default the parent may override. **Prefer the policy**: it deletes the last
declaration instead of relocating it, and it cannot disagree with itself.

## Status — Stages 1-4 DONE (91b240e, 7ca93d5, ecafaf3)

Every stage held its gate: all four tops (`mem_r_stream`, `mem_w_stream`, `mem_copy`,
`interleaver_canon`) and every generated header/`.tcl` come out **byte-identical**, `-m xsi` stays at
8 passed, fast loop at the 6-failure baseline. A boundary entry went
`(name, ep, "maxi_read", "gmem0")` -> `(name, ep)`, and `FreeRunComp` now declares nothing at all.

All four predicted payoffs landed. Two things the work itself corrected:

**`@port_read`/`@port_write` are NOT deletable — Stage 1 claimed them.** The plan below assumed
nothing used the tags. That was true when written and false three commits later:
`_DirectionalMMIFMaster` refuses a wrong-direction call by asking `_classify_port_dir`, which reads
exactly those tags. So the tags are the *mechanism the types are built on*, not a rival to them.
That is a better outcome than deleting them — the type is what codegen dispatches on, the tags are
how the type enforces itself, and there is one place saying which methods read and which write.

**`as_dir`/`CapabilityView` are still unclaimed** — test-only, no production caller, in both
`interface.py` and `memif.py`. Which is the evidence the plan wanted. But per *Not in scope* below,
delete only on confirmation: a type cannot express per-binding narrowing, so if that is ever wanted
`as_dir` is the shape for it. Nothing wants it today.

### Stage 5 — vocabulary (NOT done; needs a decision)

Merging `free_running_kernel` into `composite_kernel` is now *justified by the code*: after Stage 3 a
leaf walks through the same generator as a composite, so they are one product with N=1, not two. But
`check()` rejects unknown target names, so the vocabulary is load-bearing and collapsing a declared
name is a decision, not a refactor. Left for review.

The other predicted payoff, also unbuilt: `tb_top_spec` can now walk a **leaf** DUT, so `mem_r_stream`
and `mem_w_stream` could get generated XSI harnesses like `mem_copy`'s. What blocks it is not the
walk — it is that `mem_stream_sim.py` builds its harnesses inside `run_read`/`run_write` functions
rather than as a `CompositeComp` TB class, so there is no graph to walk. Restructuring that file is
the actual task, and it is a choice about that file, not a consequence of this plan.

## Stages (as planned)

Each gated the same way, which is what makes this safe: **the generated `.cpp` must come out
byte-identical**. This is a refactor; if the output moves, the change is wrong.

**Stage 1 — the types.** `MMIFReadMaster` / `MMIFWriteMaster`. `MemRStream`/`MemWStream` construct
them. Codegen derives `const` from the type instead of the caller's `const=` argument.
*Gate:* `gen/{mem_r_stream,mem_w_stream,mem_copy,interleaver_canon}.cpp` byte-identical.

**Stage 2 — derive `kind`.** `_boundary_port` reads the endpoint's type; `boundary` entries drop
their `kind` string. *Gate:* same, plus `-m xsi`.

**Stage 3 — the leaf declares.** `MemRStream`/`MemWStream` declare `boundary` (or it derives from
their endpoints + `kernel_task().signature` order); `top_spec_for`'s table deletes itself.
*Gate:* same.

**Stage 4 — bundle policy.** Parent assigns in declaration order; the last hand-written field goes.
*Gate:* same.

**Stage 5 — vocabulary.** Merge `free_running_kernel` into `composite_kernel`; delete `as_dir` /
`CapabilityView` / `@port_read` / `@port_write` if nothing has claimed them by then (nothing does
today). *Do this WITH the code, not ahead of it* — `check()` rejects unknown target names, so the
vocabulary is load-bearing.

## The wrinkle, stated honestly

`MMIFReadMaster` having **fewer** methods than `MMIFMaster` is not an *is-a* — it is not
substitutable, so it is not really a subclass. The clean shape inverts the hierarchy: a minimal base
carrying what both share, with read/write masters extending it. That is an `interface.py` refactor,
not a codegen change, and it is the honest reason to scope this deliberately rather than fold it into
the XSI work.

**The pragmatic first cut** is subclasses that *declare* (so codegen dispatches on type and
`isinstance(ep, MMIFMaster)` still holds everywhere downstream), with the wrong-direction methods
raising. That gets all four payoffs. The hierarchy inversion is worth doing when something other than
codegen wants it.

## Not in scope

- `as_dir`'s *other* possible use — narrowing one endpoint differently **per binding**. No caller
  wants this today, and if one ever does, a type cannot express it. Delete `as_dir` only when that is
  confirmed, not assumed.
- `StreamIFMaster`/`StreamIFSlave` already carry their direction in the type. They are the model this
  follows; nothing to do.
