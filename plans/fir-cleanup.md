# Plan — rowwise_fir round 2: control-driven DATAFLOW kernel + generated top (+ other cleanups)

## 1. Control-driven command loop + DATAFLOW body + generated top — architecture "(A)"

### Why
The current rowwise_fir codegen **hand-writes the whole top** (`fir_top.cpp`) *including* the interface,
so it drifts from `FIRAccel`'s declared ports (it carries an implicit control surface the component
never declares), and it's bespoke (doesn't reuse the proven poly codegen shell).

### Goal — reuse the poly shell, generate the top, hand-write only the DATAFLOW body
The synthesized top should be **the existing poly codegen pattern** (`examples/stream_inband/gen/poly.cpp`):
an `ap_start`-gated `while(1)` command loop with `s_axilite` control/status regs, that reads a command
header, `return`s on `END`, runs the body, and on error sets `halted`/`error`/`tx_id` and `return`s
(restartable by the processor). The **only** change vs poly is the body call: instead of poly's extracted
impl, it calls a **hand-written** `fir_dataflow(...)` whose body is a `#pragma HLS DATAFLOW` region
(load / compute / store + internal stream buffers).

This is **control-driven** TLP (Vitis's `#pragma HLS DATAFLOW` model), **not** data-driven `hls::task`.
That was a deliberate correction (2026-06-24): the poly model — start on `ap_start`, loop, graceful
`END`-return, halt-register + return on error, restart — is the common case and is exactly what
`hls::task` (free-running, no block control, no halt-and-return) gives up. `hls::task` is reserved for a
genuinely-never-stops datapath (noted alternative, not this plan).

Decision **"(A)"**: `fir_dataflow` is a **codegen marker + hand-written hook**; `run_proc` keeps the
3-process SimPy **timing model** unchanged (the three DATAFLOW stages mirror the three processes
one-to-one). Unifying sim/synth *behavior* into one source is **"(B)", deferred**.

### Division of ownership (the whole point)
| piece | owner | source |
|---|---|---|
| the entire top `fir()` — `s_axilite` regs, `m_axi`, axis ports, the `while(1)` cmd loop, `END`→return, error→halt→return | **codegen** | the **existing poly shell**, mechanically from `FIRAccel`'s declared ports + status regs |
| `fir_dataflow(...)` signature | codegen ↔ hook **contract** | the ports/cmd as C++ params |
| `fir_dataflow.tpp` — the `#pragma HLS DATAFLOW` region (load/compute/store + internal buffers) | **hook (hand-written)** | the structure codegen can't infer |

### The contract (locked details)
- The command **header is read in the generated top** (like poly), so `END`/error → `return` stay at
  the top. `fir_dataflow(cmd, gmem, m_out)` receives `cmd` as a **read-only scalar**; `load` reads the
  bulk `X` from `gmem` via `cmd`'s element offsets.
- The error leaves the DATAFLOW region through a **depth-1 status stream / the return value — NOT a
  shared scalar** (DATAFLOW processes communicate only through streams):
  ```cpp
  ap_uint<8> fir_dataflow(const FirCmdHdr& cmd, data_t* gmem, hls::stream<resp_word>& m_out) {
      hls::stream<ap_uint<8>> err_s;
      { 
  #pragma HLS DATAFLOW
          hls::stream<data_t> ld2cp, cp2st;  hls::stream<FirCmdHdr> ld_meta, cp_meta;
          load(cmd, gmem, ld2cp, ld_meta);
          compute(ld2cp, ld_meta, cp2st, cp_meta);
          store(cp2st, cp_meta, gmem, m_out, err_s);
      }
      return err_s.read();
  }
  ```
  Generated top: `ap_uint<8> err = fir_dataflow(cmd, gmem, m_out); if (err) { error=err; tx_id=cmd.tx_id; halted=1; return; }`
- `FIRAccel` must **declare the poly status-register block** — `halted`, `error`, `tx_id` (`s_axilite`
  control outputs) — in addition to `s_in` / `m_out` / `m_mem`, so the top is fully port-derived.
- **Overlap granularity:** strong load-compute-store overlap *within* a matrix (across its rows — the
  dominant FIR win); a small fill/drain bubble *between* commands (header read sits outside the region).
  The sim's back-to-back model should reflect that per-command seam (not perfectly-continuous streaming).

### Environment / Vitis (READ FIRST)
Vitis HLS 2025.1 IS installed; the toolchain auto-detects it. **Do NOT check `which vitis_hls` / PATH**
(it's `vitis-run.bat`, found by `waveflow/toolchain/toolchain.py`; verify via
`toolchain.find_vitis_path()`). Run all Python via `../pysilicon-venv/Scripts/python.exe`. This item is
**build-validated** — csynth/cosim must actually run; if Vitis errors, report the real output, never
soft-skip.

### Phase 1 — ✅ DONE (2026-06-24) — DE-RISK sandbox (no Waveflow, no codegen) — GATED Phase 2
Validate the control-driven DATAFLOW pattern empirically first (mirrors the original Phase-1 FIR sandbox
discipline: *build the hand HLS first*). New kernel in `examples/rowwise_fir/sandbox/`, e.g.
`fir_dflow_sandbox.cpp`, structured as the **poly shell + `fir_dataflow` body** above (trivial compute,
or the real FIR core). Measure / confirm (csynth + cosim, real Vitis):
1. **The poly shell + DATAFLOW body synthesizes & cosims bit-exact** — `while(1)` + `s_axilite` control
   + `END`-return + halt-register-on-error, with `fir_dataflow` as a scoped `#pragma HLS DATAFLOW` region.
2. **Error out via the depth-1 status stream / return** works (store writes one `err`; top halts on it).
3. **`m_axi` full-duplex in the region:** `load` read ∥ `store` write on the **same** `gmem` bundle
   overlap; capture the per-matrix load/compute/store overlap in the cosim timeline.
4. **Back-to-back:** feed N commands; confirm continuous processing with the **per-command barrier**;
   capture the cosim N-command timeline — the streaming-throughput reference (the old "Gate 3").
- **Deliverable:** the working sandbox kernel + `sandbox/dflow_notes.md` recording the answers.

**Phase 1 RESULT (✅ all gates met, real Vitis 2025.1 — `examples/rowwise_fir/sandbox/fir_dflow_*`):**
csim + cosim **bit-exact** (single/two/three/clean-varying/error); error path **halts + restarts**
bit-exact; m_axi **full-duplex confirmed** in the control-driven region (load-read ∥ store-write, same
`gmem` bundle, II=1, no deadlock; absolute cosim cycles stall-inflated by Vitis 2025.1 random-stall, so
the structural/functional evidence is the claim). **Two findings that change Phase 2 (folded in below):**
- **(F1) Sticky R-status regs need kernel-side clear-at-entry.** `halted`/`error` are `RegAccess.R` — the
  host *cannot* write them, so only the kernel can clear them. The generated top **must init
  `halted=0; error=0` at entry** or a restart reads stale `1`. Poly's generated top doesn't (its flow
  never restarts after error) → **likely a latent poly issue too** (confirm; may warrant a codegen-general
  fix, not just FIR).
- **(F2) The per-command seam is a FIXED constant**, not accumulating: cosim single=1477, two=2944,
  three=4411 → **+1467 cyc/command exactly** (single = 1467 + ~10 one-time drain). The
  header-read-outside-the-DATAFLOW-region barrier *is* that seam — the concrete **Gate-3 streaming
  reference** for Phase 2's sim model (per-matrix latency **+ this fixed seam**).

### Codegen trace (DONE — 2026-06-24): the (A) path is the existing poly extraction path
The generated top is **not new codegen** — it is exactly how poly is generated. Confirmed:
- **`extract_kernel(comp)`** (`waveflow/build/hwcodegen.py:1265`) picks **`on_start`** as the kernel root
  iff the component has a **`VitisRegMapMMIFSlave`** endpoint (else `run_proc`). poly's `on_start`
  (`poly.py:203`) — the `while True:` command loop (`s_in.get` → `END`→`return` → call hook → on error
  `regmap.set(...)`→`return`) — IS the C++ `while(true)` shell.
- The `HwStmtExtractor` lowers that loop: `while True:`→`while(true)`, `yield from self.s_in.get(Schema)`
  → the **built-in `Schema::read_axi4_stream<bw>`** (⇒ **issue 2a solved for free**), the `@synthesizable`
  hook call (`evaluate`, with `_impl_file`, `hwcodegen.py:1054`) → `namespace::hook(...)` + the
  hand-written `.tpp` (poly: `poly_evaluate_impl.tpp`).
- **`kernel_signature`** (`waveflow/build/hwgen.py:694`) builds the top from the declared ports:
  streams→`axis`; `VitisRegMap` fields→`s_axilite … bundle=control`; **`MMIFMaster`→`ap_uint<bw>* …` +
  `m_axi … offset=slave`** (`hwgen.py:744` — already `ap_uint`, **issue 2b solved for free**); and the
  **control protocol auto-selects `s_axilite port=return` when a regmap is present** (`hwgen.py:754`) —
  the poly/`ap_start` model, with m_axi alongside. So the poly path already emits the *exact* top we want.

⇒ Phase 2 is **mostly component-side** (adopt the poly pattern); the codegen already supports it. The
hand-written `fir_top.cpp` goes away. Two things to verify (small): (i) the on_start→hook call passes an
**m_axi** master arg (poly's `evaluate` has none — its data is in-stream; FIR passes `m_mem`); (ii) the
`on_start`/`run_proc` coexistence below.

### Phase 2 — Waveflow integration (architecture A) — AFTER Phase 1
**Design (simplified 2026-06-24): unified `on_start` entry, concurrency inside the per-command hook.**
There is **no coexistence problem and no separate `run_proc`** — FIR is the *same shape as poly*. The
3-process timing lives **inside `fir_dataflow`'s sim body**, not in a persistent `run_proc`. Because
`@synthesizable(impl_file=)` makes a hook's **Python body the sim golden** (codegen uses the `.tpp`,
never the body — `structure.md`), `fir_dataflow`'s body is free to `self.process(load/compute/store)`;
codegen lowers only the *call*.

**STATUS (2026-06-24) — uncommitted (`fir.py`, `fir_sim.py`, `hwgen.py`):**
- **2a ✅ DONE (hard checkpoint met).** `fir.py`+`fir_sim.py` restructured to `on_start` + per-command
  `fir_dataflow` sim body; old persistent `run_proc`/stages removed; `FIRTiming` unchanged. Gates
  byte-identical: single-cmd **0.11/0.14/0.60%** (sim_whole 1341.48/1211.28/3651.05), golden + timeline +
  `fir_figures --check` + non-vitis suite (15 known) + ruff all green.
- **2b codegen ✅ (items 6, 8).** Generated top is exactly the poly shell — F1 init, `read_axi4_stream`,
  `ap_uint<32>* m_mem`, `fir_dataflow(cmd, m_mem, m_out)`. **Added m_axi-master hook-arg lowering to
  `hwgen.hook_signature`** (generic path lacked it; only VMAC topgen had m_axi) — poly regression-free.
- **2b RTL ⏳ REMAINING (own focused pass):** item 7 — write `fir_dataflow.tpp` (DATAFLOW region over
  `read_array_slice<W>`/`write_array_slice<W>` per §2b; err via depth-1 stream; response in store);
  rewrite `fir_build.py` to mirror `poly_build.py` (gen-include + `HlsCodegenStep(impl_dir=".")`, retire
  static `fir_top.cpp`); adapt TB to the new top; item 9 — csim+cosim bit-exact (single/multi/clean/
  error/restart) + confirm the ~1467-cyc seam. **Then untrack `gen/`** (`git rm --cached gen/` + add
  `gen/` to the local `.gitignore`).

1. **`fir.py`:**
   - Declare a **`VitisRegMap`** (`halted`/`error`/`tx_id` + any config) + **`VitisRegMapMMIFSlave(
     regmap=…, on_start=self.on_start)`** (the `s_axilite` control AND makes `extract_kernel` pick
     `on_start`). Keep `s_in`/`m_out` streams + `m_mem` (`MMIFMaster`).
   - **`on_start` = the single entry (sim *and* codegen)**, mirroring poly: clear status at entry (F1:
     `regmap.set(halted,0); regmap.set(error,0)`), then `while True: cmd = yield from self.s_in.get(
     FIRCmd); if cmd.op==END: return; err = yield from self.fir_dataflow(cmd, self.m_mem, self.m_out);
     if err: regmap.set(halted/error/tx_id); return`. In sim it's launched by the host's `ap_start`
     (like poly's host); commands process **sequentially** — which matches F2's fixed per-command seam.
   - **`fir_dataflow` = `@synthesizable(impl_file="fir_dataflow.tpp")`**, returning `err`. Its **sim body**
     spawns the three per-command stage processes (`self.process(load/compute/store)` over **this
     matrix's rows**), waits on store's completion, returns the latency/err — modeling the intra-matrix
     load-compute-store overlap. Its **codegen** is the `.tpp` DATAFLOW. `FIRTiming` unchanged; the three
     stage procs restructure from persistent (`while True` over queues) → per-command (process one matrix,
     finish). **No `run_proc` for kernel logic** (poly's documented model — now FIR fits it cleanly).
2. **codegen wiring (`fir_build.py`):** adopt poly's `HlsCodegenStep(comp_class=FIRAccel, impl_dir=".")`
   `gen_kernel` step (the extractor + `kernel_signature` do the rest). Retire the static `fir_top.cpp`
   (the top is now generated). Move the FIR build onto the poly-style DAG steps as needed.
3. **`fir_dataflow.tpp`:** the scoped `{ #pragma HLS DATAFLOW … }` region (load/compute/store over
   `ap_uint<W>*` via `read_array_slice`/`write_array_slice` per §2b), returning `err`; fold
   `fir_respond_impl.tpp` into the `store` stage's `m_out` write. **Start from the validated Phase-1
   kernel** (`sandbox/fir_dflow_sandbox.cpp`) and its recorded canonical-form rules: `err_s` (depth-1)
   declared **outside** the `{ #pragma HLS DATAFLOW }` block and read **after** it; inter-task channels
   declared **inside**; per-command metadata (incl. the bad-size→`n_rows=0` guard) rides its **own**
   stream so stages stay balanced; taps need their **own** load→compute channel (keep `compute` off `gmem`).
4. **host (`fir_sim.py`):** poly-style — write `ap_start`, stream N commands, drain N responses; reads
   `halted`/`error`/`tx_id` on a halt (like poly's host). (No longer "never assert ap_start" — the sim
   *is* `on_start`-launched now.)

### Validation (Phase 2)
- **csim + cosim bit-exact** vs the golden (single + back-to-back).
- **The Gate-3 number becomes real (per F2):** the sim's back-to-back model = per-matrix latency **+ a
  fixed per-command seam** (the header-read barrier; Phase-1 measured **+1467 cyc/command, constant**).
  Validate the sim against the cosim N-command timeline; the seam is a fit constant, not accumulating.
- **Single-command latency gates (0.11 / 0.14 / 0.60%):** confirm they hold, OR re-derive them for the
  new kernel — the timing model may shift; **surface any change, do not absorb it.**
- Full non-vitis suite = `main`'s failure set; `ruff` clean; committed `results/*` + figure handled
  per the timing re-derivation.

### Out of scope → "(B)", later
- Auto-generating `fir_dataflow`'s **body** (the stages) from `run_proc` — the sim/synth behavioral
  unification.
- Generalizing the generated-poly-shell-calling-a-hook path to the other examples (do it once here).
- The `hls::task` data-driven variant (only for a genuinely-never-stops datapath).

---

## 2. Use Waveflow's built-in serialization (don't hand-roll) — two coupled corrections

The current hand-written `fir_top.cpp` + `fir_dataflow.tpp` **bypass the framework's built-in
serialization** in two ways. Both are largely *subsumed by* section 1 (the generated poly shell uses the
built-ins), but are called out so the section-1 rewrite honors them and the `.tpp` is fixed.

### 2a. Command deserialization — use the built-in, not 7 hand-rolled `s_in.read()`s
`fir_top.cpp` manually deserializes the command:
```cpp
ap_uint<32> op = s_in.read(); ap_uint<32> tx_id = s_in.read(); int x_off = (int)s_in.read(); ...
```
Poly's generated shell instead uses the **built-in** DataSchema stream-deserialize:
`PolyCmdHdr cmd_hdr; cmd_hdr.read_axi4_stream<32>(s_in);`. FIR should use the generated `FIRCmd`
struct's `read_axi4_stream<32>` (one call, schema-derived field order/widths), not hand-rolled word
reads + casts. **Automatic once section 1 reuses the poly shell** — recorded here as a requirement.

### 2b. Memory pointer must be `ap_uint<mem_dwidth>*`, not typed `real_t*` — built-in serdes + addressing
Convention: m_axi memory access is **always raw words `ap_uint<mem_dwidth>`**, never a C++ type like
`real_t`. The current top passes `real_t* gmem` and does raw pointer arithmetic (`gmem + x_off`), which
**bypasses serialization** and only passes cosim *by coincidence* (`real_t` is 32-bit = one bus word; it
breaks for any element whose width ≠ the bus word — complex / fixed / sub-word packing). *(Already a
known follow-fix in memory: `project-matrix-lt-fir-build`.)* Fix: the generated top signature is
`ap_uint<mem_dwidth>* gmem`, and the `.tpp` load/compute/store use the **built-in element-coordinate
slice family** (`read_array_slice<W>` / `write_array_slice` + the framework's element→byte/word address
computation + serialize/deserialize — the same packing-aware idiom used elsewhere), not typed pointer
math. **Constrains section 1's `fir_dataflow.tpp` rewrite:** the load/store stages move to
`read_array_slice` / `write_array_slice` over `ap_uint<W>*`.

Net: the FIR kernel uses **one serialization story** (consistent with the rest of the framework) and is
correct for non-float element types.

## 3. Other corrections (to add)

> *(Placeholder — add any further cleanups here; I'll fold each into the plan.)*
