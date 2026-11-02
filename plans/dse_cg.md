# Plan: does pysim speed up design-space exploration? An agentic A/B on CG

**Status: rewritten 2026-11-01.** Replaces `plans/mcp_fir.md` (in git history). The design is now
described elsewhere, and this plan says what to build, in what order, and how to know it works:

| what | where |
|---|---|
| the problem, the adaptor, the skill, the service, explained | the guide section [`docs/guide/dse/`](../docs/guide/dse/index.md) |
| the code skeleton: interfaces settled, bodies stubbed by phase | [`examples/dse_cg/`](../examples/dse_cg/README.md) |

**The first goal is a working user flow on V1:** the CG detector's adaptor, verified on a few design
points, behind a working service and `dselib`. That is Phases 1–3 below, and the hand-off point for
the student. The experiment itself (ground truth, replay, agents) and V2 come after, outlined at the
end.

**Version: everything at 2025.1**, Vitis / Vivado, on the laptop and the lab machine. The lab machine
is licensed for `xczu48dr` and makes every recorded number; the laptop uses `xczu7ev` as a stand-in
for synthesis. No 2024.1 record is used as calibration, ground truth or cost.

## The claim

**Adding pysim, a bit-exact simulation with calibrated, loosely-timed cycles and resources, makes
design-space exploration faster than a flow with only an untimed bit-exact simulation, the HLS
compiler's estimates, and synthesis plus RTL simulation.**

| evaluation | traditional arm | Waveflow arm |
|---|---|---|
| `golden`: untimed bit-exact simulation | ✓ | ✓ |
| `hls_estimate`: csynth's per-kernel estimates | ✓ | ✓ |
| `pysim`: bit-exact, calibrated timing and resources | -- | ✓ |
| `synth`, `rtlsim`: measured resources and cycles | ✓ | ✓ |

The arms differ in `pysim` alone ([The DSE problem](../docs/guide/dse/problem.md)). The result is,
per arm, the best verified design against the tool time charged, and the tool time to reach a design
within ε of the optimum.

A second contribution comes with it and must be named in the paper: the curated interface itself
(generated `DSE.md`, structured rows, derived artifacts instead of raw logs) helps an agent with or
without pysim. Both arms get it, so the A/B isolates pysim; an extra arm on the raw tools could
measure the curation later.

## Phase 1: complete the CG system (V1)

V1 is `CgDetector` (`examples/mimo_cg/hw/detector.py`): a command receiver, a memory reader, load,
control, Waveflow's `CgVectorCore` and `SystolicCore`, store and a memory writer, as one free-running
composite. Its golden (`mimo_cg_fixed.py`), its units and their calibrations exist. What is missing
is that it runs only on the older kernel-testbench flow (`build.py`'s `composite_gen`, `CgDetectorTB`,
`run_xsi`), and the adaptor's defaults all go through an `HwSystem`. So Phase 1 is mostly a
migration, plus the pieces the adaptor needs.

**1a. The detector as an `HwSystem` -- first, and blocking.** The student's initial run of the
detector was made on the older flow while the system flow was being finished; nothing after this
step can use it until it runs as an `HwSystem`. Build `HwSystem(sim, DutTop.of(...))` from what `CgDetectorTB`
already holds: a `StreamDriver` for commands, a `StreamSink` for completions, a `MemoryMod` behind an
AXI crossbar to both `m_axi` bundles, and the `CgDetector` as the DUT. Every testbench part has an RTL
twin, so the system flow (`add_system_steps`) applies as it is. Map the adaptor's parameters to the
detector's `HwParam`s (`W` and `g_s` select `fmt` through `HwConfig`).
*Gate:* at the default point, the system flow's XSI run writes every `X` word bit-exact against
`cg_fixed`, and its cycle count equals the kernel-testbench run's at 2025.1, or the difference is
explained.

**1b. Synthesis and csynth through the system flow.** The DUT is one composite with memory and
crossbar outside it, so its architecture has one part, `detector`. Synthesize it with the per-device
route (`synth_route="per_device"`) and read the csynth report of each HLS top.
*Gate:* the default point synthesized on the laptop (`xczu7ev`), with resources per instance and a
csynth report per kernel. (The 2024.1 study scored csynth resources; here `synth` is Vivado's, and
csynth's numbers are `hls_estimate`.)

**1c. Workloads.** Define the two as data: `steady` (jobs back to back, for cycles per solve; from
`measure.workload(steady=True)` and `job_nits`) and `snr_sweep` (accuracy at the five SNRs, from the
golden's scenarios).
*Gate:* each runs through `golden`, and `steady` through the 1a system in pysim and at RTL.

**1d. pysim from the system, not from `models.py`.** The adaptor's `pysim` is the default: the 1a
system simulated in pysim with the units' calibrated timing, and resources from `estimate_system`.
`models.py` is the study's hand-fitted analytic model; if pysim's advantage came from it, a reviewer
could say the advantage is a bespoke model, not the method. `models.py` stays, as a comparison.
*Gate:* at the default point, pysim's cycles and resources sit next to 1a's and 1b's measurements in
a table: reported, not gated (scoring is the ground-truth phase's job).

**1e. The units at 2025.1** (`plans/calib_2025_1.md` Stages 0–4, lab machine), in parallel. Until its
platform `xczu48dr_250mhz_vitis2025_1` exists, pysim uses the current one and its confidence levels
say so. Phases 2 and 3 test that the machinery is right, not that the predictions are good, so they
do not wait for it.

## Phase 2: the adaptor, verified

**2a. Write it.** The base class's defaults in
[`adaptor.py`](../examples/dse_cg/adaptor.py) (`architecture` with its parametric check,
`identities`, `hls_identities`, `rebuilds`, `pysim`, `synth`, `rtlsim`, `hls_estimate`), and the CG
detector's own methods in [`cg_detector.py`](../examples/dse_cg/cg_detector.py) (`context`,
`build_system`, `golden`, the workloads). What each must return is in
[The design context](../docs/guide/dse/context.md).

**2b. Fast tests, no toolchain** (`tests/examples/test_dse_cg_adaptor.py`):
- `defaults`, `fill`, `legal`, including a typo refused and `nit <= K` checked;
- `architecture()` has the same shape at the defaults and at every parameter's extremes;
- identities: changing a workload parameter changes none; changing any design parameter changes
  `detector`'s; every design parameter's `rebuilds` is non-empty;
- `golden` and `pysim` agree bit for bit on accuracy at a few points.

**2c. A few design points, every method, checked against each other** (`-m vitis` and `-m xsi`).
Pre-choose five points:

| point | why |
|---|---|
| the defaults | the reference, from Phase 1 |
| the smallest legal design | the edges of the space build |
| a large legal design | the other edge; the longest synthesis |
| the defaults with `R` changed | a design change: new identity, new synthesis |
| the defaults with `nit` changed | a workload change: same identity, no new synthesis |

Run `golden`, `pysim`, `hls_estimate`, `synth` and `rtlsim` at each. *Gates:*
- every call returns a row in the documented shape, with `status: "ok"`;
- `rtlsim` is bit-exact at every point;
- `golden` and `pysim` report the same accuracy;
- the workload-only point has the defaults' identities.

*Reported, not gated:* a table of pysim against `rtlsim` cycles, pysim and `hls_estimate` against
`synth` resources, and each call's tool time. It is the first look at prediction quality and at the
costs `DSE.md` will quote. On the laptop, synthesis uses `xczu7ev`; the lab machine reruns the five
for the record.

## Phase 3: the service and `dselib`, on the live backend

**3a. The service, in process.** [`service.py`](../examples/dse_cg/service.py) on the live backend:
the checks, the cache by identity (parts, kernels, simulations), the ledger file, an optional budget,
jobs on `k` worker slots on the real clock, `results(fn)` flattened for a data frame, and artifacts
in `results/<eval id>/`. What it does on each call is in [The service](../docs/guide/dse/service.md).

**3b. The `dselib` transport.** `dselib.bind(service)` in process first; then `serve()` and
`DSE_SERVICE` for a service in another process or on the lab machine.

**3c. `DSE.md` for the CG detector.** `brief.write_context` and `brief.install_skill`. The generated
`DSE.md` is committed, and a fast test regenerates it and fails on any difference.

*Gates:*
- a scripted test drives `dselib` through Phase 2's five points and gets the adaptor's rows;
- a repeated call is `cached` at cost 0, and the workload-only point charges no synthesis;
- an illegal point and a failed build come back as rows;
- the budget refuses a call it cannot cover; `submit` refuses a design not synthesized and simulated;
- `pandas.DataFrame(results("pysim"))` needs no further work.

*Demonstration, not a gate:* in this repository, `/dse examples/dse_cg/DSE.md <objective>` with a small
budget, run end to end by an agent on the live backend.

**Hand-off.** After Phase 3 a user, or the student, can explore V1 with an agent on real tools, and
every later phase builds on that flow.

## After the hand-off

Outlined; each gets detail when it starts.

**4. Ground truth and validation (G1).** Pre-register a grid of about 300 V1 points with their
workloads, the 6–10 points that fit the integration term (excluded from scoring), the targets and the
objectives. Measure it on the lab machine: the live backend driven over the grid, every row, artifact
and cost recorded. Score pysim's predictions once (proposed targets: DSP and BRAM exact; LUT, FF and
cycles mean ≤ 5%; at least 90% of objectives' picks within 10% of the measured optimum). A miss stops
here: fix the model's form and draw a fresh grid. G1 becomes V1's replay table.

**5. Replay and the sandbox.** The replay backend on G1; `brief.build_sandbox` for both arms, with the
test that their files differ only in the `arm:waveflow` blocks; `run_agent.py`.

**6. Scripted policies** ([`policies.py`](../examples/dse_cg/policies.py)): the first result, with no
agent. **7. Agents:** pilot runs (and the checks below), freeze and pre-register
([`prereg.py`](../examples/dse_cg/prereg.py)), N = 10 runs per arm per objective.

**8. V2, the descriptor-ring system** ([below](#v2-the-descriptor-ring-system)): the system in pysim,
bit-exact; the per-block activity report (pysim and RTL in one schema); the system at RTL; calibration
of the two front ends; then 4–7 again on a grid G2 of about 200 points.

## Fairness rules

1. **One difference.** The arms differ in `pysim`, in `dselib`, the skill and `DSE.md`, rendered from
   the same files ([Skills](../docs/guide/dse/skill.md)).
2. **Calibration is charged to Waveflow**: the units' builds in `calib_2025_1.md` and the system's
   integration builds, reported up front (the headline) and amortized.
3. **A strong baseline.** The traditional arm reads all docs and source and may build its own models.
   Both arms have `hls_estimate`, the quick look a real hardware team takes. It covers the HLS kernels
   only, not the hand-written adaptors or the crossbar, so pysim must beat the HLS compiler's
   estimates, not only full synthesis; it should, because the compiler sees each kernel alone.
4. **Pre-registered** before any scored run: objectives, ε, budget, `k`, N, the model, the skill and
   `DSE.md`.
5. **Several objectives**, so no single answer can be learned.
6. **Meter costs; never gate reasoning or information.** The agent has Python, a shell and every file;
   only expensive evaluations go through `dselib`. Capabilities an engineer would use (a raw trace of
   chosen signals, a custom input, one unit alone) are priced, not forbidden. Pilot runs check whether
   the interface binds: attempts to work around `dselib`, refused calls, and what each report says it
   lacked. If agents strain against it, widen it before the scored runs.

## Measurements

| measure | what |
|---|---|
| **anytime curve** | best verified objective against tool time charged; median and spread over runs |
| **time to target** | tool time (and elapsed time with `k` workers) to the first verified design within ε of the optimum |
| **quality at budget** | the verified objective at the budget, against the optimum |
| **spend** | syntheses and RTL runs, and the share spent on designs that failed a constraint |
| **agent cost** | tokens and agent wall time (secondary) |
| **errors** | report numbers the ledger does not back |

## V2: the descriptor-ring system

### The architecture

```
            host (SwHost, outside the DUT)
               │ first descriptor in / done descriptor out + IRQ
   ┌───────────┴──────────────────── DUT ───────────────────────────────┐
   │  [q] front end ─ systolic array      [q] front end ─ vector unit    │
   │       │   ▲ ──── CgDesc, half-iteration done ────► │   ▲            │
   │       │   └───── CgDesc, half-iteration done ◄──── │   │            │
   │       └──────────── interconnect (AXI crossbar) ───┴───┴───────────┤── m_axi ──► shared memory
   └─────────────────────────────────────────────────────────────────────┘             (outside the DUT)
     [q] = a memory-mapped descriptor queue (mm_queue_in window)
```

The DUT is the systolic array, the vector unit, their front ends and the crossbar: three parts (two
devices and the crossbar). The host and the shared memory sit outside it and are not counted.

**Decided 2026-10-31: sequencing on chip, by descriptor passing.** There is no central sequencer. The
job's descriptor travels between the two units through memory-mapped queues, and each unit does one
half-iteration per descriptor: the SALSA pattern, where units are configured by the messages passing
through them. Chosen over the host sequencing every step (host latency inside every iteration) and
over direct streams for `P` and `S` (the interconnect would carry little), because it gives the DSE
the most to decide.

### One job

```
host:     write A (or H), B into shared memory; write CgDesc{phase=MM, iter=0} into the systolic queue
systolic: read A, P[iter%2] → S = A·P → write S → wait for the write responses → CgDesc{phase=VEC} → vec queue
vec:      read S, P[iter%2], R, X, rz[iter%2] → alpha, x += alpha·p, r -= alpha·s, rz', beta, p' = r + beta·p
          → write X, R, P[(iter+1)%2], rz[(iter+1)%2] → wait for the write responses
          → iter+1 < nit ?  CgDesc{phase=MM, iter+1} → systolic queue
                          :  CgDesc{phase=DONE, status} → host done queue (raises the IRQ)
host:     woken by the IRQ; read the status; read X from shared memory
```

If the initialization is done on chip (`r₀ = b`, `p₀ = b`, `rz₀ = |b|²` with `x₀ = 0`, and optionally
the Gram matrix `A = HᴴH + σ²I` as a `MUL_AH` on the systolic array), it is a phase of its own. The
host sends the first descriptor to whichever unit owns the first phase.

### The descriptor

`CgDesc` is a `DataList` that fits in one queue packet (`[len | data]`, one bus burst):

| field | what |
|---|---|
| `tx_id` | the job, echoed in the status |
| `phase` | `GRAM` / `INIT` / `MM` / `VEC` / `DONE`: what the receiving unit does |
| `iter`, `nit` | the current iteration, and the iteration count (a workload field) |
| `K`, `N` | dimensions |
| `addr_A`, `addr_B`, `addr_X`, `addr_R`, `addr_S` | operand addresses in shared memory |
| `addr_P[2]`, `addr_rz[2]` | ping-pong buffers, selected by the parity of `iter` |
| `status` | `OK`, or the first error; an error skips straight to `DONE` |

**The units hold no job state.** Everything that crosses a half-iteration is in the descriptor, or in
memory at an address the descriptor names. That is what lets several jobs be in flight, and it keeps
the systolic array clear of a known problem: state held across commands makes csynth run out of
memory (`systolic_characterization.md`). The cost is memory traffic: `A` (`K×K`) is re-read every
iteration, and the vector unit reads four `K×N` operands and writes three per iteration. That traffic
is exactly what the interconnect parameters trade against. Programs and formats are configuration,
not job state.

### Each unit's front end

Both units are stream kernels, so each sits behind a front end of free-running tasks:
`CgDetector`'s `cg_cmd_rx` / `cg_load` / `cg_store` pattern, moved onto a bus.

```
queue-in window (MemSlaveWStream) → desc_rx → MemRStream ×k ──► unit's input streams
                                      │                            unit
                                      └─ desc ─► fwd ◄── MemWStream ×k ◄── unit's output streams
                                                  └─► bus writer (mm_stream_writer, credit) → peer's queue-in window
```

- **Systolic front end.** Turns the descriptor into a `LinalgHeader{MUL, K, K, N}`, streams `A` and
  `P[iter%2]` from memory into `s_in`, and writes the reply's `C` to `addr_S`.
- **Vector-unit front end.** Streams the three programs and the operands into `s_lprog` / `s_xprog` /
  `s_sprog` / `s_in*`, writes `s_out*` to their addresses, reads `m_status`, and chooses the next hop.
  Programs are first fetched from shared memory at an address set at configuration time; later,
  as a build option, kept resident (decoding a pass costs about 16 cycles, 42% of a CG job,
  `vecunit_future.md`).

Everything in the diagram exists except `desc_rx` and `fwd`: the queue windows
`MemSlaveWStream` / `MemSlaveRStream`, the stream adaptors `MemRStream` / `MemWStream`, the bus writer
with credit (`MmCreditStreamIF`), and the crossbar.

### Five rules that make it correct

1. **Write before notify.** `fwd` sends a descriptor only after the memory has acknowledged every
   write of the half-iteration (all B responses). Otherwise the peer reads stale data, and pysim
   will not show that race.
2. **No write into a full queue.** A write into a full window holds the crossbar's write channel and
   stalls other masters. So forwards go through credit, and every credit write is guarded on
   `wait_room()`; without the guard, the RTL deadlocks.
3. **Bound the ring.** If both queues are full, each `fwd` waits on the other. The host keeps at most
   `J ≤ min(depth_sv, depth_vs)` jobs in flight; the bound is a constraint in `space()`, and pysim
   asserts it.
4. **Ping-pong what is read and rewritten.** `P` and `rz` have two buffers each, chosen by the parity
   of `iter`; `X` and `R` too, until it is shown that the vector unit reads each block before writing
   it back.
5. **Errors are fatal and travel to the host.** A failing unit sets `status` and forwards the
   descriptor to `DONE` without doing its work (`docs/guide/systems/command_response.md`, applied to
   a ring).

### Parameters

- **Design:** systolic `R`, `C`, `L`, `form`, `word_bits`, `sob_depth`; vector unit lanes,
  `word_bits`, `VLMAX`, queue depths, register formats, the divider; interconnect width and burst
  sizes; the descriptor queues' depths.
- **Workload:** `K`, `N`, the iteration count, the SNR scenario, and `J`, the jobs in flight.

Settle which vector-unit formats are build-time and which run-time before the adaptor declares them.

### Why it separates the arms

- **Jobs in flight decide the throughput, and no formula does.** At `J = 1` the units alternate; at
  `J ≥ 2` one job's multiply overlaps another's vector half, and the bottleneck becomes the slower
  unit or the shared memory. A traditional agent must guess or pay RTL; pysim's per-block activity
  shows it in about a second.
- **The bandwidth trade is real.** Stateless units re-read their operands, so interconnect width,
  burst sizes and unit sizes trade against one another.
- **It scales to more units.** A descriptor with a next-hop field chains N units.

On V1 timing is nearly analytic (trip counts plus a fitted overhead), so a traditional agent can
write a decent cycle formula, and pysim's advantage should come mostly from resources and early
pruning. On V2 timing is emergent. The prediction: **pysim's advantage grows as timing becomes less
analytic.**

## What each phase needs

| phase | needs |
|---|---|
| 1, 2, 3 | laptop (Vitis / Vivado 2025.1, `xczu7ev` for synthesis); lab machine to rerun Phase 2's five points for the record |
| 1e | lab machine (`calib_2025_1.md`) |
| 4 | lab machine, about a day of builds |
| 5, 6 | laptop |
| 7 | API access |
| 8 | laptop, then the lab machine for G2 (15–30 tool-hours) |

## Open questions

1. **`k`, the worker slots.** It changes elapsed time, not tool time. Fix it at what the lab machine
   runs.
2. **Out-of-grid requests on replay.** The agents' space is exactly the grid (leaning), so both arms
   always see the same world.
3. **The host's software time on V2** at high `J`; it could be priced with the CPU model.
4. **Where V2's initialization runs:** on the host, as an `INIT` phase, or as a `GRAM` phase too.
5. **The vector unit or `CgVectorUnit` as a V2 knob:** 3,447 against 3,581 cycles a job at 2025.1, and
   82.6k against 21.5k LUT. A finding of its own: what programmability costs.

## Non-goals

- An MCP tool surface. The workspace, the skill and `dselib` are the interface; the service is the
  only boundary.
- Improving the models' forms, or active calibration: later, once the A/B result exists.
- A general DSE algorithm. The agent, or the scripted policy, chooses the points.
