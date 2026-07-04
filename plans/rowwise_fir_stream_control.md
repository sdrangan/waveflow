# Plan — simplify rowwise_fir: AXI-stream control (drop the AXI-MM command queue)

## Context

`rowwise_fir` (the shipped per-row FIR example) currently takes its command through a **sim-only
`AXIMMQueue` ring** (the `mmqueue` interface), while the synthesized kernel already takes the command
as `s_axilite` scalars. So the "command queue" is purely sim-side framing — and it **overloads** the
example: rowwise_fir's job is to teach the **load-compute-store dataflow** and **timing-model fitting**
(both novel, substantial), and the queue is a *third, orthogonal* control-plane concept that makes too
big a jump from the previous example (`shared_mem`).

**Decision (see `plans/example_sequence.md`): switch rowwise_fir to Option 1 — AXI-stream control,
reusing `shared_mem`'s pattern.** The command rides a `StreamIFSlave` (`s_in`), the response a
`StreamIFMaster` (`m_out`); data stays on AXI-MM. The AXI-MM command queue stays **vmac**'s concept.
rowwise_fir is reordered to come **before** `mmqueue` in the Examples progression.

**Key property that keeps this contained:** control is *orthogonal* to the dataflow and timing. The
load/compute/store stages, `FIRTiming`, the cosim calibration, the kernel hook, and the result figure
are **unchanged** — only the command *intake* (and its framing) changes. The calibration gates
(0.11 / 0.14 / 0.60%) and the bit-exact csim/cosim must come out identical.

## Environment / Vitis (READ FIRST — do not skip the build steps)

**Vitis HLS 2025.1 IS installed here and the project toolchain auto-detects it.** A previous run
wrongly concluded "Vitis not available" and skipped the build-side validation — that is a *false
negative*, and the validation gates (bit-exact csim/cosim, the 0.11/0.14/0.60% numbers) are
meaningless if the build is skipped. Specifically:

- **Do NOT check `which vitis_hls` / `PATH`.** The unified 2025.1 flow has no PATH'd `vitis_hls`; it is
  `vitis-run.bat`, which `waveflow/toolchain/toolchain.py` finds under `C:\Xilinx`. Verify:
  ```
  PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe -c "from waveflow.toolchain import toolchain; print(toolchain.find_vitis_path())"
  # -> C:\Xilinx\2025.1\Vitis\bin\vitis-run.bat
  ```
- **Run all Python via `../pysilicon-venv/Scripts/python.exe`** (a bare `python` may be system
  Python without the project deps). Run csim/cosim through **`fir_build.py`'s build steps** (which call
  `toolchain.run_vitis_hls_result`), not a PATH probe.
- **If Vitis errors, report the actual error output and stop** — never report "Vitis not available" or
  soft-skip. The toolchain finds the tool, so any failure is a real codegen/csim/cosim error to debug.

## The pattern to mirror: `shared_mem` (histogram)

`examples/shared_mem/hist.py` is the AXI-stream-control reference:
- ports: `self.s_in = StreamIFSlave(...)` (command), `self.m_out = StreamIFMaster(...)` (response),
  `self.m_mem` (AXI-MM data);
- `run_proc`: `cmd = yield from self.s_in.get(HistCmd)` → validate → read data over `m_mem` →
  compute → write → `respond(self.m_out, cmd.tx_id, status)`.
- `examples/shared_mem/hist_build.py` generates the matching top (AXI-stream control port + `m_axi`),
  with `hist_respond_impl.tpp` the response hook.

## Part A — code change (the real work)

1. **`examples/rowwise_fir/fir.py`** — replace the command queue with stream control, mirroring
   `HistAccel`:
   - Drop `self.cmd_queue` (`AXIMMQueue`), `poll_interval`, and the ring. Add `self.s_in`
     (`StreamIFSlave`) + `self.m_out` (`StreamIFMaster`).
   - `run_proc`: `cmd = yield from self.s_in.get(self.Cmd)` (instead of `self.cmd_queue.get(...)`),
     then forward `cmd` to the load stage exactly as today; send a response on `m_out` when the
     command completes (the `resp_sent` event maps to the `m_out` response).
   - **Do not touch** the three stage processes (`load`/`compute`/`store`), `FIRTiming`, `FIRCmd`'s
     fields, the `Region` slices, the golden, or the `dataflow` hook. `FIRCmd` already carries
     `x_off`/`h_off`/`y_off`/`n_rows`/`n_cols`; it now rides the stream instead of the ring.
   - Update the module docstring (the "AXIMMQueue ring is sim-only" note → "AXI-stream control,
     like shared_mem").

2. **`examples/rowwise_fir/fir_build.py`** — replace the `render_*` functions with **plain static
   files** (the FIR top/hpp/tb/tcl have no codegen-time parameters — they are fixed for this kernel,
   so building them from Python string-lists is needless indirection). Do this in **two sub-steps**,
   each validated separately:

   - **2a — pure refactor (byte-identical):** author the *current* generated top / hpp / tb / tcl as
     committed **static source files** (take the exact current `gen/fir.cpp` / `gen/fir.hpp` /
     `gen/fir_tb.cpp` / `gen/run.tcl` content verbatim), make the build's `codegen` step **copy** them
     into `gen/` — exactly the way `fir_dataflow.tpp` is already copied — and **delete**
     `render_top` / `render_hpp` / `render_tb` / `render_tcl`. **Plain static files: no templating, no
     substitution, no Jinja2.** Acceptance: `gen/*` is **byte-identical** to before and cosim is
     unchanged (same generated files ⇒ trivially same result).

   - **2b — the control change:** now edit the static `fir_top.cpp` directly (readable C++) — replace
     the `s_axilite` scalar command with an **AXI-stream control port** (+ a response), mirroring
     `shared_mem`'s generated top — and edit `fir_tb.cpp` to drive the command over the stream. Add a
     response hook if `shared_mem` uses one (`hist_respond_impl.tpp` → a `fir_respond_impl.tpp`
     analogue). Re-validate cosim **bit-exact**.

   (FIR becomes the first example with static-file codegen; mmqueue/shared_mem still render — an
   intended, transitional divergence, not an accident.)

3. **`examples/rowwise_fir/fir_sim.py`** — the host sends each `FIRCmd` over the stream (`s_in`) and
   reads the response from `m_out`, mirroring `shared_mem`'s host; remove the ring enqueue / the
   `MemComponent` command-ring layout. Data operands (X, h) and Y stay in the shared memory region.

### Validate the code change (must be behavior-preserving on the numbers + timing)
- **csim + cosim bit-exact** vs the golden (the dataflow/compute is untouched).
- **Calibration gates unchanged**: re-run `fir_calibrate.py` / the validation — the interior holdout
  (2,256) and untrained-n_col (4,128)/(4,512) gates stay **0.11% / 0.14% / 0.60%** and the committed
  `results/{cosim_grid,fir_calibration}.json` are unchanged (control timing does not enter the
  whole-kernel calibration; if it does, that's a bug to surface, not absorb).
- **`fir_figures.py --check`** still byte-identical (its data sources are unchanged).
- Full non-vitis suite = `main`'s known failures (zero regressions); `ruff` clean.

## Part B — docs change

The dataflow/timing/kernel/calibration pages barely move; the control reframe + reorder are the work.

4. **Reframe control in the example pages** (`docs/examples/rowwise_fir/`): remove every
   "command queue" / "mmqueue interface" / "reuses [mmqueue]'s command-queue" framing; the control is
   now **AXI-stream, like `shared_mem`** (the host sends the command over a stream, the accelerator
   `s_in.get`s it, responds on `m_out`). Pages that mention control: `index.md` (the **Mermaid system
   diagram** edge label `AXI-MM command queue` → `AXI-stream control`, and the intro), `fir.md` (the
   command), `pymodel.md` (`run_proc` now `s_in.get`). Pages on the dataflow/kernel/timing/cosim/fit
   are **unchanged** except any stray "queue" mention. **Also `kernel_hook.md`** references
   "`fir_build.py` `render_top`" for the generated top — update that to the **static `fir_top.cpp`**
   (per Part A #2).

5. **Drop "capstone"** from the rowwise_fir docs (`index.md`: "why it's the … capstone", "Why this is
   the capstone") — reframe as "the culmination of the load-compute-store + calibration arc" or simply
   describe what it adds. (Rationale: more complex examples will follow.) **Also soften
   `docs/examples/mmqueue/index.md`** — its "Why this is the capstone example" / "the last and most
   complete example" is now inaccurate.

6. **Reorder rowwise_fir before mmqueue:**
   - `docs/examples/rowwise_fir/index.md` `nav_order: 7 → 6`; `docs/examples/mmqueue/index.md`
     `nav_order: 6 → 7`.
   - `docs/examples/index.md`: move the `rowwise_fir` row **above** `mmqueue` in the Family-2 table;
     reframe its row — it introduces **no new interface** (reuses `shared_mem`'s AXI-stream control +
     AXI-MM data); its new concepts are the load-compute-store dataflow + cosim-calibrated timing. Keep
     the "different in kind" note (it's an internal-structure/timing step, not an interface step), now
     placed before mmqueue.

7. **Guide pointers** (`timing_model/double_buffered.md`, `custom_hooks/dataflow.md`,
   `calib/instrumentation.md`) already point at the example — leave them, just confirm none of their
   prose calls rowwise_fir a "command queue" example.

## Acceptance
- csim + cosim **bit-exact**; calibration gates **0.11 / 0.14 / 0.60%** unchanged; committed result
  JSONs unchanged; `fir_figures.py --check` byte-identical.
- Full non-vitis suite = `main`'s failure set (zero regressions); `ruff` clean on changed files.
- Docs: no "command queue" / "capstone" in the rowwise_fir pages; the example renders before mmqueue
  in nav; all links + anchors resolve (the `re`-based checker); the Mermaid diagram updated.

## Out of scope
- Migrating **vmac** to the rowwise dataflow architecture (a separate, bigger future task).
- The interconnect/crossbar full-duplex refactor ("B").

## Reference files
- Change: `examples/rowwise_fir/{fir.py, fir_build.py, fir_sim.py}`; **new static codegen files**
  (`fir_top.cpp` / `fir.hpp` / `fir_tb.cpp` / `run.tcl` authored alongside `fir_dataflow.tpp`,
  replacing the `render_*` functions) (+ a `fir_respond_impl.tpp` if mirroring `shared_mem`'s response
  hook); `docs/examples/rowwise_fir/*`, `docs/examples/index.md`, `docs/examples/mmqueue/index.md`.
- Mirror: `examples/shared_mem/{hist.py, hist_build.py, hist_respond_impl.tpp}` (AXI-stream control).
- Unchanged (cite as the orthogonal core): the dataflow/timing/calibration in `fir.py`/`fir_dataflow.tpp`/
  `fir_calibrate.py`/`results/*`.
