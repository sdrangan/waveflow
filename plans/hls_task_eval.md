# Plan — `hls::task` viability toy (de-risk the free-running synthesis target)

## Why
We're weighing **`hls::task`** (data-driven TLP) as Waveflow's free-running synthesis target — over
`#pragma HLS DATAFLOW` (control-driven) — because: (a) it matches the sim's **persistent-process** model;
(b) it sidesteps the DATAFLOW **canonical-form deadlock class** — the TLAST early-return that hung the FIR
integration in cosim; (c) it's the **SALSA tile contract** (`hls::task` modules + `axis` doorbells + `m_axi`
payload). But the **gating unknown — does `m_axi`-master-from-`hls::task` even cosim in 2025.1 — is
unverified**, and I've refused to assert it without a cosim. This toy answers it *before* any architecture
commitment. Cheap, decisive, same de-risk discipline as everything else.

## Location & nature
**`experiment/hls_task/`** — `experiment/` is already gitignored, so Vitis build artifacts stay out of git
automatically and **nothing is committed**. **Trivial compute (vector scale/copy), NOT the FIR, NOT
Waveflow framework code** — pure hand-written Vitis HLS to isolate the `hls::task` + `m_axi` + `hls::stream`
mechanics. The durable output is the **decision + gate results** (recorded in memory + this plan), not the
probe code. If `hls::task` wins, the *real* artifacts (FIR-as-`hls::task`, the tile sandbox) get checked in
properly during the migration.

## `hls::task` shape (for reference)
Use the real `hls::task` (data-driven): `hls_thread_local hls::stream<...>` channels + `hls_thread_local
hls::task t(func, args...)`; each task body processes **one** transaction and the framework re-invokes it
(no internal `while(1)` needed). Tasks block on empty input streams (back-pressure). The top instantiates
the tasks; interfaces (`axis`, `m_axi`, control) live on the **top**, tasks access them.

## The gates (staged — a failure localizes the cause)

### Gate 1 — MAKE-OR-BREAK: `m_axi` master from inside an `hls::task`
One free-running task, paced by an input trigger stream, doing an `m_axi` burst:
- `scale_task(hls::stream<trig_t>& s_in, ap_uint<W>* gmem_in, ap_uint<W>* gmem_out)`: read a trigger
  `{addr, len}` off `s_in` (blocks → paces the task), **burst-read** the block from `gmem_in` over `m_axi`,
  scale (×2), **burst-write** to `gmem_out`.
- Top: the `m_axi` ports + the `axis` trigger + a control protocol; instantiate the task.
- TB: send N triggers, seed `gmem_in`, check `gmem_out == 2·gmem_in`.
- **Q: does it csynth + cosim CLEAN?** **If NO → `hls::task`+`m_axi` is out. STOP. Stay DATAFLOW+FixedBeat.**
  Everything below is moot until this passes.

### Gate 2 — two tasks, INDEPENDENT `m_axi` bundles, stream between
- `load_task(hls::stream<trig_t>& s_in, ap_uint<W>* gmem_in, hls::stream<float>& d)`: trigger → burst-read
  `gmem_in` → stream floats to `d`.
- `store_task(hls::stream<float>& d, hls::stream<trig_t>& meta, ap_uint<W>* gmem_out)`: read floats from
  `d` → burst-write `gmem_out`.
- **Two separate `m_axi` bundles** (`gmem_in` on load's, `gmem_out` on store's), connected by `hls::stream d`.
- **Q: (a) do two independent `m_axi` bundles coexist? (b) does the inter-task stream work? (c) do load &
  store OVERLAP?** Feed N back-to-back triggers and check whether `load(N+1)` overlaps `store(N)` (the
  producer/consumer concurrency — measure the per-trigger period vs a single trigger's latency).

### Gate 3 — THE MOTIVATION: data-dependent control flow that deadlocked DATAFLOW
- A task that reads a **variable-length / TLAST-terminated** packet with the **same data-dependent control
  flow that hung the FIR** — `read_axi4_stream`-with-TLAST-early-return, or a read-until-sentinel loop.
- **Q: does it cosim in `hls::task` where it DEADLOCKED in DATAFLOW?**
  - **PASS → empirical proof `hls::task` is the more permissive model** (the entire reason to switch).
  - **FAIL (also deadlocks) → the pivot buys much less; reassess** (maybe FixedBeat is unavoidable anyway).

### Also record — control protocol
Which top control works for a task-based top: `ap_ctrl_none` (pure free-running) vs `ap_ctrl_hs`
(launched once by `ap_start`, runs forever). Note any `s_axilite`-config interaction (the launch/config +
free-running question — fuller test deferred to the tile sandbox, but flag what surfaces here).

## Environment / Vitis (build-validated)
Vitis HLS 2025.1 IS installed and auto-detected — do NOT check `which vitis_hls`/PATH; verify via
`toolchain.find_vitis_path()`. Run Python via `../pysilicon-venv/Scripts/python.exe`. If Vitis errors,
report the real output — never soft-skip.

## Deliverable
`experiment/hls_task/`: the gate kernels + TBs + run scripts + **`gates.md`** (the concrete answers:
Gate 1 ✓/✗, Gate 2 independent-bundles/overlap, Gate 3 ✓/✗ vs DATAFLOW, control protocol). Gitignored —
**nothing committed.** Report the gate results.

## Decision this produces
- **Gates 1–3 pass** → `hls::task` becomes the free-running synthesis target: redo the FIR as `hls::task`;
  next sandbox = the minimal **tile** (`s_axilite`-config/launch top + `m_axi` memory-agent task + compute
  tasks + a doorbell stream). Pause DATAFLOW-specific investments.
- **Gate 1 fails** → `hls::task`+`m_axi` out; stay DATAFLOW + the FixedBeat serializer fix; finish Stage A
  on DATAFLOW for real.

## Out of scope
- The full tile (config top + memory-agent + doorbell) — the *next* sandbox, only if these gates pass.
- Any Waveflow integration / codegen — only after the synthesis model is decided.
- The shared-`m_axi` memory-agent arbitration question — Gate 2 uses *independent* bundles; the memory-agent
  (one `m_axi` owner + stream requests) is validated in the tile sandbox.
