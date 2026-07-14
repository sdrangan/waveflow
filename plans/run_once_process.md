# Plan: single-process `run_once` and `on_start`

## Context

`run_once` today is **synchronous** — it calls `on_start()` directly, returns a value, and *raises* if
`on_start` yields ("sim-driven invocation is a follow-on"). That means a host-activated kernel's Python
model can produce a functional result but **no cycle timing**: the transaction latency currently lives in
the *testbench* (`SimpFunHost.run_proc` does `yield self.timeout(latency_cycles * clk.period)` before
polling), not in the kernel.

We want the invocation to be a **process** so a caller can time it:

```python
t0 = self.now
y  = yield from dut.run_once_sim(x, a, b)   # advances the sim clock through on_start
t1 = self.now                               # total transaction latency
```

This is the first concrete step toward the larger goal (a single-process, no-synchronous-shadow
testbench model — see the related note at the end). The key enabler already exists:
`VitisRegMapMMIFSlave._launch` (`regmap.py:574`) already drives a yielding `on_start`
(`result = self.on_start(); if result is not None: yield from result`), so the framework is ready — the
gap is (a) a sim-driven invocation entry point and (b) moving the latency into the kernel.

## Phase 1 — `run_once_sim` (additive, low-risk) — DO THIS FIRST

Add a **generator** invocation alongside the synchronous `run_once`, in
[`waveflow/hw/hw_hostactivated.py`](../waveflow/hw/hw_hostactivated.py):

```python
def run_once_sim(self, *args: Any) -> ProcessGen[Any]:
    """Sim-driven mirror of run_once: drives on_start through SimPy so the caller can time it.
    `y = yield from dut.run_once_sim(x, a, b)`."""
    regmap, inputs, outputs = self._invoke_fields()   # factored from run_once (see below)
    if len(args) != len(inputs): raise TypeError(...)
    for name, value in zip(inputs, args):
        regmap.set(name, value)
    result = self.on_start()
    if result is not None:
        yield from result        # <-- drives a yielding on_start; advances the clock
    outs = [regmap.get(n) for n in outputs]
    return outs[0] if len(outs) == 1 else tuple(outs)
```

- **DRY:** factor the field-derivation (find the `VitisRegMapMMIFSlave`, walk `regmap._fields` skipping
  `is_vitis_auto`, `inputs = RW|W`, `outputs = R`, the stream-bearing guard) out of the existing
  `run_once` into a private `_invoke_fields(self)` and have both call it. Keep `run_once`'s behavior
  byte-identical.
- **Keep `run_once` synchronous and unchanged** — it stays the functional / codegen-mirror path (a
  `SeqTB.main()` extracts `dut.run_once(...)` from the AST → the C++ call; it never *runs* it).
- `run_once_sim` works whether `on_start` yields (drives it) or is synchronous (`result is None`, skips
  the `yield from`), so it is safe to add before Phase 2.

**Tests** (`tests/hw/test_hw_hostactivated.py`): with `simp_fun`'s current synchronous `on_start`,
`run_once_sim` returns the same values as `run_once` (drive it inside a throwaway `Simulation` via a small
process); and with a *yielding* fixture `on_start` (`yield self.timeout(...)`), `t1 - t0 > 0`.

## Phase 2 — `on_start` as a process (move latency into the kernel)

Make the kernel model its own latency instead of the testbench pre-waiting.

- **`examples/regmap/simp_fun.py`** — `SimpFunComponent.on_start` yields the latency:
  ```python
  def on_start(self):
      self._log("kernel_busy", 1)
      yield self.timeout(self.latency_cycles * self.clk.period)   # model compute latency here
      self.regmap.set("y", self.compute(...))
      self._log("kernel_done", 1)
  ```
  and **remove** the artificial `yield self.timeout(...)` from `SimpFunHost.run_proc` (`simp_fun.py:143`)
  so the host genuinely polls until `ap_done` flips.
- **Codegen constraint:** `on_start` *is* the extracted kernel body for a `HostActivated`. The timing
  `yield` must be **`@sim_only`** so the kernel extractor strips it (C-sim is untimed). **Verify
  `self.timeout` is treated as sim-only** by `_validate_no_implicit_capture` / the extractor; if not,
  that's a small fix (mark it `@sim_only`). The generated `simp_fun` kernel C++ must stay
  **byte-identical**.
- **Tests:** the synchronous `run_once` now *raises* on `simp_fun` (its `on_start` yields), so migrate
  the functional `run_once` unit tests (`test_run_once_*`) to `run_once_sim` inside a `Simulation`.
  Re-validate the timing expectation (`transaction_cycles == 5` in `test_regmap_simp_fun.py`) — the total
  should be preservable; adjust the expected value only if the poll-granularity genuinely shifts it, and
  say so.

> Phase 2 is the riskier half (touches timing + codegen + tests). If the byte-identical kernel or the
> timing re-validation can't be kept clean, **stop and report** rather than forcing it — Phase 1 stands
> on its own.

## What this buys / doesn't

- **Buys:** `yield from dut.run_once_sim(...)` gives **total** transaction timing in one call, and (Phase
  2) the latency lives in the kernel where it belongs.
- **Doesn't:** *per-step* timing (each `ap_start`/busy/`ap_done` event) still needs the explicit host
  (`SimpFunHost`) that logs each event — that stays the "DebugTB" role.

## Related / future (separate item) — `check_extractable`

There is **no** extractability predicate today; the checks live inside the extractor and
`raise SynthesisError`. A useful consolidation:

```python
ok, err_msg = check_extractable(func, *, sequential=False)
```

- Wraps the extractor's existing checks (implicit `self.X` capture, non-`@synthesizable` calls, forbidden
  ops) into a **boolean predicate with a user-readable message** instead of a raised exception.
- `sequential=True` adds a **new "single process" gate**: reject `env.process(...)` fan-out and
  interleaved TB↔DUT feedback that have no straight-line `int main()` lowering (the Flow 2 vs Flow 3
  boundary — sequential → C++, concurrent → SystemC).
- Enables **documented synthesizability contracts**, e.g. *"A `HostActivated` synthesizes to a standalone
  Vitis kernel iff (a) it has no sub-components / internal interfaces, and (b) its `on_start` is
  `check_extractable(sequential=True)`."* — and the same predicate powers a clear up-front error when a
  component can't be lowered.

This is the groundwork for the longer-term unification (a single-process `SeqTB` using `write`/`get`
everywhere, timing yields stripped by codegen, concurrency routed to SystemC) — **out of scope here**,
noted so it's on record.

## Verification

- Run via the venv: `../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis"`. Branch is clean iff
  failures ⊆ the 15-test baseline (test_build×9, dataschema_poly×1, poly timing×5).
- Phase 1: new `run_once_sim` tests pass; `run_once`/codegen untouched.
- Phase 2: `simp_fun` kernel C++ **byte-identical** (a quick `kernel_files_to_str` diff); timing test
  re-validated; migrated `run_once` tests pass.
