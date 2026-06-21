# Brief: auto-synthesize the VMAC top — extract `run_proc`, make the command queue synthesizable

Waveflow repo (`c:\Users\sdran\Documents\repos\pysilicon`, package `waveflow`, folder/venv
still `pysilicon`). This is the larger follow-on to the VMAC refactor that already landed on
`main` (pure `execute` golden + memory-owning `vmac_compute` shell + clean `run_proc`).
**Phase 0 (design) is DONE — the decisions below are locked.** Branch from `main`:

```
git checkout main && git checkout -b vmac-top-autosynth
```

## Goal

Today VMAC's synthesizable top is hand-rolled f-strings in `examples/vmac/vmac_build.py`
(`render_top` → `void vmac(gmem, ...scalars...)` calling `vmac_compute_core`; `render_cosim_tb`).
Make VMAC's consumer loop **auto-extract** to HLS C++ via the framework's
`HwStmtExtractor` (which parses `run_proc` by default), the same path the histogram uses.
End state: a generated top + TB, conformance-checked bit-exact, replacing the f-strings.

## Locked design decisions (Phase 0 outcome)

**D1 — Kernel = `run_proc`, NOT `on_start`.** The `on_start` vs `run_proc` choice is the
**regmap-controlled vs no-regmap** axis: poly uses `on_start` (VitisRegMap + host ap_start);
hist is *"`run_proc` (stream-controlled, no regmap)"* ([hist.py:22](examples/shared_mem/hist.py#L22),
kernel body at [hist.py:335](examples/shared_mem/hist.py#L335)). VMAC has **no regmap**
(mm-queue-driven), so it follows hist: the kernel stays `run_proc`. Do **not** add `on_start`.
(VMAC's `while True`-until-`OpCode.end` loop body is fine and synthesizable — poly's `on_start`
body is itself a `while True … return-on-END`; the loop shape is independent of the regmap axis.)

**D2 — Make `AXIMMQueue.get` synthesizable; same call syntax as hist.** No `get_one`. The
existing typed single-element path (`get(schema_type)` with no `count` → one deserialized
instance, [aximm_queue.py:367](waveflow/hw/aximm_queue.py#L367)) becomes synthesizable, so
`run_proc` reads exactly like hist:
```python
cmd: VmacCmd = yield from self.cmd_queue.get(self.Cmd)     # cf. hist: self.s_in.get(HistCmd)
```
Mirror `StreamIFSlave.get`, which is already `@synthesizable(synth_fn=_not_implemented_synth,
stmt_class=StreamGetStmt)` ([interface.py:496](waveflow/hw/interface.py#L496)).

**D3 — Poll moves onto the queue; off the call site.** Set the poll interval on the queue in
`pre_sim` (e.g. `self.cmd_queue.poll_interval = self._poll`, derived from the structural
`poll_cycles`) so the call is the bare `get(self.Cmd)` (no `poll_interval=` kwarg), matching
hist's blocking `.get`. In the synthesizable C++ the poll lives inside the dequeue hook.

**D4 — Recognize `AXIMMQueue` as a synthesizable resource.** hist's `self.s_in` is an
endpoint, so the extractor allows `self.s_in.get(...)`. VMAC's `self.cmd_queue` is an
`AXIMMQueue` proxy over `m_mem`; the extractor's read rule currently allows only endpoints /
RegMap / `@sim_only` (`hwcodegen.py` ~line 163-175). Teach it to recognize `AXIMMQueue` the
same way it recognizes RegMap. (Keep the attribute name `cmd_queue` — a rename to a named
queue interface is optional and out of scope.)

**D5 — `get` lowering = hand-written C++ hook (Strategy A).** Give `get` a
`@synthesizable(stmt_class=AXIMMQueueGetStmt)` whose C++ is a hand-written ring-dequeue
(mirrors the `vmac_compute_impl.tpp` hook pattern), over the `AXIMMQueueLayout`
([aximm_queue.py:49](waveflow/hw/aximm_queue.py#L49)): word0=head, word1=tail, word2=capacity,
data slots after. The dequeue: read `(head,tail)` (one m_axi read), poll while `head==tail`,
read one slot (`elem_words` words) at `slot_addr(head)`, advance `head=(head+1)%capacity`
(m_axi write), deserialize → `VmacCmd`. (`% capacity` is a mask when capacity is a power of
two; document/assert that, like the `.tpp`'s `elem_to_word`.)

**D6 — Generated top reads the command from memory — this AVOIDS the nested-struct pitfall.**
`render_top` hand-unpacks the command into scalar s_axilite registers *because* a `VmacCmd`
struct mis-decomposes at csynth across the top boundary. In the mm-queue model the command is
read from memory by the dequeue hook (a local), never crossing the s_axilite boundary — so the
generated top's only ports are the `m_axi` gmem (+ ap_ctrl). Cleaner than the hand-rolled top.

**D7 — Codegen via the hist path; retire the f-strings last.** Generate the kernel the way
hist does — `kernel_to_cpp(HistAccel)` / `header_to_cpp(HistAccel)`
([hist_build.py:594](examples/shared_mem/hist_build.py#L594)) — applied to `VmacAccel`
(extracting `run_proc`), wired as a build step (cf. poly's `HlsCodegenStep`). Generate
**alongside** `render_top`/`render_cosim_tb` first; cosim both; retire the f-strings only once
the generated top passes bit-exact. `execute_mem` (the conformance/cosim vector generator)
stays as-is.

## Status: Phases 1–2 DONE and merged-pending (branch `vmac-top-autosynth`)

Phases 1–2 are complete and reviewed; the extracted `run_proc` IR was approved. This run does
**Phases 3–4** on the same branch (`vmac-top-autosynth`, off `main`). Continue from there
(`git checkout vmac-top-autosynth`).

**Seam resolution (decision 2, corrected after review):** the generated command struct already
provides the m_axi-word unpack `read_array<word_bw>(const ap_uint<word_bw> x[])` (the method
the csim TB uses: `cmd.read_array<32>(cmdw)`) — it is NOT stream-only. So the hook's flagged
`out.unpack(slot)` becomes **`out.read_array<MEM_BW>(slot)`**. The only prerequisite: that
`read_array` is specialized only for `word_bw ∈ {32, 64}` today; it must be generated for the
ring's `MEM_BW` (the accelerator `mem_dwidth`, e.g. 16/32/64 in the tput configs). Extend the
command `DataSchemaStep`'s `word_bw_supported` to include the mem width(s) the generated top
uses. No new codegen entry point.

### Phase 1 — make the queue dequeue synthesizable [DONE]
- Add `AXIMMQueueGetStmt` (an `HwStmt` subclass; model on `StreamGetStmt` /
  `RegMapGetStmt` in `waveflow/hw/hwstmt.py`) + decorate `AXIMMQueue.get` with
  `@synthesizable(stmt_class=AXIMMQueueGetStmt)` for the typed single-element path.
- Hand-write the C++ ring-dequeue hook (D5), in the VMAC example's hook style (a `.tpp` or
  the codegen impl-file convention the extractor expects for a stmt-class hook — check how
  `StreamGetStmt`/`RegMapGetStmt` emit their C++).
- Teach the extractor to recognize `AXIMMQueue` as a resource (D4).
- Move the poll onto the queue (D3); update `run_proc`'s call site to `get(self.Cmd)`.
- Unit-test the lowering (mirror `tests/hw/test_hwstmt.py` and the regmap stmt tests).
- Sim must still pass: golden/numeric tests + `vmac_queue_sim` headline invariants unchanged
  (rho OK; ab_eq 16/32; per-command latency anorm == abcorr; drain ≈ the post-coarse-poll
  baseline). The `get(self.Cmd)` + poll-on-queue change must not move the sim numbers.

### Phase 2 — make `run_proc` extract [DONE]
- Resolve the remaining extractor read sites in `run_proc`: `self.cmd_queue` (now recognized),
  `self.m_mem` (endpoint), `self.Cmd` (schema-type arg — see how hist passes `HistCmd` to
  `s_in.get`), and the `dequeue_t = self.now` line (sim-only — move the `self.now` capture into
  the `@sim_only _record_dequeue` and have `_record_command` read the stashed value, so
  `run_proc`'s body has no `self.now`).
- Confirm `HwStmtExtractor(comp).extract()` succeeds on `VmacAccel.run_proc`; add an
  extraction test alongside `tests/hw/test_extract_poly.py`.

### Phase 3 — generate the kernel + TB; wire the deserialize seam
- Wire the ring-dequeue hook's deserialize: replace `out.unpack(slot)` in
  `waveflow/build/aximm_queue_impl.tpp` with `out.read_array<MEM_BW>(slot)` (the existing
  generated word-array unpack). Generate the command struct's `read_array` for the ring
  `MEM_BW` by adding the mem width(s) to the command `DataSchemaStep`'s `word_bw_supported`
  (today `[32, 64]`; the tput configs use `mem_dwidth ∈ {16,32,64}`).
- Generate the kernel via the hist path: `kernel_to_cpp(VmacAccel)` / `header_to_cpp(VmacAccel)`
  (cf. [hist_build.py:594](examples/shared_mem/hist_build.py#L594)), wired as a build step in
  `vmac_build.py` (cf. poly's `HlsCodegenStep`). The generated top's only ports are the `m_axi`
  gmem + ap_ctrl (D6 — the command is read from the ring in memory, not s_axilite scalars; this
  sidesteps the nested-struct pitfall that forced `render_top` to unpack scalars). Wire the
  `#include "aximm_queue_impl.tpp"` + the `complex_utils.hpp`/`vmac_compute_impl.tpp` includes
  into the generated kernel; confirm the `queue_get<CmdT, MEM_BW, BASE, CAP, EW>` template ABI.
- Generate the cosim TB: build the ring image in memory (head=0, tail set, capacity, one
  command slot + an `END` slot via the producer-side serialize), lay the A/B operands, invoke
  the generated top (it drains until `END` and returns), and check the Y region against
  `execute_mem`'s golden image. Reuse `execute_mem` for the expected vectors — do NOT add a new
  golden.
- Generate **alongside** `render_top`/`render_cosim_tb` first (don't delete them yet).

### Phase 4 — Vitis verify, then retire the f-strings
- Vitis csim + cosim the **generated** top; assert **bit-exact** against `execute_mem`'s golden
  (the existing `--through csim` conformance must still pass; add the generated-top cosim).
- **GUARD:** only if the generated top is bit-exact, retire `render_top` / `render_cosim_tb` and
  switch `vmac_build.py` to the generated path. If cosim is NOT bit-exact, STOP, leave the
  f-strings in place, and report — the golden is the spec; fix the kernel/hook, never loosen
  the compare.

## Verification

Venv explicitly (`../pysilicon-venv/Scripts/python.exe`). Non-Vitis first:
```
../pysilicon-venv/Scripts/python.exe -m pytest tests/hw/test_hwstmt.py tests/hw/test_extract_poly.py tests/hw/test_aximm_queue_stmt.py tests/hw/test_extract_vmac.py -q
../pysilicon-venv/Scripts/python.exe -m pytest tests/examples/test_vmac_golden.py tests/examples/test_vmac_numeric.py -q
../pysilicon-venv/Scripts/python.exe -m pytest tests/hw tests/examples tests/simulation -m "not vitis" --tb=no -q
../pysilicon-venv/Scripts/python.exe -m examples.vmac.vmac_queue_sim
```
Then Vitis (installed here — 2025.1; verify empirically, watch for soft-skips):
```
../pysilicon-venv/Scripts/python.exe -m examples.vmac.vmac_build --through csim      # existing conformance still bit-exact
# + the new generated-top cosim step you add (csim/csynth/cosim of the generated `vmac` top)
```
Baseline: non-vitis suite has known pre-existing failures (e.g. `test_dataschema_poly` —
missing `examples/stream_inband/poly.hpp`); a clean branch's failures must be a subset of main's.

## Definition of done (this run)
- The ring-dequeue hook deserializes via `read_array<MEM_BW>`; the command struct's `read_array`
  is generated for the ring `MEM_BW`.
- A generated VMAC kernel + cosim TB exist (hist `kernel_to_cpp` path), wired into `vmac_build.py`.
- Vitis csim + cosim of the **generated** top pass **bit-exact** against `execute_mem`'s golden.
- `render_top`/`render_cosim_tb` retired (only if bit-exact); `vmac_build.py` uses the generated
  path. Existing `--through csim` conformance still bit-exact.
- All non-vitis tests pass (failures a subset of main); ruff clean.

## Wrap-up
- Continue on branch `vmac-top-autosynth`, scoped commits, end messages with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Do NOT push or merge.
- Summary: what changed; paste the generated kernel top (the `vmac(...)` C++ signature + body)
  and the Vitis csim/cosim bit-exact result; whether you retired the f-strings; anything
  uncertain — especially the `queue_get` template ABI wiring or any deserialize-width issue.
