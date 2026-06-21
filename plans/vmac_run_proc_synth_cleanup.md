# Task: clean up `VmacAccel.run_proc` toward synthesizability (sim-only marking, realistic poll, pre_sim)

Waveflow repo (`c:\Users\sdran\Documents\repos\pysilicon`, package `waveflow`, folder/venv
still named `pysilicon`). Focused refactor of one method. Read the whole brief first.

## Prerequisite / starting point

This builds on the **`vmac-compute-shell`** branch (the refactor that made `execute` a pure
golden + `vmac_compute` the memory-owning shell). That branch is **not yet merged to main**.
Start from it:

```
git checkout vmac-compute-shell && git checkout -b vmac-run-proc-synth
```

## Background

`run_proc` is the SimObj consumer loop and is the **extraction target** for HLS codegen
(`waveflow/build/hwcodegen.py`'s `HwStmtExtractor` parses `run_proc` by default). Today it
mixes real hardware control flow (dequeue → dispatch to `vmac_compute`) with **sim-only**
instrumentation (logging, `q_events`, `cmd_records`), a setup precondition (`raise`), Python
introspection (element geometry), an unrealistic poll interval, and the II-decoupling pad.

This task separates the two: hardware control flow stays in `run_proc`; everything sim-only
is moved to `pre_sim`, to `@sim_only` helpers, or into `vmac_compute` (whose body is not
extracted). This does NOT attempt to make `run_proc` actually extract to C++ end-to-end —
that needs `AXIMMQueue.get` to be synthesizable, which is a separate follow-on brief
(`plans/vmac_top_level_autosynth.md`). The goal here is the clean, correct shape + a
realistic poll, then re-simulate.

The mechanism for "not synthesized": `@sim_only` (`waveflow/hw/synth.py`) — the extractor
silently drops `@sim_only` call subtrees and reads that resolve to `@sim_only` callables.
`vmac.py` already imports `synthesizable` from there; add `sim_only`.

## Current `run_proc` (examples/vmac/vmac.py, ~line 476)

It currently, in order: raises if `cmd_queue is None`; caches `_mem_bw` / `_elem` /
`_elem_words` / `_elem_bytes`; computes `poll = 1.0/float(self.clk.freq)`; loops on
`cmd_queue.get`; appends `q_events` + logs on dequeue; breaks on `OpCode.end`; sets
`self._cmd_idx`; calls `vmac_compute`; applies the II-pad (`yield self.timeout`); appends
`cmd_records`.

## The 7 changes

**1. Move the `cmd_queue is None` precondition out of the loop.** Add a `pre_sim(self)` to
`VmacAccel` (it has none today; SimObj calls `pre_sim` before `run_proc`). Put the `raise
RuntimeError(...)` there. `pre_sim` is never synthesized, so the `raise` is fine there.

**2. Move element-geometry caching to `pre_sim`, and drop `_elem_words`.** Lines computing
`self._mem_bw` / `self._elem` / `self._elem_bytes` are one-time Python introspection
(`nwords_per_inst` → `serialize`) — not synthesizable, structural constants. Move them to
`pre_sim`. `_elem_words` is used ONLY to derive `_elem_bytes` (verified: no other reference)
— inline it: `self._elem_bytes = self._elem.nwords_per_inst(self._mem_bw) * (self._mem_bw //
8)` and delete the standalone `self._elem_words`.

**3. Make the poll a coarse structural parameter, computed once.** The current
`poll = 1.0/float(self.clk.freq)` is a per-call float division AND models a ring read every
single bus cycle (the consumer would saturate the AXI bus polling the queue). Replace with:
   - Add a structural `poll_cycles: HwParam[int]` to `VmacAccel` (default a coarse value —
     use **64**). Follow the existing `HwParam[int]` field declarations on the class.
   - In `pre_sim`, compute once: `self._poll = float(self.poll_cycles) / float(self.clk.freq)`
     (the one float op, in setup, not the loop).
   - Use `poll_interval=self._poll` in the `cmd_queue.get(...)` call.
   - Keep the host-side poll in `vmac_queue_sim.py` consistent if it sets `poll_interval` /
     `poll_interval` on the consumer (search for `poll_interval` / `1.0 / ... clk.freq` in
     `examples/vmac/vmac_queue_sim.py` and align it to the same coarse cycle count, or let it
     read `accel.poll_cycles`). Do NOT leave a 1-cycle poll anywhere in the VMAC path.

**4. `q_events.append(...)` + the dequeue `logger.log(...)` → a `@sim_only` helper.** Add
`@sim_only def _record_dequeue(self, cmd_idx, t): ...` containing both the `q_events.append`
and the dequeue `logger.log`. Call it from `run_proc`.

**5. The II-pad (`yield self.timeout`) moves into `vmac_compute`.** `vmac_compute` is
`@synthesizable(impl_file=...)`, so its body is not extracted — putting the pad there both
satisfies "move it" and makes it non-synthesized for free. Have `vmac_compute` capture its
own entry time `t_entry = self.now` at the top and, after the writeback, apply:
```python
if self.timing is not None:
    trips = n * math.ceil(m / self.pf)
    sched_secs = self.timing.cycles(trips) / float(self.clk.freq)
    pad = sched_secs - (self.now - t_entry)
    if pad > 0:
        yield self.timeout(pad)
```
`run_proc` calls `vmac_compute` immediately after dequeue (same sim tick), so `t_entry ==
dequeue_t` and per-command latency is unchanged. Remove the pad block from `run_proc`.

**6. `cmd_records.append(...)` → a `@sim_only` helper.** Add `@sim_only def
_record_command(self, cmd_idx, cmd, dequeue_t): ...` that computes `complete_t = self.now`
and appends the record (same fields as today: cmd_idx, op name, ab_eq, n_rows, n_cols,
dequeue_t, complete_t, latency). Call it from `run_proc` after `vmac_compute` returns.

**7. `cmd_idx` is sim-only — fold it into the record helpers.** It exists only to label the
`@sim_only` records and thread into `_record_txn`. Keep `self._cmd_idx` as an instance
counter that the `@sim_only` helpers maintain/read (e.g. `_record_dequeue` increments it),
so the synthesizable loop body doesn't carry the `cmd_idx += 1` / `self._cmd_idx = cmd_idx`
bookkeeping. `vmac_compute` already reads `self._cmd_idx` for its `_record_txn` calls — keep
that working (set it in `_record_dequeue` before `vmac_compute` runs).

### Resulting `run_proc` should be ~this shape

```python
def run_proc(self) -> ProcessGen[None]:
    """<keep a concise docstring: free-running queue consumer; per-command datapath is the
    synthesizable vmac_compute shell; sim-only bookkeeping is in @sim_only helpers / pre_sim>."""
    while True:
        cmd = yield from self.cmd_queue.get(self.Cmd, poll_interval=self._poll)
        dequeue_t = self.now
        self._record_dequeue(cmd, dequeue_t)          # @sim_only (also bumps _cmd_idx)
        if OpCode(int(cmd.op)) is OpCode.end:
            break
        yield from self.vmac_compute(cmd, self.m_mem)
        self._record_command(cmd, dequeue_t)          # @sim_only
```

(Exact threading of `dequeue_t` / `_cmd_idx` is your call as long as the sim-only data is in
`@sim_only` helpers and the loop body holds only hardware control flow + the `vmac_compute`
call. Note: this brief does NOT require the extractor to accept `run_proc` yet — that's the
follow-on. Just get the shape + markings right.)

## Verification

Use the venv explicitly (`../pysilicon-venv/Scripts/python.exe`):

```
../pysilicon-venv/Scripts/python.exe -m pytest tests/examples/test_vmac_golden.py tests/examples/test_vmac_numeric.py -q
../pysilicon-venv/Scripts/python.exe -m pytest tests/hw tests/examples tests/simulation -m "not vitis" --tb=no -q
../pysilicon-venv/Scripts/python.exe -m examples.vmac.vmac_queue_sim
../pysilicon-venv/Scripts/python.exe -m ruff check examples/vmac/vmac.py examples/vmac/vmac_queue_sim.py
```

**Baseline:** the non-vitis suite has known pre-existing failures (e.g.
`tests/hw/test_dataschema_poly.py` — missing `examples/stream_inband/poly.hpp`). A branch is
clean iff its failures are a subset of main's. Do not "fix" those.

**Behavior change — expected, this is the one that moves numbers.** The coarser poll changes
*when* commands dequeue, so the queue-sim timeline shifts. After the change, these
**invariants MUST still hold** (they are command-level / II-driven, independent of poll):
- `rho matches numpy reference: OK`
- `read-bus words : anorm(ab_eq)=16  abcorr=32  (anorm = half of abcorr: True)`
- `latency : anorm=<X> ns  abcorr=<X> ns  gap=0.0 ns` (anorm == abcorr; per-command latency
  unchanged by poll — it's the II schedule from `dequeue_t`).

These will **legitimately change** (record the new values; they become the new baseline):
- `sim drained at t = … ns` (coarser poll adds queue wait → larger).
- `queue depth peak` / occupancy.

If a committed timeline artifact is checked by a test or regenerated (search
`examples/vmac/timeline/` and any test asserting its contents), regenerate it and commit the
new baseline. If the per-command latency equality or the ab_eq 16/32 split breaks, something
in the pad move (#5) or the read suppression is wrong — fix before proceeding.

## Definition of done

- `pre_sim` holds the precondition + geometry; `_elem_words` deleted; `_elem_bytes` retained.
- `poll_cycles` is a structural `HwParam[int]` (default 64); `self._poll` computed once in
  `pre_sim`; no `1.0/float(freq)` poll anywhere in the VMAC path.
- `q_events` / dequeue-log in `@sim_only _record_dequeue`; `cmd_records` in `@sim_only
  _record_command`; `cmd_idx` maintained only in the sim-only helpers.
- II-pad moved into `vmac_compute`; gone from `run_proc`.
- `run_proc` body = while-loop + `cmd_queue.get` + dequeue record + `end` break +
  `vmac_compute` + command record. Per-command latency invariants hold; new drain/occupancy
  baseline recorded.
- All verification commands pass (failures a subset of main); ruff clean on the edited files.

## Wrap-up

- Commit on `vmac-run-proc-synth` (branched from `vmac-compute-shell`) in 1–2 scoped commits.
  End commit messages with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Do NOT push or merge.
- Summary: the changes, the NEW queue-sim headline block (so the human can eyeball the
  shifted drain/occupancy and confirm the invariants), and anything uncertain.
