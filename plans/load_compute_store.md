## Overview
This note documents how we can model and implement the load-compute-store paradigm using the row-wise FIR example.
We have two versions we wish to implement:

- Matrix-LT version:
    - One event per stage (load / compute / store) **per matrix** — the whole matrix moves through
      each stage as a single timed event.
    - User manually writes the DATAFLOW implementation in Vitis HLS (a hand-written hook).
    - Fast simulation, but less accurate (intra-matrix row interleaving is abstracted into the
      latency model; only inter-matrix pipelining is explicit).
- Row-LT version:
    - One event per stage **per row** — captures the intra-matrix interleaving too.
    - WaveFlow builds the scaffolding for the load, compute, and store methods and dataflow region.
    - Slower simulation, more accurate.

**Both versions use the same three persistent stage processes (load / compute / store) over a shared
bus resource.** The *only* difference is granularity: matrix-LT fires one event per stage per matrix;
row-LT fires one per row. Start with matrix-LT — it needs no module-interconnect / sub-module
codegen infrastructure (the DATAFLOW kernel is a single hand-written hook).

## Creating a matrix LT version

### Why three persistent processes (not one `run_proc`)

A tempting simplification is a single `run_proc` that, per command, reads X, computes Y, advances
time by the calibrated whole-matrix latency, and writes Y (the VMAC shape). **This is wrong for a
streamed command queue.** It serializes successive matrices —
`load₁ → compute₁ → store₁ → load₂ → …` — and, critically, leaves the **memory bus idle during
`compute(N)`** while the single process is blocked there. In a real free-running load-compute-store
dataflow, `load(N+1)` reclaims that idle bus and overlaps `compute(N)`; that overlap is the whole
point of the structure. So the matrix-LT model **must** keep load / compute / store as three
persistent processes that each pull the next matrix as soon as they are free.

### The two key modeling ideas

**1. Fictitious (unsynthesized) inter-stage messages.** Create plain dataclasses for the
load→compute and compute→store handoffs. Because they are never synthesized, they do **not** have to
be `DataSchema` classes (in real hardware this data moves through the partitioned-BRAM / FIFO
channels; in sim it is just a Python object). They carry the data plus the timing tag.

**2. Per-direction channel resources (NOT one shared bus).** This is the Phase 1 correction
(2026-06-22): an AXI `m_axi` bundle is **full-duplex** — independent read (AR/R) and write (AW/W)
channels — so a read and a write **never contend**, even on a single bundle. Model **one resource
per direction**, `bus_rd` and `bus_wr` (each capacity-1), *not* a single shared bus. compute() runs
out of BRAM and touches neither. Consequences:
- `compute(N)` holds no channel → freely overlaps `load(N+1)` and `store(N)` (the throughput win,
  and the reason the 3-process structure beats a single `run_proc` — see above).
- `load(N+1)` and `store(N)` use **different** channels → they **also** overlap (full-duplex). The
  read channel serializes only *successive loads* (load(N) vs load(N+1)); the write channel only
  *successive stores*. Per-matrix throughput = `max(read-channel time, write-channel time,
  compute time)`.
- There is **no single-port II=2 floor** for a read+write kernel — Phase 1 cosim proved single-bundle
  == split-bundle byte-for-byte. The `#streams→II` floor is a *same-direction* phenomenon (two reads,
  VMAC); it does not apply here, and the multi-bundle knob is dropped from this example.

```python
@dataclass
class FIRCompMsg:
    """load -> compute handoff (unsynthesized; data really moves via shared BRAM)."""
    tstart: float          # time the first row of X is available to compute
    X: ndarray             # loaded input matrix
    cmd: FIRCmd            # replica of the command

@dataclass
class FIRStoreMsg:
    """compute -> store handoff (unsynthesized; data really moves via a FIFO)."""
    tstart: float          # time the first row of Y is available to store
    Y: ndarray             # computed output matrix
    cmd: FIRCmd
```

### The component

> **As-built (branch `matrix-lt-fir`) — read `examples/rowwise_fir/fir.py` as the source of truth.**
> The sketch below is the design intent; the shipped code differs in two API/structure points found
> during the build:
> - **Memory API:** stage data moves via the `Region` `read_slice` / `write_slice` (like VMAC), not
>   `read_array`. The build used the **blocking** slices, but `store` should use an **early-anchored
>   pipelined write** (the plan intent) — `StreamIF.write_pipelined` exists but has no memory/`Region`
>   equivalent, so a `Region.write_slice_pipelined` / `read_slice_pipelined` must be added. **This is
>   the main fix** (see **§ Build findings**); it is what makes the intra-matrix overlap emerge.
> - **Secondary limitation:** the Region `word_bw` is *linear* (cyc/word) and cannot represent the
>   **per-row burst-setup** term, so the read span under-reads (272 vs RTL 392). Per-row-aware spans
>   are needed (the module docstring claims timeout-driven timing but the code is `read_slice`-driven
>   — resolve that).

```python
class FIR(HwComponent):

    def __post_init__(self):
        # Sanctioned SimPy primitives (see waveflow/simulation/simobj.py):
        self.load_q    = self.transaction_queue()    # run_proc -> load   (simobj.py:183)
        self.compute_q = self.transaction_queue()    # load    -> compute
        self.store_q   = self.transaction_queue()    # compute -> store
        # Per-DIRECTION channel resources (simobj.py:194) — a single m_axi bundle is full-duplex,
        # so read and write get independent capacity-1 resources, NOT one shared bus.
        self.bus_rd = self.resource(capacity=1)       # AR/R channel (loads serialize here)
        self.bus_wr = self.resource(capacity=1)       # AW/W channel (stores serialize here)

    def pre_sim(self):
        # Three persistent stage processes (simobj.py:146 spawns the coroutines).
        self.process(self.load())
        self.process(self.compute())
        self.process(self.store())

    def run_proc(self) -> ProcessGen[None]:
        # The host-facing entry: pull commands and hand them to the load stage.
        while True:
            cmd: FIRCmd = yield from self.cmd_queue.get(FIRCmd)
            self.load_q.put(cmd)        # kicks the pipeline; does NOT block on completion

    # The ONLY synthesizable method: codegen emits the m_axi top + the hand-written
    # DATAFLOW kernel. In sim it is never executed — the three processes below model its timing.
    @synthesizable(impl_file="fir_dataflow.tpp")
    def dataflow(self, cmd) -> ProcessGen[None]:
        ...

    def load(self) -> ProcessGen[None]:
        while True:
            cmd = yield from self.load_q.get()
            self.logger.log(event='cmd_arrive', tx_id=cmd.tx_id)
            with self.bus_rd.request() as req:       # read channel (serializes successive loads only)
                yield req
                # tstart = ABSOLUTE sim-time the first input row is available to compute.
                tstart = self.now() + self.t_row_load(cmd)
                self.logger.log(event='load_begin', tx_id=cmd.tx_id)   # == start of X-read burst in RTL
                # read_array reads the whole matrix: it ALREADY advances time by the read duration
                # AND holds bus_rd for it — so NO extra timeout here (that would double-count).
                X = yield from self.mem_if.read_array(cmd.xaddr, ...)
                self.logger.log(event='load_end', tx_id=cmd.tx_id)     # == end of X-read burst
            self.compute_q.put(FIRCompMsg(tstart=tstart, X=X, cmd=cmd.copy()))

    def compute(self) -> ProcessGen[None]:
        while True:
            msg = yield from self.compute_q.get()    # arrives at load-finish; msg.tstart is earlier (overlap)
            # t_compute = time to process the whole matrix (measured from first-input-row-available).
            # t_tail    = remaining compute after the last input row lands; 0 if compute is fully
            #             hidden under the load tail. Using ABSOLUTE times (not `t_compute - msg.tstart`,
            #             which mixes a duration with an absolute time).
            t_compute = self.t_compute(msg.cmd)
            t_done = msg.tstart + t_compute
            t_tail = max(t_done - self.now(), 0.0)
            self.logger.log(                       # internal marker — NOT bus-visible in RTL
                event='comp_begin',
                t_compute=t_compute,
                t_tail=t_tail,
                tx_id=msg.cmd.tx_id)
            Y = fir_golden(msg.X, msg.cmd.h, axis=...)          # the ONE shared golden
            yield self.timeout(t_tail)
            # First Y row available to store = first input row + one row's compute time:
            tstart_out = msg.tstart + self.t_row_compute(msg.cmd)
            self.store_q.put(FIRStoreMsg(tstart=tstart_out, Y=Y, cmd=msg.cmd))

    def store(self) -> ProcessGen[None]:
        while True:
            msg = yield from self.store_q.get()
            with self.bus_wr.request() as req:       # write channel — independent of bus_rd (full-duplex)
                yield req
                self.logger.log(event='store_begin', tx_id=msg.cmd.tx_id)  # == start of Y-write burst in RTL
                # write_pipelined(data, t_out_start) -- 2-arg (interface.py:637); addressing via mem_if.
                yield from self.mem_if.write_pipelined(msg.Y, msg.tstart)
                self.logger.log(event='store_end', tx_id=msg.cmd.tx_id)    # == end of Y-write burst
                # Response back to the host — a small write burst on bus_wr, after the Y writes:
                rsp = FIRResp(tx_id=msg.cmd.tx_id, ...)
                yield from self.mem_if.write(...)
                self.logger.log(event='resp_sent', tx_id=msg.cmd.tx_id)    # == the response-write burst
```               

**`tstart` convention (pin it once):** every `tstart`/`tstart_out` field is an **absolute sim-time**
= the moment that stage's **first output row** becomes available to the next stage. This is the
single pipeline-fill quantity threaded through the three stages; it can legitimately be *earlier than
`self.now()`* at the receiving stage (that gap is the inter-stage overlap). It is consistent with the
existing `now()` / `write_pipelined(data, t_out_start)` APIs, which are all absolute-time.

Notes / corrections vs. the first sketch:
- `self.store_q.put(msg)` (not `self.store_q(msg)`); `msg.cmd` is in scope in `compute()` (the bare
  `cmd` was undefined); `write_pipelined` is **2-arg** `(data, t_out_start)`.
- **Don't double-count time.** `load` lets `read_array` advance time (no extra timeout); `compute`
  waits only the **remainder** `max(msg.tstart + t_compute − now, 0)` so the load-tail overlap is
  credited; `store` lets `write_pipelined(Y, tstart_out)` pace the writes from the first-Y-row time
  (clamped to `now` if already past).
- `load` wraps its traffic in `bus_rd.request()`, `store` in `bus_wr.request()`, `compute` in
  neither — those three independent timelines (read channel ∥ write channel ∥ BRAM compute) are the
  whole full-duplex pipeline model.
- **Abstraction boundary (matrix-LT):** message handoff happens at full-stage completion, so
  *intra*-matrix compute∥store overlap is folded into the calibrated `t_compute`/`t_store` + the
  `tstart` fill model, while the bus resources correctly serialize *successive* same-direction
  matrices (inter-matrix throughput). Modeling intra-matrix overlap explicitly is the row-LT job.

### Timing parameters this model needs (calibration)

The overlapped model needs the budget **split by the resource it consumes**, so the read-channel,
write-channel, and compute timelines overlap correctly:
- **read-channel time per row** → hold time for `bus_rd`; **write-channel time per row** → hold time
  for `bus_wr`. From Phase 1: throughput ≈1.25 cyc/output (n_row=4; full-duplex, single==split), and
  reads (`n_col` words) ≈ writes (`n_col−T+1` words) per row.
- **compute latency / fill** (`t_compute`, `t_row_compute`) → the BRAM timeout that overlaps both
  channels; from the `L0 + L_row·n_row + L_col·n_col` fill terms of the Phase 1 **bilinear** fit.
  Note the per-output rate is n_row-dependent (2.44 @ n_row=1 → 1.25 @ n_row=4), so calibrate the
  per-row fill and the steady rate separately rather than a single II.

A single-matrix calibration cannot, by construction, observe the inter-matrix overlap (load(N+1) ∥
compute(N) ∥ store(N)). **Phase 1 addendum:** add a **back-to-back 2-matrix (2-invocation) cosim
point** to the sandbox so the streamed timeline validates this overlap model — confirm the predicted
time for two consecutive matrices matches cosim, not just the single-matrix latency.

### Calibration protocol (data points, isolation, fit)

**1. Isolate per-stage timing — calibrate single-task.** Per-stage parameters (`t_row_load`,
`t_load`, `t_compute`, `t_row_compute`, write-channel rate) must be measured with **exactly one
command in flight**, so `bus_rd` / `bus_wr` / the inter-stage queues are *never contended* — any
contention would confound the pure per-stage durations. The calibration harness drives one matrix,
waits for full completion, then the next. (Inter-matrix overlap is then *validated* separately via
the 2-matrix back-to-back point above, not used to fit the per-stage primitives.)

**2. Where the calibration logs go.** Each stage emits structured, `tx_id`-correlated timestamped
events (gated behind a calibration/trace flag) via the sim logger (`waveflow/simulation/logger.py`).
The mechanics — which events, which map to RTL bus bursts, and how to match the two timelines — are
in **§ Calibration with logging** below. Keep the schema structured (event, tx_id, n_row, n_col, t)
so the fit consumes it directly ([[project-cycle-model-training]]).

**3. Data-point grid (fixes Phase 1's mistake).** The latency is **bilinear**, so:
- **≥3 `n_row` values** (e.g. `{1, 2, 4, 8}`) — Phase 1's `{1,4}`-only grid under-constrained the
  n_row coupling, making the per-output rate (2.44→1.25) impossible to pin.
- **`n_col` sweep entirely in-range** (e.g. `{16,32,64,128,256,512,1024}`); **do NOT** hold out an
  extrapolated point. Phase 1's `(4,1024)` holdout was a 2× `n_col` extrapolation → misleading 19%.
- **Hold out an INTERIOR point** (e.g. `(2,256)`) so the held-out error measures interpolation
  quality, the property the `block` model actually relies on.

**4. Fit with scikit-learn (the long-term plan).** Fit `cycles ≈ L0 + L_row·n_row + L_col·n_col +
II·trips` with `sklearn.linear_model.LinearRegression` on feature matrix
`[n_row, n_col, trips]` (intercept = `L0`), where `trips = n_row·(n_col − T + 1)`. Report
coefficients, `R²` (`.score`), and the interior held-out relative error. `scikit-learn` is now a
project dependency — **installed in the venv (1.9.0); add `scikit-learn>=1.3` to `pyproject.toml`
`dependencies`** (the canonical dependency list here; `requirements.txt` is empty/unused — the
install path is `pip install -e ".[dev]"`).

### Calibration with logging — matching the PySim and RTL timelines

The stage generators emit timestamped events, correlated across runs by `tx_id`:

| event | logged where | RTL counterpart (bus-visible?) |
|---|---|---|
| `cmd_arrive` | load entry | command-descriptor read — **yes** |
| `load_begin` / `load_end` | around `read_array` | **X-read burst span** — **yes** |
| `comp_begin` (carries `t_compute`, `t_tail`) | compute entry | none — internal BRAM marker, **not** bus-visible |
| `store_begin` / `store_end` | around `write_pipelined` | **Y-write burst span** — **yes** |
| `resp_sent` | after the response write | response-write burst — **yes** |

**Match by burst span, not just end-to-end.** RTL exposes four bus-touching boundaries — the X-read
span (`load_begin`→`load_end`), the Y-write span (`store_begin`→`store_end`), and the response write
(`resp_sent`). So read each transfer knob off *its own* burst span instead of inferring it from the
total: the read model from the X-read span, the write model from the Y-write span. The compute
contribution is then the **observable gap** between `load_end` and `store_begin`, not a hidden
residual (`comp_begin` stays a useful PySim-internal marker, but it has no RTL burst to match).
Anchor both timelines at `cmd_arrive` (cancels any constant command-fetch offset), compare each
later event's offset, and tune until the residual < ε ([[project-cycle-model-training]] — tolerance
as an evaluation metric, not a pass/fail bit).

**Which knob a mismatch implicates — the `t_tail` diagnostic.** Pick calibration points that sit
unambiguously in one regime so the attribution is clean:

- **Case 1 — `t_tail = 0` (data-limited).** `t_compute` cannot move the timeline (compute is hidden),
  so any mismatch is in the **transfer model** (`read_array` / `write_pipelined` cycle counts).
  Calibrate on a **wide-short** matrix (large `n_col`, `n_row = 1`) where memory clearly dominates.
  **For FIR this is the usual case** — compute is II=1, so the kernel is memory-bound (the Phase 1
  ≈1.25 cyc/out finding); the transfer model is the primary target and `t_compute` only bites at
  large `T`.
- **Case 2 — `t_tail > 0` (compute-limited).** The `load_end`→`store_begin` gap is real compute time;
  adjust `t_compute` to match it. Isolate on a **compute-heavy** point (large `T`).

*Validity caveat:* the Case 1/2 attribution assumes `t_compute` is already roughly right (a
badly-wrong `t_compute` could predict `t_tail = 0` when reality is compute-bound). So fix the
transfer model on a confidently data-limited point **first**, then `t_compute` on a compute-limited
point.

**Single command, then back-to-back.** Calibrate and first validate with **one** command in flight
(no resource contention → the per-stage spans are pure). Then send **≥2 commands back-to-back** and
confirm the overlapped timeline still matches — that is what actually exercises the `bus_rd`/`bus_wr`
serialization and the `load(N+1) ∥ compute(N) ∥ store(N)` overlap, the key design goal of the model.

### Build findings (branch `matrix-lt-fir`, 2026-06-22) — latency over-estimate is a wrong-primitive bug, NOT structural

The first build validated functional correctness (sim + generated-kernel cosim bit-exact, zero test
regressions) and **inter-matrix overlap in sim** (load(N+1)∥store(N)). The single-command latency
over-estimate is **not** a structural limit of the serial three-process model — it is that the build
used **blocking** `read_slice`/`write_slice` instead of the **early-anchored pipelined** transfers
this plan specified.

- The blocking write times from `now` (= compute-done ≈ load-done), so the Y-write is placed *after*
  the X-read: at 4×64 that's `392 + 437 = 829`. The full-duplex kernel **overlaps** them → RTL
  whole-kernel is **656** (= `max(read, write) + fill`).
- **Fix = pipelined memory transfer anchored at the early `tstart`** (what the plan always intended):
  `store` writes via a pipelined write that completes at `tstart_out + t_write`, overlapping the read
  → `~219 + 437 ≈ 656`. The serial structure is fine; **no overlapped-hold / row-LT restructure
  needed.** `load` likewise uses a pipelined read to get an accurate `tstart` (secondary — it sharpens
  the fill; load still blocks for the full X since the golden needs all of it).

**Infra gap (the reason the build substituted blocking slices):** the early-`t_out_start` anchor
exists on `StreamIF` (`write_pipelined` / `get_pipelined`) but **not on the memory/`Region` path** —
memif's `read_array_pipelined`/`write_array_pipelined` are misnamed *instrumented-blocking* wrappers
(they return `tstart`/`tend` but have no anchor). So the fix needs **`Region.write_slice_pipelined(i0,
data, t_out_start)` + `read_slice_pipelined(i0, i1) -> (data, tstart)`** mirroring `StreamIF` — the
[[project-memory-modeling-unification]] seam.

*Second-order caveat:* a pipelined write fixes **latency**, but `bus_wr` is still acquired late, so
its resource hold `[acquire, end]` is shorter than the real `[tstart_out, end]` occupancy. Harmless
for FIR (read ≈ write → throughput stays read-bound = correct); only matters for a write-channel-bound
kernel. Note, don't fix now.

**Codegen follow-fix (deferred):** generated `gmem` is typed `real_t*` and bypasses serialization;
it should be `ap_uint<word_bw>*` with `serialize`/`deserialize` (passes cosim for float only by
coincidence; won't generalize to struct/mixed/complex — [[project-complexfield-serialization]]).

**Ship gates (all open):** (1) the pipelined-transfer latency fix (add the `Region` pipelined
variants) + per-row-aware spans so the burst-setup term can fit; (2) real per-stage cosim calibration
(read/write spans off their own bursts, ≥3 n_row, sklearn bilinear fit); (3) the **back-to-back
2-matrix cosim** match (headline throughput claim — shown in sim, not yet validated vs RTL). That
triplet is the decisive experiment for "calibrated matrix-LT matches RTL within tolerance for both
latency and throughput." Structural / row-LT fidelity stays optional-future.

### Codegen

The `@synthesizable(impl_file="fir_dataflow.tpp")` `dataflow` method is the single hand-written hook:
`fir_dataflow.tpp` contains the real streams, shared (partitioned-BRAM ping-pong) memory, the three
load / compute / store functions, and the `#pragma HLS DATAFLOW` region — i.e. **the validated
Phase 1 sandbox `fir_accel` kernel**, reused as the hook. On the framework axes from
`dataflow_composition.md` this version is `exec_model=hook` + `sim_fidelity=block`; nothing in the
module-interconnect / sub-module codegen path is needed.

## Implementing the Row-Wise LT version 
