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

> **Stage 1 DONE** (`422de26`, pushed): the extractor accepts a yielding `main()`; `run_once_sim` /
> `write` / `get` lower byte-identical to `run_once` / `push` / `pop`.

## Stage 2 — `SeqTB` runnable as one process (the timing payoff)

Decided design (all four D-questions settled with the user):

- **D1 — in-process timing via `@sim_only`.** The `SeqTB` gets `@sim_only` timer methods
  (`_start_timer` / `_stop_timer_and_log`) that read `self.now` and log **inside** them, so `main()` only
  ever makes bare `@sim_only` *calls* — which the extractor already strips (`_visit_stmt_tb` falls
  through to `_visit_expr_stmt`'s `@sim_only` skip). **No extractor change needed.** `main()` never
  bare-reads `self.now`.
- **D2 — ambient sim.** Add a "current `Simulation`" context (a `contextvar`) that
  `Component.__post_init__` picks up when no `sim=` is passed. So `dut = SimpFunComponent()` in `main()`
  is identical for both modes (codegen: no sim; run: the ambient sim). *This is the load-bearing new
  mechanism — keep it small and well-tested.*
- **D3 — regmap-only.** Only `run_once_sim` needs to run in sim; **do not** make stream `write`/`get`
  runnable yet (that's a later sub-stage for `poly`).
- **D4 — demote `SimpFunHost`.** Default timing comes from the `SeqTB`; keep `SimpFunHost` present but
  unused-by-default (deletable later). Per-step timing is not needed — the VCD carries it.

**Scope (i):** runnable + timing + retire `SimpFunHost` from the default flow. **Leave the input
`regmap.get(...)` round-trip** in the `simp_fun` TB for now — removing it needs read-into-local or the
`DataList`-in-TB primitive, which is a **separate follow-on** (Stage 2b).

Concrete steps:

1. **Ambient sim** — `contextvar` in `waveflow/simulation/simulation.py` (a `@contextmanager` on
   `Simulation`, e.g. `with sim.as_current():`), read by `Component.__post_init__` (or `SimObj`) when
   `sim is None`. Existing explicit-`sim=` callers unaffected.
2. **Runnable `SeqTB`** — a harness (a `SeqTB.run()` method and/or a `PySimStep` path) that: creates a
   fresh `Simulation`, enters `as_current()`, spawns `main()` as **one** process, runs to completion,
   and exposes `self.now` to the `@sim_only` timers. `main()`'s `yield from dut.run_once_sim(...)` drives
   `on_start` (which yields its latency), advancing the clock.
3. **`simp_fun` TB rewrite** — `SimpFunTBHls.main()` → read inputs (regmap file I/O, unchanged) →
   `_start_timer()` → `y = yield from dut.run_once_sim(dut.regmap.get("x"), ...)` → `_stop_timer_and_log()`
   → `write_status_json`. **Byte-identical `*_tb.cpp`.**
4. **`PySimStep` swap** — run the `SeqTB` for the functional golden **and** `py_timing` (total
   transaction cycles from the timer); stop using `SimpFunHost` in the DAG (leave the class in the file).
5. **Verify** — byte-identical `*_tb.cpp` for all four TBs; **re-run `simp_fun_build.py --through
   validate_timing --force`** (Vitis) and confirm PASS (the new `py_timing` vs RTL within the ±4
   tolerance — expect a small shift, that's fine); full `pytest -m "not vitis"` ⊆ baseline.

## Stage 2b (follow-on) — clean the input

Add the codegen primitive to read a `uint32` file into a plain local / a `DataList` input struct in a TB,
so `run_once_sim(inp.x, inp.a, inp.b)` drops the `regmap.get(...)` round-trip and the example reads the
way the user sketched. Its own bounded item.

> **Stage 2b DONE**: the input round-trip is gone. `x = Int32().read_uint32_file(path)` — the **standard
> schema file-IO spelling** (`DataSchema.read_uint32_file`, the same `PolyCmdHdr().read_uint32_file(p)` a
> build script uses), so it needed **no new runtime**: a run of `main()` gets a real `Int32` back and
> `run_once_sim(x, a, b)` passes it straight in. In codegen the local **aliases the input field's C++
> local** — the input-side mirror of the Stage-1 output capture (`y = yield from run_once_sim(...)`) —
> so it lowers to the same `TbRegmapFileReadStmt` the round-trip emitted and **all four `*_tb.cpp` stay
> byte-identical** (`simp_fun` included; the round-trip was only ever a Python-side detour). Vitis
> `--through validate_timing --force` PASSES (`pass:true`, py 4 vs cosim 5 cyc). v1 scope: the local must
> name an input regmap field (`RW`/`W`) of a bound DUT and carry that field's schema — a **free-standing**
> local (naming no field) would need its own C++ decl plus an arg-carrying `KernelCallStmt`, as would a
> `DataList` input struct; both remain follow-ons.

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
