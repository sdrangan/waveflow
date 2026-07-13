# Plan: execution-model component classes (`FreeRunComp` + the dedicated-class hierarchy)

## Motivation

Today a synthesizable component's **execution model is inferred**, in two places:

- **Method** — [`extract_kernel`](../waveflow/build/hwcodegen.py) (hwcodegen.py:1265) picks `main` if
  `type(comp)._is_testbench`, else `on_start` if any endpoint is a `VitisRegMapMMIFSlave`, else `run_proc`.
- **Pragma** — `control_mode` defaults to `ControlMode.AUTO`, which infers `FREE_RUNNING` (ap_ctrl_none)
  from a **`WhileStmt` at the extracted root**, or `PER_INVOCATION` (ap_ctrl_chain) from a `SeqStmt` root
  (hw_component.py:160).

The `AUTO` "find a `while True` at the root" rule is exactly the parse-and-check we want to delete. And
"has a `VitisRegMapMMIFSlave` ⇒ host-activated" is genuinely ambiguous — `poly` has a regmap *and*
streams data; a future free-running tile could carry a regmap purely for config. **Make the execution
model a declared property of the class, not something inferred.** `HwTestbench` (hw_testbench.py:26,
`_is_testbench: ClassVar[bool] = True`) already sets the precedent.

## Key existing machinery to build on (do NOT reinvent)

- `ControlMode` enum (hw_component.py:160): `AUTO`, `FREE_RUNNING` (ap_ctrl_none), `PER_INVOCATION`
  (ap_ctrl_chain). Classes set `control_mode` explicitly to skip `AUTO` inference.
- `control_mode: ClassVar[ControlMode]` on `HwComponent` (hw_component.py:251).
- `_is_testbench: ClassVar[bool]` + the `extract_kernel` dispatch — the pattern each new class copies.

## The design: one class per execution model

Each class declares its **kernel entry method** and its **`control_mode`**; `extract_kernel` dispatches
on the declared method instead of inferring. Mapping (mirrors `components/taxonomy.md`):

| Class | Entry method | `control_mode` | HLS shape |
|---|---|---|---|
| `FreeRunComp` | **`run_iter`** | `FREE_RUNNING` (ap_ctrl_none) | single-firing `hls::task` body (runtime re-fires) |
| `HostActivatedComp` | `on_start` | `PER_INVOCATION` (ap_ctrl_chain) | runs once per `ap_start` |
| `HwTestbench` *(exists)* | `main` | — (testbench) | sequential C++ driver |
| `LoopComp` *(future)* | `run_proc` (`while(1)`) | **new mode** (ap_ctrl_hs persistent) | persistent loop kernel (FIR-freerun form) |
| *behavioral* | — | — | plain `HwComponent`, never run through codegen |

`FreeRunComp` is the core:

```python
class FreeRunComp(HwComponent):
    control_mode: ClassVar[ControlMode] = ControlMode.FREE_RUNNING   # explicit — no root-shape inference
    _kernel_method: ClassVar[str] = 'run_iter'

    def run_proc(self) -> ProcessGen[None]:
        while True:
            yield from self.run_iter()      # pysim: the DES stand-in for the runtime re-firing

    def run_iter(self) -> ProcessGen[None]:
        raise NotImplementedError
```

Why this is the right shape (verified against the generated code): `mem_r_stream_task.h` is a
**single-firing** body — *"the hls::task runtime RE-FIRES this on each new command (there is NO internal
command loop)"*. So `run_iter` maps 1:1 to the task function; the `while True` is only the pysim wrapper.
**No `run_proc_setup`** — the hls::task model has no "before the loop"; persistent state lives on `self`
(set in `__post_init__`) and lowers to `static` locals. (A component that truly needs `while(1)` +
pre-loop setup is `LoopComp`, which overrides `run_proc` directly.)

## Contract checks (a benefit of explicit classes)

Each class validates itself in `__post_init__` and fails early/specifically:

- `FreeRunComp`: `run_iter` overridden; `on_start` NOT defined.
- `HostActivatedComp`: has a `VitisRegMapMMIFSlave`; `on_start` overridden.
- (general) a component may not set two conflicting markers.

## Implementation phases

**Phase 1 — `FreeRunComp` base + retrofit (pysim only; no codegen change).**
- Add `FreeRunComp` (in `hw_component.py`, next to `HwComponent`, or a small `hw_freerun.py`).
- Retrofit the current free-running components' **pysim** onto it: `MemRStream`, `MemWStream`
  (`waveflow/hw/mem_stream.py`) and the six interleaver tiles (`CmdRx`, `IlMemR`, `IlLoad`, `IlCompute`,
  `IlStore`, `IlMemW` in `examples/interleaver/interleaver.py`): `run_proc` (while True) → `run_iter`.
  These use **template bodies** (`KernelTask`), so codegen is unaffected — this is a pysim-ergonomics +
  intent change that proves the pattern. Set `control_mode = FREE_RUNNING` (harmless; already their mode).
- Regression: pysim goldens still byte-identical; existing codegen unchanged.

**Phase 2 — `extract_kernel` dispatch (gated on the first *extracted* free-running kernel).**
- Generalize `extract_kernel` to read a declared `_kernel_method` (default `run_proc`) and extract that,
  and to honor an explicit `control_mode` (skip `AUTO` root inference). `FreeRunComp` ⇒ extract
  `run_iter`, emit ap_ctrl_none — no `WhileStmt`-root detection needed.
- Verify the explicit `FREE_RUNNING` path emits ap_ctrl_none for a non-`while` (`run_iter`) body.

**Phase 3 — `HostActivatedComp`.**
- Formalize the on_start/PER_INVOCATION path as a class; migrate `poly`/`regmap` opportunistically.
- Keep the `VitisRegMapMMIFSlave`-presence inference as a **fallback** so nothing breaks mid-migration.

**Phase 4 — future (defer).**
- `LoopComp`: needs a NEW `ControlMode` value (today only ap_ctrl_none + ap_ctrl_chain exist; the
  ap_ctrl_hs-persistent form is unrepresented). Add when that codegen path is generated.
- `SystemCTestbench` (concurrent TB); optional `SynthComp` base to make synthesizable-vs-behavioral a
  type distinction (`isinstance(comp, SynthComp)` ⇒ run codegen).

## Docs updates (do alongside)

- **`components/taxonomy.md`** — replace "the kind is selected automatically from the endpoints" with
  "the kind is the component's **class**"; name `FreeRunComp` / `HostActivatedComp` / `HwTestbench` /
  behavioral in the tree and the selection table. The taxonomy becomes the class hierarchy.
- **`comp_codegen/structure.md`** — update the execution-model table: the mode is *declared*
  (`control_mode` + entry method) not inferred from a `while` root; add `run_iter` / `FreeRunComp`.
- **`concurrency/python/subcomponent.md`, `lcs.md`, `mem_wrap.md`** — the tiles become `FreeRunComp`
  with `run_iter` (update the example code once retrofitted).
- **`concurrency/hls/synth_types.md`, `hlstask.md`** — `FreeRunComp` ⇒ ap_ctrl_none single-firing body.

## Open questions

- [ ] Name: `run_iter` (chosen) for the body.
- [ ] File location: extend `hw_component.py` vs. a new `hw_freerun.py` (HwTestbench uses its own file).
- [ ] Introduce `SynthComp` base now or later (encodes the synthesizable-vs-behavioral boundary).
- [ ] `HostActivatedComp` name (vs `LaunchedComp` / `InvokedComp`).

## Sequencing

1. Phase 1 (`FreeRunComp` + retrofit pysim) — self-contained, non-breaking; land + verify goldens.
2. Docs: `components/taxonomy.md` + `comp_codegen/structure.md` (the class-based framing) once Phase 1 lands.
3. Phase 2 when an extracted free-running kernel exists to test against.
4. Phase 3 (`HostActivatedComp`) when convenient; keep inference fallback.
5. Phase 4 deferred.

> Note: untracked plan; touches no tracked files. Grounded in hwcodegen.py:1265 (`extract_kernel`),
> hw_component.py:160/251 (`ControlMode` / `control_mode`), hw_testbench.py:26 (`_is_testbench`),
> waveflow/build/mem_r_stream_task.h (single-firing body).
