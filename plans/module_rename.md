# Rename the component classes to *module* names

## Why

The base names imply a **leaf node** when the type is actually the generic building block —
the top level of a design or a testbench is *also* one of these. `HwComponent` and especially
`FreeRunComp` read as "a leaf component", yet `FreeRunComp` is the base a **composite** (a whole
design, a testbench) inherits from. The SystemC convention (`sc_module`) got this right: a *module*
is a node that may be a leaf or contain a hierarchy, and the name carries no leaf connotation.

This follows the `CompositeComp` elimination (commit 64110be), which removed the alias that dressed
the same confusion up as a second class. The vocabulary is now **standalone** (a `run_iter` body) vs
**composite** (sub-components); `_kind()` already returns those. This plan finishes the job at the
class-name level.

## The hierarchy today

```
NamedObject                      (named.py)
└─ SimObj                        (simobj.py)         — three-phase lifecycle
   └─ Component                  (component.py)       — add_comp / add_endpoint / add_if
      └─ HwComponent             (hw_component.py)    — hardware object; typed ports, HwParam
         ├─ FreeRunComp          (hw_freerun.py)      — free-running ap_ctrl_none execution model
         └─ HostActivated        (hw_hostactivated.py)— host-activated (ap_ctrl_hs + regmap) model
SeqTB                            (hw_testbench.py)    — extends NamedObject (separate)
```

## Proposed names (CONFIRM before executing)

**Scheme A — keep the `Hw` prefix (recommended):** least semantic drift, keeps the namespace signal.

| today | proposed |
|---|---|
| `HwComponent` | `HwModule` |
| `FreeRunComp` | `FreeRunModule` |
| `HostActivated` | `HostActivatedModule` (or leave `HostActivated` — it never implied "leaf") |
| `Component` (SimObj subclass) | leave as-is, or `ModuleBase` — it is the lower sim-level base |

**Scheme B — drop `Hw`, plainer:** reads cleaner, but `Module` is a common word and loses the
hardware signal at call sites.

| today | proposed |
|---|---|
| `HwComponent` | `Module` |
| `FreeRunComp` | `FreeRunModule` |
| `HostActivated` | `HostModule` |

Open questions for the user:
1. Scheme A or B?
2. Rename `HostActivated`, or leave it (it never implied leaf)?
3. Rename the file basenames too (`hw_component.py` → `hw_module.py`, `hw_freerun.py` →
   `hw_freerun_module.py`)? File renames add churn but keep module names aligned with class names.
4. Keep `Component` as the SimObj-level base name, or lift it into the scheme?

## Scope

- `HwComponent`: 184 occurrences
- `FreeRunComp`: 81
- `HostActivated`: 64
- **52 files** touched (source, tests, examples), plus docs.

## Approach — atomic per symbol, no lingering aliases

The `CompositeComp` lesson: a back-compat alias kept "to avoid churn" *is* the churn — it becomes a
second name readers trip over. So do NOT introduce `HwComponent = HwModule` shims. Rename each symbol
in one pass across all files, gated, and delete the old name entirely.

Order (each its own commit, each fully gated before the next):

1. **`FreeRunComp` → `FreeRunModule`** first — smallest blast radius (81), and the one whose name most
   misleads. Includes the `hw_freerun.py` docstrings already using "standalone/composite".
2. **`HostActivated` → `HostActivatedModule`** (if confirmed) — independent of 1.
3. **`HwComponent` → `HwModule`** last — largest (184), and the other two now sit under it cleanly.
4. **File renames** (if confirmed) — `git mv` + fix imports, separate commit so history stays legible.
5. **Docs sweep** — prose references, after the code names are final.

Mechanically: `git grep -l <Old>` → scripted word-boundary replace (`\bOld\b`) → **read the diff**,
never trust the sed blindly (watch for substring hits: `HwComponentFoo`, `FreeRunCompositeX`, doc URLs,
`.rst` cross-refs `:class:\`...HwComponent\``).

## Gates (every step)

- **No generated-code drift** — regenerate all examples (`mem_copy.py`, `interleaver.py`,
  `gather_toy.py`, `mem_stream_gen.py`, `toy.py`); `git status` on `gen/`+`xsi/` must be empty.
- **Fast loop** at the **6-failure baseline** (`-m "not vitis and not xsi"`).
- **`-m xsi`** — 8 passed, 158/176/2835/3469.
- **Import smoke test** — every example class still resolves and subclasses the renamed base.

## Risks / notes

- **Substring false positives** — `HwComponent` is a prefix of nothing common, but `Component` (the
  SimObj base) IS a substring of `HwComponent`; if `Component` is also renamed, do `HwComponent`
  first or the replace collides. Word-boundary anchors mitigate but read every diff.
- **`.rst`/Sphinx cross-refs** in docstrings (`:class:\`~waveflow.hw.hw_component.HwComponent\``)
  break silently if the module file is renamed — update path AND symbol together.
- **CLAUDE.md** describes `Component` as "Base class for hardware objects (HwObj)" and names
  `Component`/`HwComponent`/`FreeRunComp` in the architecture section — update it in the same arc.
- **`CodegenPath` kind `'leaf'`** (in `codegen_dispatch.py`) is a *separate* concept from
  `FreeRunComp._kind()` and spans host-activated too; decide separately whether it also becomes
  `'standalone'`. Not part of the class rename.
- **memory** — update [[project-one-component-two-flows]] and any note naming these classes once the
  names are final.

## Sequencing

Per the user: **finish the mem_copy docs first** (codegen.md stub, index.md), *then* run this rename.
The docs written in the meantime will use the current names; the docs sweep (step 5) catches them.
