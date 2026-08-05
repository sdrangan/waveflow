# The integration term is a measurement, and it is not stored

## Why

`InspectSynthStep` attributes a synthesis report into three parts:

```text
top  =  Σ(modules)  +  integration
```

Two of them are durable.  `store_report` files a record per module, keyed by the module's elaborated
structure, and a resource model derives its corpus from those.  **The integration term is computed,
written to an untracked `results/resources.json`, and then lost.**

So both examples carry it as a hand-typed constant:

```python
INTEGRATION_TERM = {"lut": -2, "ff": 0, "dsp": 0, "bram": 0}          # examples/vecmult
INTERFACE_BY_MEM_DWIDTH = {32: {"lut": 1984, "ff": 1949, ...}}       # examples/fir_block
```

That is the same defect the calibration harmonization spent its length removing for per-module
figures — a measurement transcribed into source — sitting one level up.  It went unnoticed because
`vecmult`'s per-module numbers were migrated to records in the same session that wrote
`INTEGRATION_TERM` from a literal.

Verified rather than assumed: `top − Σ(modules)` over `fir_block`'s committed corpus yields
`{lut: 1984, ff: 1949, dsp: 0, bram: 2}` at **all 24 points, one distinct value** — exactly the
constant.  `vecmult`'s is `{lut: -2}` at all 16.

The stakes are not small.  On `fir_block` the term is **1984 LUT, 29% of the design** and the
second-largest contributor after the compute.  It is the `m_axi` adapters, the inter-task FIFOs, the
AXI-Lite control block and the DATAFLOW shell — and it is the one number in the estimate with no
provenance behind it.

### The diagnostic is "no writer", not "a number in source"

Worth stating precisely, because the obvious pattern-match is wrong and was made once already while
drafting this.  `examples/mem_copy/calibrate_platform.py` also holds measurements as literals:

```python
RTL_SPAN = {128: 183.0, 512: 615.0}
```

That is **not** this defect.  The timing axis has a complete writer —
`RtlSimStep` → `ExtractBurstsStep` → `CollectTimingStep`, which calls `collect_rtl` and
`collect_pysim` and fills the raw tier from the trace with no transcription anywhere.  That script
bypasses it deliberately so the platform can be regenerated on a machine with no Vivado, with the real
loop gated behind `-m xsi`.  It is a reproduction path with a stated purpose, closer in kind to
`vecmult`'s `GRID` oracle than to a gap.

The integration term has no such path.  Nothing files it, so a literal is the *only* place it can
live — which is why the fix is a writer rather than discipline.

**The test to apply before calling something this defect:** ask whether an automated path exists that
would file the number, and whether the literal is bypassing it or substituting for it.

### The architecture already says it should be a record

Nothing here needs a new concept, which is why this is worth doing before anything is built on top:

* `InterfaceResourceModel` **is a** `ResourceModel` **is a** `CalibModel`, so it already has a
  `corpus_path` and a `fit`.  Nothing writes to them.
* the composite **already has a module identity** — `walk_modules(top, include_top=True)` yields
  `FirBlock → fir_block-5548d6c1`.  There is a key to file under.
* `store_report` already receives that identity in its `identities` map, and skips it only because
  `report.modules` has no row for it.

The writer is the missing piece.  Everything it would write into exists.

## Design sketch

### File the integration term as a record against the top's identity

```python
store_report(report, store, identities, ...)
  ├─ one record per report.modules entry          (unchanged)
  └─ one record for report.top_name's identity     (new) — payload = report.integration
```

Same envelope, same provenance (part, period, tool, signature), same `source="hls_estimate"`.  The
payload carries the integration counters and a marker distinguishing it from a module row, because
a reader must not sum a composite's own cost as though it were a leaf's.

**One record per synthesis**, not one per distinct value.  `fir_block` would file 24 integration
records that happen to agree, and that is the point: today the invariance across compute parameters is
*asserted* in a docstring, and after this it is **derivable from the store** — 24 rows, one distinct
value, checkable rather than claimed.  A future point that broke the invariance would show up as a
second value instead of quietly contradicting a comment.

### The keying wrinkle, and why per-point records resolve it

`InterfaceResourceModel` keys its table on the **boundary signature**, because that is what the term
depends on — it moved with `mem_dwidth` and not with `ntap`/`samp_w`/realization.  The store keys on
**module key**, which for a composite varies with every compute parameter.  Two different identities
for the same object.

Per-point records make this a *reduction* rather than a conflict:

```text
24 integration records (one per synthesis, keyed by the composite's module key)
   │  corpus_from_records → rows carrying the boundary columns
   ▼
1 table entry per distinct boundary   ← the model's own dedup, and its evidence
```

So the corpus rows must carry the boundary (`n_ports`, `n_channels`, `ports`, `channels`), which
`InterfaceResourceModel.get_params` already produces.  That means the reduction needs the model's
`get_params`, not just `identity.params` — see open decision 2.

### What the examples lose

```python
INTEGRATION_TERM        = ...   # examples/vecmult/vecmult_corpus.py   — deleted
INTERFACE_BY_MEM_DWIDTH = ...   # examples/fir_block/fir_block_corpus.py — deleted
vec_mult_shell()                # the hand-built probe table            — reads the store instead
FirBlock.add_rm_self            # ditto
```

## Phases

### P0 — the gate

Both examples' composed estimates must be **unchanged**.  `tests/calib/test_harmonize_equivalence.py`
already snapshots them (fir_block 24 points, vecmult 16), so the gate exists; this phase only confirms
it is green before anything moves and records that the golden must not shift.

**Gate:** P0 snapshot byte-identical at the start and end of this work.

### P1 — write the record — **DONE**

Extend `store_report` to file the integration row.  Decide the payload marker (open decision 1).

**Gate:** a real synthesis of each example files one integration record per point; the payload equals
`results/resources.json`'s `integration` block exactly.  `vecmult` is the cheap check — 16 points,
~12 minutes.

### P2 — read it back — **DONE**

`corpus_from_records` (or a sibling) yields the integration corpus.  `InterfaceResourceModel` builds
its table from that corpus rather than from a passed-in dict, deduplicating by boundary and **raising
if one boundary carries two different measurements** — that is a real contradiction in the data, not
something to average away.

**Gate:** the table built from the store equals the transcribed constants, entry for entry, on both
examples.  This is the phase that proves the transcriptions were right.

### P3 — retire the constants — **vecmult DONE; fir_block deferred**

Delete `INTEGRATION_TERM` and `INTERFACE_BY_MEM_DWIDTH`, and the probe-table builders that consume
them.  Update `docs/guide/resource_model/predict.md` and both examples' pages, which currently
describe a table built by hand.

**Gate:** P0 unchanged; `test_documented_numbers` still green (the 1984 appears in prose); the symbol
guard catches any page still naming a deleted constant.

**`fir_block` deferred, deliberately.**  Its store holds no integration records — populating them
means re-sweeping 26 points on a bigger design.  Deleting `INTERFACE_BY_MEM_DWIDTH` first was measured
rather than guessed at: the composed estimate drops from 6734 to **4750 LUT**, losing 1984 — 29% of
the design.  It would not be *silent* (the confidence correctly collapses to `UNCALIBRATED`, which is
the "nothing is silently zero" property working), but the estimate would be wrong and P0 would fail.
So the constant stays until `fir_block` is next swept, at which point this phase completes for it with
no code change — `InterfaceResourceModel.load_table` already prefers the store.

### P4 — the invariance becomes a check — **DONE**

Replace the docstring claims — *"measured invariant across all 24 compute configurations"* — with a
test that reads the store and asserts one distinct value per boundary.

**Gate:** the test passes and fails when a synthetic second value is injected.

## Open decisions

1. **How is an integration record distinguished from a module record?**  They share a store and a
   shape, and summing a composite's own cost as a leaf would double-count.  Options: (a) a payload
   marker (`"kind": "integration"`); (b) a separate `target` (`"integration"` alongside `"resource"`),
   which the `Record` schema already supports and which keeps `read(key, "resource")` returning only
   module rows.  Leaning (b) — it costs nothing, and a caller that forgets the distinction gets an
   empty result rather than a wrong sum.

2. **Whose `get_params` builds the interface corpus?**  `corpus_from_records` uses
   `identity.params` — hardware parameters — but the interface term is keyed on the boundary, which is
   not among them.  Either the reducer takes an optional model whose `get_params` enriches each row
   (mirroring what `VitisResourceModel.corpus` already does for structure columns), or the boundary is
   recorded in the payload at write time.  Leaning the latter: the boundary is a *fact about what was
   built*, and `corpus.md` says such facts belong in the row.

3. **Does the top's module key belong in the store at all?**  Filing it means the store contains an
   entry whose "module" is the whole design.  That is defensible — a composite is a module, and its
   own cost is what an `InterfaceResourceModel` predicts — but it does mean `ModuleStore.keys()` mixes
   scales.  The alternative is a sibling tier (`integration/` beside `modules/`).  Leaning: same tier,
   different `target`, per decision 1.

4. ~~**`fir_block`'s store has 26 `FirCompute` records but a current elaboration finds none.**~~
   **RESOLVED — it was a bug, now fixed** (`plans/key_stability.md`, merged).  A `LinCalibModel`
   reached the structure signature through an attribute the exclusion list did not cover, so
   refactoring that class moved the key of every module holding one.  The store has been re-addressed
   and a reachability guard now fails if it happens again.  P2's equality check can therefore trust
   what it reads.

## Risks

* **P1 needs synthesis.**  Filing the record is cheap; proving it files the right thing needs a real
  run.  `vecmult` at ~12 minutes is the affordable check; `fir_block` at 24 points is not, so P2's
  equality check against the transcribed constants is what covers it — the constants become the
  oracle for the mechanism that replaces them.
* **Deleting the constants removes the only copy** until the store is populated on every platform that
  needs one.  Sequence matters: P2 must prove equality *before* P3 deletes.
* ~~**Decision 4 is unresolved and load-bearing.**~~  Resolved and fixed before starting; the store
  now resolves for every leaf, guarded by `tests/calib/test_key_stability.py`.
