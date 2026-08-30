# Interface docs and naming — one map, one vocabulary

Two problems that have to be solved together, because fixing either one alone means writing the same
pages twice.

1. **Docs are filed along three axes at once**, so reading along any one of them finds holes that are
   really material on another.
2. **The access vocabulary disagrees with itself** — three spellings of "pipelined read", two verbs
   for an addressed read, two names for a reference.

The naming goes first.  Documenting names you are about to change is writing them twice, which is
the argument that (correctly) deferred `bram_access/python.md` until after the typed-`BramIF` work.

---

## Part 1 — the survey, measured

### The three axes now in play

| location | organized by | audience | size |
|---|---|---|---|
| `guide/interface/` (13 pages) | **interface type** | 100% `python` | ~3,750 lines |
| `guide/comp_codegen/` (15 pages) | **codegen concern** | 100% `hls` | ~2,330 lines |
| `guide/custom_hooks/` (8 pages) | **hook type** | 100% `hls` | ~970 lines |
| `guide/schema/{python,hls}` | **layer** | mixed | — |
| `guide/vectorization/{python,hls}` | **layer** | mixed | — |

Every page in `guide/interface/` is tagged `audience: python` — **all thirteen**.  The HLS side is
not missing; it is in `comp_codegen/interface.md` (164 lines), `xsi_tb.md` (218), and
`custom_hooks/`.  That is why reading along the type axis finds a hole that is not there.

### Why NOT to re-folder `guide/interface/` into `python/ hls/ bfm/`

Considered and rejected.  Recorded so it is not re-proposed:

- **It duplicates the arc split rather than replacing it.**  The guide's reading order is already a
  layer progression: `schema -> vectorization -> sim -> interface -> flows -> comp_codegen ->
  custom_hooks`.  An `interface/hls/` folder would compete with `comp_codegen/interface.md` for the
  same content, and one of the two would rot.
- **The material will not fill it.**  3,750 Python lines against ~330 lines of HLS-side interface
  material outside `comp_codegen`.  The `hls/` folder would hold a third of one page.
- **It splits each interface's story across three files.**  A reader asking *"how do I use a
  stream?"* wants one page.  The reader asking *"how does lowering work?"* is already served by
  `comp_codegen`.

**The real defect is cross-axis discoverability.**  `guide/interface/stream.md` is 288 lines about
`StreamIF` in Python that never mentions `comp_codegen/interface.md` exists.  Same failure as the
deleted `bram_access/overview.md`, one level up: material stranded because the page that owns the
context does not link to it.

---

## Part 2 — the interface map: where each layer's docs live

**Legend:** ✅ exists · ➕ to write · ➖ deliberately none · 🔗 link stub to add

| interface | Python | HLS lowering | BFM / XSI |
|---|---|---|---|
| `StreamIF` | ✅ `interface/stream.md` | ✅ `comp_codegen/interface.md`, `freerunning*.md`, `custom_hooks/stream.md` | ✅ `comp_codegen/xsi_tb.md` |
| `MMIF` / `DirectMMIF` / `AXIMMCrossBarIF` | ✅ `interface/aximm.md`, `crossbar.md`, `mmqueue.md`, `poll.md` | ✅ `comp_codegen/interface.md`, `hostactivated.md` | 🔗 stub |
| `BramIF` | ✅ `interface/bram.md` | ✅ `comp_codegen/rtl_module.md` | 🔗 stub — the wrapper *is* the BFM seam |
| `StreamOfBlocksIF` | ✅ `interface/sob.md` | ⚠️ only `freerunning_composite.md` — ➕ needs its own section | ➖ |
| `RegMapMMIFSlave` | ✅ `interface/regmap.md` (759 lines) | ✅ `comp_codegen/extractor.md`, `hostactivated.md`, `interface.md` | ➖ refused at the ap_ctrl_none boundary |
| `SchemaTransferIF` / `ArrayTransferIF` | ✅ `interface/schema_transfer.md`, `array_transfer.md` | ➕ **missing** | ➖ |
| **`CreditStreamIF`** | ❌ **NO DOCS ANYWHERE** | ❌ | ❌ |
| **`AckedStreamIF`** | ❌ **NO DOCS ANYWHERE** | ❌ | ❌ |
| `RFSampIF` | ✅ `guide/rf/rfdc/*` | ✅ same | ✅ same |
| `CrossBarIF` (queued) | ⚠️ thin — `interface/crossbar.md` is 65 lines | ➖ | ➖ |

### What the map exposes

- **`CreditStreamIF` and `AckedStreamIF` have zero documentation** — `waveflow/hw/reverse_stream.py`
  shipped undocumented.  Biggest single gap.
- **`SchemaTransferIF` / `ArrayTransferIF` have 814 lines of Python docs and no lowering story.**
- **`RFSampIF` is correctly specialized out** into `guide/rf/`.  That is the pattern for
  domain-specific interfaces and should stay.
- **`behavioral.md`** (203 lines, 19 XSI/BFM references) is the BFM page misfiled as an interface
  *type*.  Either move it beside `custom_hooks/bfm_model.md`, or keep it and state that it is the
  bridge page.

### The link-stub convention (the cheap fix, do it everywhere)

Every `guide/interface/*.md` gets a short **"How it lowers"** section — three or four lines and the
links, never a restatement.  An example states its case and links to the principle; a Python page
states the model and links to the lowering.  Restating is what produced context-free arcana in
`bram_access/overview.md`.

Also: **move `guide/schema/hls/serialization.md`** (104 lines).  It lands at reading position 2,
before interfaces exist at position 5, but it is about how a schema reaches a *stream*.  It belongs
beside `comp_codegen/interface.md`.

---

## Part 2b — one dispatch, two backends (and why `comp_codegen` does NOT split)

A correction to a natural but wrong mental model: *"HLS lowers endpoints automatically, BFM models
are hand-written."*  **Both are automatic, and they are the same dispatch.**

`kind_of_endpoint(ep)` produces one vocabulary — `axis_in`, `axis_out`, `maxi_read`, `maxi_write`,
`mm_slave`, `axilite_slave`, `bram` — and **two tables consume it**: the boundary-port emitter (HLS)
and `BFM_DUALS` (XSI).  `composite_gen.py` says so at the table:

> *"Keys are `kind_of_endpoint`'s vocabulary, so this table and the boundary-port lowering cannot
> disagree."*

What *is* hand-authored on the XSI side is the **testbench graph** — which `StreamDriver`s exist and
what bundles they play — not the per-port model.  That is the distinction worth teaching, and it is
not the one a reader assumes.

`BFM_DUALS` also records its own holes as rows rather than in prose, which is worth copying
elsewhere:

- `mm_slave` -> no model, none planned (the kernel is always the master in this flow)
- `axilite_slave` -> **no model: a regmap / `HostActivated` DUT cannot be XSI-lowered at all today**
- `bram` -> **absent from the table entirely**, because a BRAM port's counterpart is the *wrapper*,
  not a BFM

### Rejected: splitting `comp_codegen` into `hls/` and `xsi/`

- **The numbers do not support it.**  Of 15 pages, exactly one is substantially XSI (`xsi_tb.md`,
  36 hits / 218 lines).  The rest have 1–8 passing mentions, and `rtl_module.md`'s 8 are about the
  wrapper seam, which belongs with the codegen it wraps.
- **It cuts the wrong joint.**  `kind_of_endpoint` is *shared*.  An `hls/` vs `xsi/` split implies
  two independent pipelines and would make the single dispatch harder to see, not easier.

**What the section actually needs is one page that does not exist**: *the endpoint-kind vocabulary,
and the two tables that consume it.*  That is worth more than any re-foldering.

## Part 2c — present interfaces in four tiers, not one list

The overview page is overwhelming because it lists thirteen interfaces flat.  There is a natural
order, and **`kind_of_endpoint` is the test** rather than a matter of taste:

| tier | test | members |
|---|---|---|
| **Primitive, boundary** | has a `kind_of_endpoint` kind | `StreamIF`, `MMIF` (+ `RegMap`), `BramIF` |
| **Primitive, internal** | lowers to a real HLS construct, never a boundary port | `StreamOfBlocksIF` (-> `hls::stream_of_blocks`), `CrossBarIF` |
| **Derived** | transactions over primitives | `CreditStreamIF`, `AckedStreamIF`, `SchemaTransferIF`, `ArrayTransferIF` |
| **Simulation-only** | no lowering at all | `RFSampIF` |

The source already knows this — `derive_internal_edges` says *"an `AckedStreamIF` is two FIFOs that a
module wants to talk about as one thing."*  That is the definition of *derived*, written down but
never surfaced.

**The middle tier is the one that earns its place.**  Without it `StreamOfBlocksIF` looks derived (it
has no boundary kind) when it is really a primitive that only exists inside a kernel — which is
exactly why its docs are thin and stranded in `freerunning_composite.md`.

## Part 2d — the target structure for `guide/interface/`

Four of the thirteen pages are **not interface types at all**, which is why a flat list of "the
interfaces" never quite worked:

| page | what it actually is |
|---|---|
| `mmqueue.md` | `AXIMMQueue` — a ring-buffer **protocol** over `MMIFMaster`. **Derived**, by Part 2c's own test |
| `poll.md` | a **timing model** — `poll_until`, the bandwidth-steal derating. Not an interface |
| `behavioral.md` | an **authoring guide** for behavioral edges. Not an interface |
| `crossbar.md` | genuinely an interface (`CrossBarIF`) — primitive-**internal**, like `sob` |

### Target layout

```
guide/interface/
  index.md              "Interfaces" — the tiered map, the ONE summary table
  primitive/
    index.md            "Primitive interfaces" — has a real HLS lowering
    stream.md           axis_in / axis_out
    aximm.md            maxi_read / maxi_write / mm_slave
    bram.md             bram
    regmap.md           axilite_slave  (a specialization of MMIF, but its own kind)
    sob.md              INTERNAL -- hls::stream_of_blocks
    crossbar.md         INTERNAL -- the n x m fabric
  derived/
    index.md            "Derived interfaces" — transactions over primitives
    schema_transfer.md
    array_transfer.md
    mmqueue.md          <- moved in from the top level
    credit_stream.md    NEW
    acked_stream.md     NEW
```

**Two pages leave the section entirely:**

- `poll.md` -> `guide/timing/`.  It is a timing model that happens to be reached through
  `MMIFMaster`; filing it under Interfaces is what made the section look like it covered
  everything m_axi.
- `behavioral.md` -> `guide/custom_hooks/`, beside `bfm_model.md`.  Already flagged in Part 2.

### Three decisions behind the shape

- **The tier table lives in `index.md`, not a separate `primitive_overview.md`.**  `index.md` is
  already the landing page and Just the Docs generates its TOC from front matter.  A second overview
  page would duplicate it and drift — which is precisely what `bram_access/overview.md` did before it
  was deleted.
- **Boundary vs internal is a COLUMN, not a folder.**  `sob` and `crossbar` are primitives that only
  exist inside a kernel.  That matters when reading about lowering; it does not justify a third
  folder.  One column in the index table carries it.
- **Subfolders are safe, because the precedent works** — `schema/{python,hls}` and
  `vectorization/{python,hls}` already nest this deep.

### The nav constraint, learned the hard way

Just the Docs resolves `parent:` by **matching the title string**, and this repo has already shipped
one collision (two pages titled `Overview`, seven children binding to the wrong one).  So:

- the two new section titles must be **globally unique**: `Primitive interfaces` and
  `Derived interfaces` — **never** bare `Primitive` / `Derived`
- every child needs `grand_parent: Interfaces`
- **re-run the duplicate-title audit afterwards.**  Any title used as a `parent:` value whose
  children lack `grand_parent` is a latent mis-binding, and the symptom only appears when the set of
  candidates changes.

### One judgment call to settle BEFORE the move

`regmap.md` is 759 lines, the largest page in the section, and it is arguably more about the **host
launch lifecycle** (`ap_start` / `ap_done`, `BoundRegMap`) than about an interface.  Filing it under
`primitive/` is defensible — `axilite_slave` is a real boundary kind — but if the page is really
*"how a host drives a kernel"*, it belongs near `comp_codegen/hostactivated.md` instead.  Decide
before moving it, not after.


---

## Part 3 — the naming convergence

### Measured inventory (public access methods, noise removed)

```
StreamIFSlave       drain, get, get_nb, get_pipelined, pop*, pop_array*
StreamIFMaster      offer, push*, push_array*, write, write_pipelined
MMIFMaster          read, read_array, read_array_anchored, read_array_pipelined, read_schema,
                    read_spanned, write, write_array, write_array_pipelined, write_schema,
                    write_spanned, poll_until
MMIFSlave           (none — behavioral, via rx_write_proc / rx_read_proc)
BramIFMaster        array_ref, ii_for, mem_read, mem_write, read_pipelined, write_pipelined
SobIFMaster         acquire_write, commit_write
SobIFSlave          acquire_read, release_read
CreditStreamMasterIF   poll_credit, write_nb, write_resp_nb
CreditStreamSlaveIF    get, offer_credit
AckedStreamMasterIF    assert_clean, can_write_frame, harvest, write_frame
AckedStreamSlaveIF     read_frame_nb, read_nb, send_status
SchemaTransferIFMaster / ArrayTransferIFMaster   write

* pop / pop_array / push / push_array all raise NotImplementedError — codegen-only in v1.
```

### The renames

**R1 — one spelling for a pipelined transfer.  Three exist; keep one.**

| now | becomes | sites |
|---|---|---|
| `StreamIFSlave.get_pipelined` | `get_pipelined` *(unchanged)* | 10 |
| `MMIFMaster.read_array_pipelined` | `read_pipelined` | 1 |
| `BramIFMaster.read_pipelined` | `read_pipelined` *(unchanged)* | 5 |
| `MMIFMaster.write_array_pipelined` | `write_pipelined` | — |
| `StreamIFMaster` / `BramIFMaster.write_pipelined` | *(unchanged)* | 9 |

The `_array_` infix carried no information — every pipelined transfer moves an array.

**R2 — an addressed read is `read`, everywhere.**

| now | becomes |
|---|---|
| `BramIFMaster.mem_read` / `mem_write` | `read` / `write` |
| `MMIFMaster.read` / `write` | *(unchanged)* |

10 sites.  The `mem_` prefix distinguished nothing: a `BramIFMaster` only ever addresses memory.

**R3 — one name for a Case 3 reference.**

| now | becomes |
|---|---|
| `BramIFMaster.array_ref` | `array_ref` *(unchanged)* |
| `_DirectBackedMMIFMaster.as_words` / `as_array` / `as_schema` | `array_ref` / removed |

**This resolves a live defect by construction.**  `as_words()` returns a genuine numpy view, but
`as_array()` goes through `arrayutils.read_array`, which builds a fresh object — so an `as_*` method
silently degrades to a copy and writes reach nothing.  Under `array_ref`'s rule (*a view for every
element type, or a declaration-time refusal*) that cannot happen.  Currently **zero real callers**,
so this is latent, not live — fix it before someone finds it.

**R4 — delete the dead surface.**  `pop`, `pop_array`, `push`, `push_array` are public and all four
raise `NotImplementedError`.  Live-looking API that cannot be called, sitting next to the names this
plan is making legible.

**R5 — one suffix for non-blocking.**  `get_nb` / `read_nb` / `write_nb` use `_nb`; `offer` and
`can_write_frame` do not.  Converge on `_nb`, **except** `offer`, which should keep its name and gain
a docstring saying why: it is for a producer that *physically cannot wait* (a converter), whereas
`get_nb` is for a consumer that *must not* wait.  Different reasons, and the asymmetry is real.

### What must NOT change, and why it must be SAID

**`get` on a stream stays `get`.  It does not become `read`.**

A stream read is a **destructive dequeue**; an addressed read is not.  That distinction is real and
the naming currently earns it — but nothing says so, which is why it reads as accidental.  So this is
a *docs* item, not a rename: `guide/interface/overview.md` states the rule as
deliberate.

Applies equally to `SobIFSlave.acquire_read` / `release_read` and `CreditStreamSlaveIF.get`: an
acquire is a lease, a get is a dequeue, a read is an addressed look.  Three verbs, three meanings —
worth a short vocabulary table on the overview page.

---

---

## Part 4 — endpoints should own their boundary kind

### The problem

`kind_of_endpoint` is an 8-branch `isinstance` chain in `waveflow/build/composite_gen.py`, and the
kind then escapes as a bare string that is re-tested seven more times:

```
composite_gen.py:425   p.kind == "bram"
composite_gen.py:435   p.kind == "maxi_read"
composite_gen.py:463   ch.kind == "sob"
composite_gen.py:876   kind_of_endpoint(ep) in ("maxi_read", "maxi_write")
composite_gen.py:906   kind == "maxi_read"
composite_gen.py:907   kind == "bram"
composite_gen.py:2103  kind_of_endpoint(dep) == "bram"
codegen_dispatch.py:79 isinstance(ep, VitisRegMapMMIFSlave)
```

Adding an endpoint type means touching a chain in another package.  The infrastructure has to know
every specialized type.

### The concrete bug this design is exposed to

**The `isinstance` chain has a silent ordering dependency.**  `RegMapMMIFSlave` must be tested before
`MMIFSlave`, and `MMIFReadMaster` before `MMIFMaster` — subclass before base.  Reorder those lines
and there is no error: an `axilite_slave` quietly lowers as `mm_slave`.  A class attribute has no
such hazard because inheritance resolves it.

### A rejected objection, tested and refuted

It was argued that `axis_in` is *Vitis vocabulary*, so putting it on an endpoint would make
`waveflow/hw/` know about a backend.  **That argument is wrong**, and it was checked rather than
debated:

- **`hw/` already imports from `build/`** — six modules, including `bram.py:154-155` pulling
  `LoweringError` and `_bram_addr_shift` out of `waveflow.build.*`.  There is no layer to invert.
- **`hw/` already encodes lowering-only distinctions, with a class-level tag.**  `MMIFReadMaster`
  exists for no other purpose than to make codegen emit `const T*` (its docstring says so), and it
  already carries `port_dir: ClassVar[str] = 'R'`.  The proposed pattern is *already in the tree*;
  `kind_of_endpoint` is the inconsistent one.

Recorded so the objection is not re-raised.

### The design

**Endpoints own what they ARE; `build/` owns what is DONE with them.**

```python
class StreamIFSlave(...):
    boundary_kind: ClassVar[str] = "axis_in"

class BramIFMaster(...):
    boundary_kind: ClassVar[str] = "bram"

class MMIFMaster(...):
    boundary_kind: ClassVar[str | None] = None   # refuses: the direction IS the type
```

`kind_of_endpoint` becomes a lookup of `ep.boundary_kind` plus the `None` refusal — keeping the one
piece of real logic it has (a bare `MMIFMaster` under-specifies, and guessing emits a `const` pointer
for a port that is written).

`BFM_DUALS` and the port emitters **stay in `build/`**.  "Which C++ class drives this port from
outside" is a fact about the testbench library, not about the endpoint: the endpoint says `axis_in`,
the testbench decides that means `AxisMaster`.

**Name it `boundary_kind`, not `kind`.**  The same endpoint lowers differently by position — a
`StreamIFSlave` is `axis_in` at a boundary but an `hls::stream` FIFO internally, and internal
lowering is derived from the *interface* type in `derive_internal_edges`, a separate walk.  A bare
`kind` would be read as covering both.

### The seven downstream tests — a separable second question

Some become endpoint properties (`is_pin`); some stay table lookups (`needs_bfm`).  Draw that line
case by case rather than up front.  One of them is already inconsistent and should be fixed either
way: `if kind == "bram": continue` bypasses `BFM_DUALS`, while `mm_slave` and `axilite_slave`
express "no model" as a table row.  A bare `None` cannot absorb `bram`, because the two mean
different things — *"needs a model, none exists"* (a gap that should eventually error) versus *"needs
no model at all"* (by design, skip silently).  Distinguish them explicitly.

### Loose end

`kind_of_endpoint`'s docstring cites `plans/endpoint_types_not_tags.md`, **which does not exist**.
It is the stated rationale for the whole design.  Recover it or fix the citation.

## Order

**Code first, then docs.**  Documenting names and a dispatch you are about to change is writing them
twice — the argument that correctly deferred `bram_access/python.md` until after the typed-`BramIF`
work.

1. **R1–R5** (Part 3), mechanically.  Every generated artifact byte-identical and the XSI gates
   unmoved: a rename changes no logic.
2. **`boundary_kind` on the endpoints** (Part 4).  Same gate — this is a refactor, not a behaviour
   change.  Land it before any page describes the dispatch.
3. **The vocabulary table** on `guide/interface/overview.md` — get vs read vs acquire, and `_nb`.
4. **The four-tier presentation** (Part 2c) on the overview page, replacing the flat list.
5. **The missing page**: the endpoint-kind vocabulary and the two tables that consume it (Part 2b).
6. **Link stubs** on every `guide/interface/` page.
7. **Move `schema/hls/serialization.md`** beside `comp_codegen/interface.md`.

**The Part 2d re-org SPLITS across this ordering, and the split is the point:**

- **Structural (independent of steps 1–2, can go FIRST):** the folder moves, the two relocations out
  of the section, the `index.md` tier table, the front-matter/`grand_parent` rewiring, the
  duplicate-title audit.  Moving a page does not document a method name, so nothing here is
  invalidated by a rename.
- **Prose (must wait for steps 1–2):** the link stubs, the vocabulary table, and above all the two
  NEW pages.  `CreditStreamIF` and `AckedStreamIF` carry `write_nb` / `read_nb` / `read_frame_nb`,
  which **R5 renames** — writing those pages first means writing them twice.
9. **Write `CreditStreamIF` / `AckedStreamIF` docs** — the real gap, and the largest single piece of
   new writing here.
10. **`SchemaTransferIF` / `ArrayTransferIF` lowering** section.

Steps 1–2 block everything downstream.  Steps 9–10 are new writing and can be scheduled
independently of the rest.

## Open question for the author

This plan assumes the three-arc structure (`interface` -> `comp_codegen` -> `custom_hooks`) was
**deliberate**, inferred from folder layout and `audience` tags rather than from any stated
principle — `guide/index.md` says only that the TOC is generated.  If the arcs emerged by accretion
instead, the case for re-foldering gets stronger and Part 2 should be reopened before Part 3 lands.
