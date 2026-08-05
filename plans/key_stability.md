# A module key must not depend on its calibration models

## Why

A module's record-store address is a digest of `structure_signature(comp)`.  That signature is built
by `_canon`, which drops elaboration *context* — the sim, back-references, names — by **attribute
name**, from `_CONTEXT_ATTRS`.

`FirCompute` holds its timing model under two names:

```python
self._timing_model = ...        # in _CONTEXT_ATTRS -- dropped
self.timing = self._build_timing_model()      # NOT in _CONTEXT_ATTRS -- serialized
```

So the model's internals land in the signature:

```text
('timing', ('obj', 'waveflow.calib.calib.LinCalibModel',
   (('_coef', …), ('intercept_name', …), ('path', …), ('platform', …), ('seed', …))))
```

**A module's resource key therefore depends on the field layout of its timing model.**  Refactoring
`LinCalibModel` moves the key of every module that holds one, and every stored measurement for those
modules becomes unreachable.

### It already happened

The calibration harmonization gave `LinCalibModel` defaults on `basis`/`target`, renamed `basis_fn` to
`transform_fn` and added `target_names`.  Each changes the serialized field set.  Measured against the
committed `fir_block` store across its 24-point grid:

| class | keys found in store | keys missing |
|---|---|---|
| `FirCmdRx`, `MemRStream`, `MemWStream` | 24 | 0 |
| **`FirCompute`** | **0** | **24** |
| `FirBlock` | 0 | 24 — *expected; the composite has no records (see `integration_record.md`)* |

Same parameters, different address: `fir_compute-5bf01fd8` from a fresh elaboration,
`fir_compute-d73b0e06` in the store.  `FirCompute` is the only leaf carrying a timing model, which is
exactly why it is the only one that moved.

The control makes it conclusive: `vecmult`'s store, written the same day by the same code path, has
**16 of 16** keys resolving.  The two stores differ in exactly one respect — whether their modules
carry a timing model.

### And nothing noticed

`FirCompute` is served by a **fitted** model that trains from the committed grid and never consults
the store, so its 26 records are readable but unaddressable and every gate stayed green.  Had it been
a *lookup* — as the three static modules are — the design would have silently read 26 measurements
lighter.

That is the failure mode `docs/guide/calib/corpus.md` already warns about, reached from the other
direction: not a row filed under a key no composite produces, but a **key that moved under rows
already filed**.

### Blast radius

At least two examples use the pattern, so it is idiom rather than accident:

```python
examples/fir_block/fir_block.py:353        self.timing = self._build_timing_model()   # FirCompute
examples/interleaver/interleaver_inband.py:237  self.timing = self._build_timing_model()   # IlComputeInband
```

## The fix is by type, not by name

Adding `"timing"` to `_CONTEXT_ATTRS` repairs today's instance and leaves the mechanism intact: the
next attribute holding a model under a different name leaks again, silently, and is discovered when a
store stops resolving.

**A `CalibModel` is never structure.**  It predicts something *about* the hardware; it is not part of
it.  Nothing about a module's ports, sub-components, interfaces or parameters changes because a
coefficient was refitted — and a key that moves when a coefficient moves is not addressing structure.

So `_canon` should drop a value because of **what it is**, not what it is called.

### How, without inverting the dependency

`waveflow/build/elaborate.py` importing `waveflow.calib.calib` to `isinstance`-check would add a
build → calib edge at module scope, while `waveflow/calib/module_key.py` already imports
`build.elaborate`.  No cycle today (the calib side imports `module_key` lazily), but it is a needless
coupling in the layer everything else is addressed by.

**Leading option — a marker.**  `CalibModel` declares itself context; `_canon` looks for the marker:

```python
class CalibModel:
    __wf_structure_context__ = True      # never part of a structure signature

# in _canon
if getattr(type(value), "__wf_structure_context__", False):
    return ("context", type(value).__name__)
```

No import in either direction, and it extends: a future class that is *about* a module rather than
*part of* it declares the same thing.  Keeping the type name in the tuple preserves the one bit that
is arguably structural — *whether* a model is attached — while dropping everything mutable inside it.

See open decision 1 on whether even that bit belongs.

## Migration

The keys move again when this lands, deliberately, and the committed stores must follow.

**Map by `(cls_name, resolved params)`**, not by re-elaborating each leaf standalone.  A module with
ports has no settled structure until wired, so elaborating `FirCompute` alone would produce a third
key belonging to no real design — the trap `corpus.md` documents.  Instead: walk the *design* over its
corpus grid, build `{(cls_name, params) → new key}`, and rename each stored directory to its match.

Verifiable both ways, and that is the gate: every stored key maps to **exactly one** new key, and
every walked key is claimed by **exactly one** stored key.  A many-to-one or an orphan means the
mapping is wrong and must stop rather than guess.

Confirmed reachable for the case at hand — the fresh `fir_compute-5bf01fd8` and the stored
`fir_compute-d73b0e06` already agree on every parameter.

## The missing guard

Nothing checks that a store's keys are still reachable from a current elaboration.  That is why a
refactor in `waveflow/calib/` could orphan 26 records in `examples/fir_block/` with every test green.

Two levels, and both are cheap:

* **per example** — a test that walks the design over its corpus grid and asserts every leaf key
  resolves in the store.  It would have failed the moment `LinCalibModel` changed.
* **a CLI** — `waveflow_calib check <platform> --design <module>` reporting reachability, coverage and
  orphans, for a library a design does not ship with.

The per-example test must know that a composite legitimately has no records until
`integration_record.md` lands — so it asserts over *leaves*, and gains the composite when that does.

## Phases

### P0 — pin the current keys

Snapshot the walked keys for both examples over their corpus grids.  This is the gate the migration is
measured against: the point is not that `fir_compute-5bf01fd8` is *right*, but that the mapping from
old to new is total and one-to-one.

**Gate:** a committed key snapshot per example, and `tests/calib/test_harmonize_equivalence.py`
green — predictions must not move.

### P1 — exclude calibration models from the signature

The marker, and `_canon` honouring it.  Keys move for every module holding a model.

**Gate:** the leak is gone — `_serialize(structure_signature(comp))` contains no `CalibModel` for any
module in either example; and a *changed* `LinCalibModel` field no longer moves any key, asserted
directly by mutating one in the test.

### P2 — migrate the committed stores

The `(cls_name, params)` mapping, applied to `fir_block`'s store (and `vecmult`'s, which has no timing
models and so should be a no-op — a useful control).

**Gate:** the one-to-one check above; then the reachability test from P3 passes, which is the real
proof the migration landed.

### P3 — the reachability guard

Per-example test first, since it is what would have caught this.  The CLI after.

**Gate:** it fails when a key is perturbed, and passes on both migrated stores.

### P4 — retire the second name — **premise was wrong; done differently**

This phase assumed `self.timing` was a duplicate handle on `_timing_model`, to be collapsed.  It is
not:

| | |
|---|---|
| `self.timing` | a `LinCalibModel` — the **compute** model, `cycles = latency + ii·n`, read directly in `timed_delay` |
| `_timing_model` | **`None`** on these modules — set only by `add_timing_model()`, for *residual* calibration, discovered by `CollectTimingStep` |

Two different models with confusingly adjacent names.  Collapsing them would have deleted the only
model the module has.

The hazard the phase existed to remove was already gone: P1 excludes **by type**, so a model is
harmless whatever it is called — verified by attaching one under an attribute name no exclusion list
knows about and confirming the key does not move.

What remained was the naming collision alone, since `self.timing` sat beside a `timing_model` property
returning something else.  Renamed to `self.compute_timing` in both examples.

**Gate:** P0 keys unchanged (a rename cannot move them, now that names do not matter — which is itself
the check).

## Open decisions

1. **Should "a model is attached" be structural at all?**  The marker proposal keeps the type name, so
   attaching a model still perturbs the key once.  Arguments for dropping it entirely: whether a
   module carries a timing model is a *calibration* fact, and a design measured before a model was
   attached describes the same hardware as one measured after.  Arguments for keeping it: a module
   with a model may be *built* differently.  Leaning: drop it entirely — emit nothing — and let P1's
   mutation test cover both.

2. **Do `HwConst`-like calibration inputs belong in the signature?**  A seeded model changes what the
   *simulation* predicts without changing the hardware.  This plan says a model is never structure,
   which answers it — but it is worth stating explicitly in `module_key.py`, whose docstring currently
   lists three properties it enforces and should list this as a fourth.

3. **Is `_CONTEXT_ATTRS` still needed?**  Most of its entries (`sim`, `parent`, `name`,
   `firing_records`) are not models and stay.  The marker complements it rather than replacing it.

4. **Should the migration be a script or a one-off?**  A committed script is auditable and re-runnable
   for the next deliberate key move; a one-off leaves the repo tidier.  Leaning script, under
   `waveflow/calib/`, because this is the *second* deliberate key move in two months and there will be
   a third.

## Risks

* **Two deliberate key moves in quick succession.**  The P1 `_CONTEXT_ATTRS` change moved keys, and
  this moves them again.  Both stores must be migrated in step, and the P0 snapshot is what makes the
  second move auditable rather than a second silent shift.
* ~~**`vecmult` as the control.**~~  **Confirmed:** `vecmult` attaches no timing model, leaks no
  `CalibModel` into any signature, and **all 16 of its store keys resolve** against a fresh
  elaboration.  A store written by the same machinery, on the same day, differing only in whether its
  modules carry a model — which is what makes "the model is what moved the key" a diagnosis rather
  than a hypothesis.
* **Sequencing.**  `integration_record.md` P2 reads the store to prove an equality.  Doing that against
  a store with stale keys would compare the wrong things, so **this plan comes first**.
