# Plan: tested toy components (`Square`, `Double`→`Square`)

## Context

**The guide currently shows code nobody has run.** `docs/guide/components/` teaches the component kinds
with *illustrative* examples that are not backed by any file and are not exercised by any test. Two known
defects were flagged when those pages were written and are still there:

- **`freerun.md`'s `Square`** — never executed. The neighbouring `overview.md` `MovingAvg` had to be
  labelled *"simulation model, not synthesizable form"* precisely because its NumPy body would not
  survive the extractor; nothing checks that `Square`'s array-operator body actually does.
- **`composite.md`'s `Double`→`Square`** — its `StreamIF(...)` **omits `clk=`**, which the working
  `Neuron` in `docs/guide/concurrency/python/subcomponent.md` passes. Flagged at the time as "verify";
  never verified.

Meanwhile the *real* examples of these kinds (`MemRStream`, the interleaver) are far too complex to be a
fixture or a teaching example.

So: **make the docs' toys real and tested.** That kills a whole class of doc-rot (the code is executed by
CI, so it cannot silently drift), and it hands
[`plans/codegen_check_family.md`](./codegen_check_family.md) the fixtures it needs — a minimal subject
**per kind** that `check()` can be pointed at, plus crafted variants that must fail.

Coverage after this lands:

| kind | tested minimal example |
|---|---|
| `HostActivated` | ✅ `simp_fun` (already tested, documented, Vitis-verified) |
| `FreeRunComp` | **`Square`** (this plan) |
| `CompositeComp` | **`Double`→`Square`** (this plan) |

## Scope

**Scope (ii)** — the two missing kinds. Deliberately **not**:

- promoting `MovingAvg` to a full `pure_stream` teaching example (its own item, see
  [[project-example-rename-scheme]]);
- a full build DAG / Vitis flow for the toys (that is what `regmap` is for);
- making `run_iter` auto-extraction work (a known gap — see below).

## Where it lives

`examples/toy/` — a minimal, importable module the guide quotes and the tests import. It is a **toy**,
not a teaching walkthrough: no DAG, no Vitis, no docs pages of its own. Name it plainly so nobody mistakes
it for a reference design; a short `README.md` should say *"minimal components used by `guide/components`
and by the infra tests; see `examples/regmap` for a real end-to-end example."*

*(If `examples/toy/` reads wrong, `examples/minimal/` is the alternative — settle before writing.)*

## What to build

1. **`Square(FreeRunComp)`** — one firing = one *n*-vector squared, matching `freerun.md`:
   ```python
   class Vec(DataArray):
       element_type = Float32
       static = True
       max_shape = (4,)

   @dataclass
   class Square(FreeRunComp):
       cpp_kernel_name: ClassVar[str | None] = "square"
       # x_in: StreamIFSlave, y_out: StreamIFMaster in __post_init__
       def run_iter(self) -> ProcessGen[None]:
           x = yield from self.x_in.get(Vec)
           yield from self.y_out.write(self.square(x))
       @synthesizable
       def square(self, x): return x * x
   ```
2. **`Double(FreeRunComp)`** — `z = x + x`, and **`ScaledSquare(CompositeComp)`** wiring
   `x → double → z → square → y`, matching `composite.md`. **Resolve the `clk=` question here** — pass it
   if the binding needs it, and fix the doc to match reality either way.
3. **Tests** (`tests/examples/test_toy.py`): each component elaborates; the pysim runs and produces the
   right values (`Square`: y = x²; `ScaledSquare`: y = (2x)²); the composite schedules its children and
   the internal edge carries data.
4. **Point the docs at the real code** — `freerun.md` and `composite.md` quote `examples/toy/` and link
   it, exactly as `hostactivated.md` links `simp_fun`.

## The honest part: what these toys can and cannot claim

Do **not** let the docs overclaim once the code is real. Two live gaps
([[project-component-tree-simplified]]):

- **No `FreeRunComp` is auto-extracted today.** `MemRStream`/`MemWStream` hand off *fixed hand-written*
  `hls::task` bodies via `kernel_task()`; their `run_iter` is explicitly "NOT extracted — pysim golden
  only". So `Square` will be a **tested pysim model in synthesizable form**, not a generated kernel.
  If `kernel_files_to_str(Square)` happens to extract cleanly, say so and add it as a test; if it does
  not, **that is a finding worth recording**, not something to paper over — and it is exactly the kind of
  thing `check(Square, "free_running_kernel")` is meant to report once
  [`codegen_check_family.md`](./codegen_check_family.md) lands.
- **Cross-iteration mutable state is not supported** (the extractor forbids reading mutable `self.X`).
  Both toys are deliberately **stateless** for this reason. Do not add an accumulator.

So the claim these toys earn is: *"this is real code, it runs, the docs match it."* Not *"this
synthesizes."* Keep `freerun.md`/`composite.md` honest about that distinction, and let the check family
make the synthesis claim checkable later.

## Verification

- Run via the venv: `../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis"` with `PYTHONPATH=.`;
  failures ⊆ the documented baseline (`test_build`×9 + `dataschema_poly`×1 + `poly` timing×5 — see
  [[project-test-baseline-failures]]).
- New `tests/examples/test_toy.py` passes.
- **Byte-identical** generated C++ for the existing kernels and all four TBs — this plan adds an example
  and tests; it must not touch codegen.
- `ruff check` the new files.
- The quoted code in `freerun.md` / `composite.md` **matches the file** (copy it, do not paraphrase).
