# Plan: single-process `SeqTB`

## Context

Today Waveflow maintains **two shadows of every testbench operation**: the synchronous, codegen-only
forms a `SeqTB.main()` uses (`push`/`pop`, `run_once`, `regmap.read_uint32_file`) and the yielding,
sim-side forms a `SimObj` testbench uses (`write`/`get`, and now `run_once_sim`). `push`/`pop` literally
*raise* if run ("codegen-only in v1; use get() for sim"). This split means every stream op exists twice,
and a `SeqTB` can never model timing (no `yield`s → no clock).

The goal (decided in discussion, see [[project-run-once-sim-and-seqtb-direction]]): make `SeqTB.main()` a
**single sequential process** — `yield`s allowed — so it can use `write`/`get`/`run_once_sim` *and* still
lower to a straight-line C++ `int main()`. The line that stays fundamental is **concurrency**, not
`yield`: a single sequential process lowers to C++; `env.process(...)` fan-out / interleaved TB↔DUT
feedback does not (that is the Flow 2 → Flow 3 / SystemC boundary).

`run_once_sim` + `on_start`-as-process (merged `98975fe`) did the **invocation** side. This plan does the
**testbench** side. **Payoff:** the `simp_fun` example becomes one clean `SeqTB` (input struct →
`run_once_sim` with timing → output), and `SimpFunHost` goes away.

## Current mechanics (what a Stage-1 implementer must know)

- `extract_testbench(comp)` → `HwStmtExtractor(comp, method_name='main', is_testbench=True).extract()` →
  `resolve_testbench` → IR (`DutBindStmt`, `KernelCallStmt`, `TbStreamIOStmt`, `TbFileIOStmt`,
  `TbRegmapFileReadStmt`, `TbStatusJsonStmt`, …) → `tb_to_cpp` emits the C++ (`waveflow/build/hwgen.py`).
- The extractor **already strips `yield <@sim_only call>`** (added with `on_start`-as-process,
  `hwcodegen.py` ~L1003) — this is shared, so it should already apply in `is_testbench` mode. Confirm.
- `push`/`pop` (`interface.py`) lower to `TbStreamIOStmt`; `run_once`/`dut.run()` lower to
  `KernelCallStmt`. `run_once`'s **return value is not captured** today — the TB reads outputs via
  `regmap.write_status_json`.

## Stage 1 — the extractor accepts a yielding `main()` (codegen only, byte-identical)

Teach `extract_testbench` to lower the *process* forms to the **same C++** it already emits for the
synchronous forms, so a yielding `main()` and the current synchronous `main()` are byte-identical:

1. **`yield self.timeout(...)`** in `main()` → stripped (verify the shared `@sim_only`-yield handling
   fires in `is_testbench` mode; add if not).
2. **`[y =] yield from dut.run_once_sim(...)`** → the *same* `KernelCallStmt` as `dut.run_once(...)`,
   **plus** capturing the return into a local (new: bind `y` to the kernel's output field(s) so a later
   `out.y = y` / file write can use it). `run_once` and `run_once_sim` must lower identically.
3. **`yield from ep.write(data)` / `x = yield from ep.get(Schema, ...)`** → the *same* `TbStreamIOStmt`
   as `ep.push(data)` / `ep.pop(x)`.

**Gate:** all four existing TBs (`poly_tb`, `hist_tb`, `simp_fun_tb`, `block_scale_tb`) stay
**byte-identical** (they don't yield yet, so nothing changes); and a *yielding rewrite* of one TB (e.g. a
scratch `_PolyTBProc` using `yield from`) produces the **same** `*_tb.cpp` as its synchronous twin. This
is the safe, bounded first slice — **do this stage first and stop for review.**

## Stage 2 — `SeqTB` runnable as one process (the timing payoff)

Make `SeqTB.main()` *run* in Python as a single SimPy process (give `SeqTB` a throwaway `sim`, or a run
harness that spawns `main()` as one process), with `write`/`get`/`run_once_sim` working in sim (not
raising). This yields a **timed Python golden straight from the `SeqTB`** — the thing that lets
`SimpFunHost` be deleted (its per-step logging role can stay as an optional "debug" `SimObj`, but the
default timing comes from the single-process `SeqTB`). `push`/`pop` become thin sync aliases of
`write`/`get` (or are retired).

## Stage 3 — the sequential-subset gate (first slice of `check_extractable`)

Statically reject what has no straight-line `int main()` lowering: `env.process(...)` fan-out and
interleaved TB↔DUT feedback, with a clear message ("this testbench is concurrent — it needs the SystemC
path, not C-sim"). Package as `check_extractable(func, *, sequential=False) -> (ok, err_msg)` (see
[[project-run-once-sim-and-seqtb-direction]]) so the same predicate powers documented contracts.

## Payoff (after the stages land) — separate follow-up

Rewrite `examples/regmap/simp_fun.py`'s TB to the clean single-process form and delete `SimpFunHost`;
optionally introduce an input/output `DataList` (needs DataList-in-TB file I/O — its own small codegen
item). Update the regmap docs to match.

## Verification (every stage)

- Run via the venv: `../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis"`; failures ⊆ the
  documented baseline (see [[project-test-baseline-failures]] — currently `dataschema_poly`×1 + `poly`
  timing×5 on this tree; `test_build` may or may not be present).
- **Byte-identical `*_tb.cpp` for all four example TBs at every stage** — capture before/after via
  `waveflow.build.hwgen.tb_files_to_str(<TBClass>)` and diff. This is the load-bearing gate.
- `ruff check` the touched files.
