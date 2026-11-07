# Plan: does pysim speed up design-space exploration? An agentic A/B on LTBF

**Status: rewritten 2026-11-01; retargeted 2026-11-05** from the `mimo_cg` CG detector to the LTBF
system of [`examples/ltbf_cg_mem_dse`](../examples/ltbf_cg_mem_dse/hw/system.py) (its plan
[`plans/ltbf_dse/ltbf_dse.md`](ltbf_dse/ltbf_dse.md)); **the decisions of 2026-11-06 folded in**
([below](#decisions-2026-11-06)): what is hardware and what is modelled, the end-to-end pipeline,
the formats, the memory axes, and the space as a grid measured in a week. Replaces
`plans/mcp_fir.md` (in git history). The design is described elsewhere, and this plan says what to
build, in what order, and how to know it works:

| what | where |
|---|---|
| the problem, the adaptor, the skill, the service, explained | the guide section [`docs/guide/dse/`](../docs/guide/dse/index.md) |
| the code skeleton: interfaces settled, bodies stubbed by phase | [`examples/dse_cg/`](../examples/dse_cg/README.md) |

The guide and the skeleton predate the decisions of 2026-11-06; Phase 2a brings them in line.

**The first goal is a working user flow on V1:** the LTBF system's adaptor, verified on a few design
points, behind a working service and `dselib`. That is Phases 1–3 below, and the hand-off point for
the student. The experiment itself (the grid, replay, agents) and V2 come after, outlined at the end.

**Version and machine: everything at Vitis / Vivado 2025.1, on the lab machine** (`xczu48dr` at
4 ns; licensed for every version and device since 2026-11-06), which makes every recorded number.
The units were recalibrated at 2025.1 by [`plans/calib_2025_1.md`](calib_2025_1.md), complete
2026-11-06, and the LTBF study was re-measured at 2025.1 the same day; no 2024.1 number is used.

## Decisions (2026-11-06)

Made with the user before execution:

1. **Built (synthesized, simulated at RTL, calibrated where a calibration exists):** the systolic
   engine and the vector unit, each with its wrapper (Job FIFO window, Ctrl bank, memory front end,
   done record), the AXI crossbar (Vivado's IP, with pysim's current crossbar model: **not
   calibrated**), and the Resp hub (an HLS device, as today). **The CPU scheduler** is the
   scheduler's real software: priced by the calibrated A53 platform in pysim, run by the C++ host at
   RTL, as today; the A53 is the RFSoC's hard core and is never synthesized.
2. **Modelled only, never synthesized:** Shared Mem and the Ingest DMA. In pysim, each is its own
   model; at RTL, Shared Mem is a C++ memory behind the crossbar's slave port and the Ingest DMA a
   testbench process writing into it, with the same parameters as pysim's models. Shared Mem's block
   RAM is estimated by formula (`hw/dse.py`'s `shared_mem_bram18`) and counted against the device;
   the DMA's resources are not counted.
3. **The example is extended, not changed.** Every new behaviour is an option of
   `examples/ltbf_cg_mem_dse` whose default keeps today's behaviour; no existing code is removed or
   changes what it does, and the example's tests pass unchanged.
4. **The whole LTBF pipeline runs end to end in Python**, from the Sionna RT drops to the equalized
   symbols, and only the built parts are hardware. The 100 Denver drops traced with Sionna RT 2.2
   (`data/drops/`) are used as cached; one `-m sionna` test re-traces a drop and checks it matches.
5. **Stage mapping, as today:** rendering and OFDM demodulation (`signals.received`, `frontend.py`)
   in the O-RU stand-in; SRS estimation, CG initialization, DMRS estimation and the measurement as
   Python stages at **zero simulated time, charged to nothing**; the products and `Z` on the systolic
   engine; the CG steps and the equalizer on the vector unit; the per-UE projections on the A53.
6. **Formats per design.** The fixed-point formats are compiled into the hardware (the systolic
   engine's format traits, the vector unit's datapath widths), and today `inputs()` profiles them from
   the point's `t`, `form` and `target`, so each of the three rebuilt hardware. They are now profiled
   once per design, over drops 1–10, `t` up to 10 and both target sets (which gives the [−6, 14]
   formats exactly): `t` and `target` are workload parameters on one build, and `form` is a design
   parameter. [−6, 3] then runs on the [−6, 14] formats, slightly less accurate than the study's
   per-target formats.
7. **The memory bottleneck is a DSE axis.** Validated on the 2025.1 re-run (`paper_data/FINDINGS.md`):
   Shared Mem's port is busy 90–97% of the long-term update at every array size, and the systolic
   engine is bound by it at about 2.13 cycles a word it reads, so a 2 × 4 and an 8 × 8 array take
   the same time. The study's what-ifs at N_rx 64 (one sector's update; resource elements a second):
   as built 6.59 ms, 0.81 M; one word a cycle 4.51 ms, 1.31 M; 2 banks 6.48 ms, 1.12 M; both
   4.44 ms, 1.98 M. Two axes follow, `mem_rate` and `banks`.
8. **The agents' space is the grid, measured in full** (option (a) of the discussion): every point
   is measured on the live backend before any scored run, and measuring the whole grid takes **at
   most a week of real time** on the lab machine at two heavy jobs at once. That sets the space
   ([The space](#the-space-a-grid-measured-in-a-week)).
9. **The timing metric is one sector's long-term update** (`T_upd`: drop 1, sector 1), not the
   study's T_LT (three sectors back to back, the worst of drops 1, 11 and 12: nine updates a
   point). The slot workload is not in the grid.

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

## The space: a grid measured in a week

The agents' space is a full product of these values, every point legal and measured:

| axis | kind | values | default | what a new value changes |
|---|---|---|---|---|
| (`R`, `C`) | design | (2, 4), (4, 4), (4, 8), (8, 8): the calibrated shapes | (4, 8) | the systolic part |
| `W` | design | 12, 16 | 16 | the systolic and vector parts (their formats) |
| `form` | design | factored, explicit | factored | the systolic part |
| `L` | design | 2, 4 | 4 | the vector part |
| `banks` | design | 1, 2: Shared Mem's banks, each its own crossbar port | 1 | the crossbar |
| `mem_rate` | design | a read word every 2 cycles (the BRAM leaf as built), every cycle | 2 | nothing synthesized: the memory model |
| `cg_window` | design | 1, 2, 4: CG chains in flight in the scheduler | 2 | nothing synthesized: the host's software |
| `t` | workload | 2, 3: CG iterations of the long-term update | 3 | nothing synthesized: the job graphs |
| `target` | workload | [−6, 14], [−6, 3] dB: the UEs' SINR targets | [−6, 14] | the data only: `golden`'s accuracy, not the cycles |

**Fixed:** N_rx 64 (`n` 8), one A53 core, the static policy, `c` 8, `Mmax` 16, `J` 9, 64-bit words,
drop 1 sector 1 for timing. The default point is AC9's design (4 × 8, `W` 16, 4 lanes, factored).

**Size.** 64 hardware builds (shape × `W` × `form` × `L` × `banks`) × 6 simulation settings
(`mem_rate` × `cg_window`) = 384 designs; × 2 values of `t` = 768 RTL points; × 2 targets = 1,536
(design, workload) points the agents see. Half the axes (`mem_rate`, `cg_window`, `t`) change
nothing that synthesis or the HLS compiler sees: their effect shows only in a simulation, about
12 minutes at RTL against about a minute in pysim. That is where pysim should pay.

**What measuring it costs (estimates at 2025.1; Phase 2c measures them):**

| work | count | slot-hours | basis |
|---|---|---|---|
| part builds (csynth + Vivado) | about 23: 16 systolic, 4 vector, 2 crossbar, 1 hub | about 8 | csynth 1.7 min median (systolic, max 10.4), 7.4 (vector, max 11.2) in the study's 2025.1 builds; Vivado per part estimated 10–20 min |
| RTL builds (elaboration) | 64 | about 6 | |
| RTL simulations | 768, one sector's update each | about 155 | the AC9 run: 5,493,471 cycles in about 52 min with its build; one update is about 1.65 M cycles |
| pysim and `golden` | every point | about 15 | |
| **total** | | **about 185** | **about 4 days at two jobs at once** |

**The resizing rule**, applied once, before the grid is pre-registered (step 4), on the costs Phase 2c
measures: the grid's estimated cost × 1.3 must fit 7 days × 2 slots = 336 slot-hours. With room to
spare, add `t` 5 first, then `W` 14; over, drop `cg_window` 4 first, then `banks` 2.

**Left out, and why:** N_rx 256 (an RTL run about four times as long); the slot at RTL (about
45 min a run: it would double the grid); the dynamic policy (the RTL host replays the static
policy's decisions, precomputed in Python; dynamic would need the scheduler in C++); 2 cores (the
A53 is busy 23% of the update); `W` 14 and `t` 5 (each pushes the grid to about 6 days).

With the grid as the space, the DSE is hard because the agents' budget is small against the grid's
cost, not because of the grid's size: the budget is pre-registered (step 7) as a fraction of the
grid's measured cost.

## Phase 1: the LTBF system at any grid point (V1)

V1 is the system of `examples/ltbf_cg_mem_dse` (`hw/system.py`): the LTBF receiver on a shared-memory
accelerator. A CPU (the calibrated A53) schedules jobs into the Job FIFOs of a systolic engine
(`SystolicEngineMem`) and a vector unit (`VecUnit`); an Ingest DMA brings each symbol into Shared Mem;
every engine reads its operands from Shared Mem and writes its results there over one AXI crossbar,
and reports done through a Resp hub into the CPU's Resp FIFO. Its golden (the fixed-point chain,
`ltbf_fixed_chain.py`), its devices and their gates, and a DSE over it exist, re-measured at
2025.1. It runs as an `HwSystem` through the system flow (`hw/build.py`'s `system_dag`; the gate
`tests/examples/test_ltbf_cg_mem_dse_xsi.py::test_system_at_rtl`, passing at 2025.1).

**1a. The modelled parts and the memory axes, as options of the example -- first, and blocking.**
New options of `SysParams` (or of `build`), each defaulting to today's behaviour:
- Shared Mem modelled: in pysim, a memory model with `mem_rate` and `banks`; at RTL, outside the
  DUT, a C++ memory behind the crossbar's slave port(s) with the same parameters (the framework
  serves a kernel's memory ports this way today, `harness._memory_stand_in`); the memory plan
  places the buffers across the banks;
- the Ingest DMA modelled: in pysim its own model, at RTL a testbench process;
- the formats per design (decision 6): `inputs()` takes the per-design formats instead of profiling
  the point's `t`, `form` and `target`.

First check that the framework can put a memory outside a system's DUT at RTL; if it cannot, stop
and report. *Gates:* every existing test of the example passes unchanged; at the default point in
the modelled configuration, pysim against RTL as `test_system_at_rtl` checks (every Job FIFO and
Ctrl write, every Resp record per source, every data burst; pysim's end within 5% of RTL's), at
`banks` 1 and 2 and `mem_rate` 2 and 1; generating the HLS at `t` 2 and 3 and at both targets gives
identical sources.

**1b. The system at a design point.** `build_system(params)` maps the grid's parameters to
`SysParams` and returns `build(inputs(SysParams(...)))` with the modelled options, its sources
generated by `LtbfCodegenStep(sysp=...)` in a root of its own (as `hw/finalists.py` builds a
finalist). *Gate:* the default point's system is the one 1a gated.

**1c. Synthesis and csynth per part.** The built parts are the systolic, vector and Resp hub devices
and the crossbar. Synthesize them with the per-device route (`synth_route="per_device"`) on the
`xczu48dr` at 4 ns (a platform of the build's own: without one, the system synthesis targets the
framework's default Zynq-7020 at 10 ns, as `hw/finalists.py` learned), and read the csynth report of
each HLS top (`ltbf_sys`, `ltbf_vec`, `ltbf_hub`). Shared Mem's resources are the formula's, the
DMA's none. *Gate:* the default point synthesized, with resources per instance and a csynth report
per kernel, and each step's tool time recorded.

**1d. Workloads and the end-to-end pipeline.** Two workloads as data: `long_term` (one sector's
long-term update, drop 1 sector 1: `T_upd`) and `accuracy` (the median and 90th-percentile SINR loss
against exact float LTBF over the held-out drops at each target set, with the design's formats, as
`ltbf_accuracy_sweep.py` measures it). The **end-to-end test** (decision 4): a cached Sionna drop,
rendered and OFDM-demodulated, through the system in pysim -- the long-term update and one slot --
to the equalized symbols, checked bit for bit against the fixed-point golden and in SINR loss
against float LTBF; at RTL once, at the default point; and the `-m sionna` test that re-traces a drop.
*Gates:* each workload runs through `golden`, and `long_term` through the 1b system in pysim and at
RTL; the end-to-end test passes.

**1e. pysim from the system, not from `hw/models.py` and `hw/jobsim.py`.** The adaptor's `pysim` is
the 1b system simulated in pysim with the units' calibrated timing, and resources from
`estimate_system` plus the Shared Mem formula. The study's frozen per-part models (`hw/models.py`)
and its job-level simulator (`hw/jobsim.py`) were fitted by hand for this one system; if pysim's
advantage came from them, a reviewer could say the advantage is a bespoke model, not the method. They
stay, as a comparison. *Gate:* at the default point, pysim's cycles and resources sit next to 1a's
and 1c's measurements in a table: reported, not gated (scoring is step 4's job).

**The units at 2025.1** are done (`plans/calib_2025_1.md`): the vector unit, the systolic engine (its
core's timing at the LTBF configurations and a resource model on the platform) and the A53 (the
shipped platform and the LTBF kernels, recompiled by 2025.1's gcc). The crossbar, Shared Mem and the
Ingest DMA are not calibrated: pysim takes their timing from their own models, and Shared Mem's
resources from the formula.

## Phase 2: the adaptor, verified

**2a. Write it.** The base class's defaults in
[`adaptor.py`](../examples/dse_cg/adaptor.py) (`architecture` with its parametric check,
`identities`, `hls_identities`, `rebuilds`, `pysim`, `synth`, `rtlsim`, `hls_estimate`), and the LTBF
system's own methods in [`ltbf_cg_mem.py`](../examples/dse_cg/ltbf_cg_mem.py) (`space` as the grid
above, `context`, `build_system`, `golden`, the workloads). What each must return is in
[The design context](../docs/guide/dse/context.md). Bring the guide section and the skeleton's README
in line with the decisions of 2026-11-06.

**2b. Fast tests, no toolchain** (`tests/examples/test_dse_cg_adaptor.py`):
- `defaults`, `fill`, `legal`, including a typo refused;
- `architecture()` has the same shape at the defaults and at every parameter's extremes;
- identities: changing a workload parameter (`t`, `target`) changes none; changing `mem_rate` or
  `cg_window` changes no part's identity (only `rtlsim`'s); changing a design parameter changes the
  parts it reaches and no others (`L`: `vec` alone; `banks`: the crossbar alone); every hardware
  parameter's `rebuilds` is non-empty;
- `golden` and `pysim` agree bit for bit on accuracy at a few points.

**2c. Six design points, every method, checked against each other** (`-m vitis` and `-m xsi`):

| point | why |
|---|---|
| the defaults | the reference, from Phase 1 |
| the smallest (2 × 4, `W` 12, 2 lanes, factored, 1 bank) | an edge of the grid builds |
| the largest (8 × 8, `W` 16, 4 lanes, explicit, 2 banks, `mem_rate` 1, `cg_window` 4) | the other edge |
| the defaults with `R` changed | a hardware change: a new identity for `sys` alone |
| the defaults with `mem_rate` 1 and `cg_window` 4 | a simulation-only change: no new part, a new RTL run |
| the defaults with `t` 2 | a workload change: same identities, no new synthesis |

Run `golden`, `pysim`, `hls_estimate`, `synth` and `rtlsim` at each. *Gates:*
- every call returns a row in the documented shape, with `status: "ok"`;
- `rtlsim` is bit-exact at every point;
- `golden` and `pysim` report the same accuracy;
- the workload-only and simulation-only points have the defaults' part identities.

*Reported, not gated:* a table of pysim against `rtlsim` cycles, pysim and `hls_estimate` against
`synth` resources, and each call's tool time: the first look at prediction quality, the costs
`DSE.md` will quote, and **the measured costs the resizing rule is applied to**.

## Phase 3: the service and `dselib`, on the live backend

**3a. The service, in process.** [`service.py`](../examples/dse_cg/service.py) on the live backend:
the checks, the cache by identity (parts, kernels, simulations), the ledger file, an optional budget,
jobs on `k` = 2 worker slots on the real clock (the lab machine's limit for heavy jobs),
`results(fn)` flattened for a data frame, and artifacts in `results/<eval id>/`. What it does on each
call is in [The service](../docs/guide/dse/service.md).

**3b. The `dselib` transport.** `dselib.bind(service)` in process first; then `serve()` and
`DSE_SERVICE` for a service in another process.

**3c. `DSE.md` for the LTBF system.** `brief.write_context` and `brief.install_skill`. The generated
`DSE.md` is committed, and a fast test regenerates it and fails on any difference.

*Gates:*
- a scripted test drives `dselib` through Phase 2's six points and gets the adaptor's rows;
- a repeated call is `cached` at cost 0; the workload-only point charges no synthesis, and the
  simulation-only point charges only its RTL run;
- an illegal point and a failed build come back as rows;
- the budget refuses a call it cannot cover; `submit` refuses a design not synthesized and simulated;
- `pandas.DataFrame(results("pysim"))` needs no further work.

*Demonstration, not a gate:* in this repository, `/dse examples/dse_cg/DSE.md <objective>` with a small
budget, run end to end by an agent on the live backend.

**Hand-off.** After Phase 3 a user, or the student, can explore V1 with an agent on real tools, and
every later phase builds on that flow.

## After the hand-off

Outlined; each gets detail when it starts.

**4. The grid, measured (G1).** Apply the resizing rule to Phase 2c's costs, then pre-register the
grid, its workloads, the objectives and the scoring targets. Measure every point on the live backend
(at most 7 days at two jobs at once), every row, artifact and cost recorded. Score pysim's
predictions once, over the whole grid (proposed targets: DSP and BRAM of the built parts exact; LUT,
FF and cycles mean ≤ 5%; at least 90% of objectives' picks within 10% of the measured optimum); if a
system-level integration term is needed, the few points that fit it are chosen beforehand and
excluded from scoring. A miss stops here and goes to the user. G1 is the replay table, and each
objective's optimum is known exactly.

**5. Replay and the sandbox.** The replay backend on G1; `brief.build_sandbox` for both arms, with the
test that their files differ only in the `arm:waveflow` blocks; `run_agent.py`.

**6. Scripted policies** ([`policies.py`](../examples/dse_cg/policies.py)): the first result, with no
agent. **7. Agents:** pilot runs (and the checks below), freeze and pre-register
([`prereg.py`](../examples/dse_cg/prereg.py)) -- the budget as a fraction of the grid's measured
cost --, N = 10 runs per arm per objective.

**8. V2, the descriptor-ring system** ([below](#v2-the-descriptor-ring-system)): the system in pysim,
bit-exact; the per-block activity report (pysim and RTL in one schema); the system at RTL; calibration
of the two wrappers' descriptor forwarding; then 4–7 again on a grid G2, sized by the same rule.

## Fairness rules

1. **One difference.** The arms differ in `pysim`, in `dselib`, the skill and `DSE.md`, rendered from
   the same files ([Skills](../docs/guide/dse/skill.md)).
2. **Calibration is charged to Waveflow**: the units' builds in `calib_2025_1.md` and the system's
   integration builds, reported up front (the headline) and amortized.
3. **A strong baseline.** The traditional arm reads all docs and source and may build its own models.
   Both arms have `hls_estimate`, the quick look a real hardware team takes. It covers the HLS kernels
   only, not the hand-written adaptors or the crossbar, so pysim must beat the HLS compiler's
   estimates, not only full synthesis; it should, because the compiler sees each kernel alone.
4. **Pre-registered** before any scored run: the grid, objectives, ε, budget, `k`, N, the model, the
   skill and `DSE.md`.
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
            host (SwHost on the A53, outside the DUT)
               │ first descriptor in / done descriptor out + IRQ
   ┌───────────┴──────────────────── DUT ───────────────────────────────┐
   │  [q] wrapper ─ systolic engine       [q] wrapper ─ vector unit      │
   │       │   ▲ ──── CgDesc, half-iteration done ────► │   ▲            │
   │       │   └───── CgDesc, half-iteration done ◄──── │   │            │
   │       └──────────── AXI crossbar ──────────────────┴───┴── Shared Mem
   │  Ingest DMA and Resp hub: as in V1                                  │
   └─────────────────────────────────────────────────────────────────────┘
     [q] = the engine's Job FIFO (V1's mm_queue_in window)
```

The DUT is V1's: the systolic and vector wrappers, the Ingest DMA, the Resp hub, Shared Mem and the
crossbar. What changes is who sequences the long-term update's CG chains; the real-time pipeline
(chunks, `Z`, the equalizer) stays scheduled by the CPU, as in V1.

**Decided 2026-10-31 (retargeted to LTBF 2026-11-05): sequencing on chip, by descriptor passing.** In
V1 the CPU sequences every job: it writes each job into an engine's Job FIFO and learns of each
finish from the Resp FIFO's interrupt, so the host's latency sits inside every iteration. In V2 there
is no central sequencer for a CG chain: the chain's descriptor travels between the two units through
their Job FIFOs, and each unit does one half-iteration per descriptor: the SALSA pattern, where units
are configured by the messages passing through them. Chosen over the host sequencing every step (V1)
and over direct streams for `D` and `U` (the interconnect would carry little), because it gives the
DSE the most to decide.

### One job

One CG chain: a block of `c` columns of one sector's long-term update, factored form.

```
host:     the SRS and init stages write Ĥα (in both layouts the products need), X₀, R₀, D₀;
          write CgDesc{phase=MM, iter=1} into the systolic Job FIFO
systolic: read Ĥα, D[iter%2] → T = ĤαᴴD → write T; read Ĥα, T → U = Ĥα T → write U
          → wait for the write responses → CgDesc{phase=STEP} → vec Job FIFO
vec:      read X, R, D[iter%2], U → S = D + U, ω, X += ωD, R −= ωS, ν, D = R + νD
          → write X, R, D[(iter+1)%2] → wait for the write responses
          → iter < t ?  CgDesc{phase=MM, iter+1} → systolic Job FIFO
                     :  CgDesc{phase=W} → systolic: W = Ĥαᴴ X → write W → CgDesc{phase=DONE, status} → host
host:     woken by the Resp FIFO's interrupt; runs the chain's per-UE projections on the A53
```

In the explicit form the systolic half-iteration is one product, `U = K D`, with `K = ĤαĤαᴴ`
formed once per update as a phase of its own. The host sends a chain's first descriptor once its
inputs are in Shared Mem.

### The descriptor

`CgDesc` is a `DataList` that fits in one queue packet (`[len | data]`, one bus burst):

| field | what |
|---|---|
| `tx_id` | the chain, echoed in the status |
| `phase` | `K` / `MM` / `STEP` / `W` / `DONE`: what the receiving unit does |
| `iter`, `t` | the current iteration, and the iteration count (a workload field) |
| `rows`, `cols`, `c` | dimensions: N_rx padded, Ĥα's columns, the chain's columns |
| `addr_Hah`, `addr_Ham`, `addr_K` | Ĥα in the layout each product reads; `K` in the explicit form |
| `addr_X[2]`, `addr_R[2]`, `addr_D[2]` | ping-pong buffers, selected by the parity of `iter` |
| `addr_T`, `addr_U`, `addr_W` | the products' results |
| `status` | `OK`, or the first error; an error skips straight to `DONE` |

**The units hold no job state.** Everything that crosses a half-iteration is in the descriptor, or in
memory at an address the descriptor names. That is what lets several chains be in flight, and it
keeps the systolic array clear of a known problem: state held across commands makes csynth run out
of memory (`systolic_characterization.md`). The cost is memory traffic: `Ĥα` is read twice an
iteration (for `T` and for `U`), and the vector unit reads four `rows × c` operands and writes three.
That is already V1's traffic, on the Shared Mem port the study found busy 90–97% of the
long-term update (at 2024.1 and at 2025.1), and it is what the interconnect parameters trade against. Programs and formats are
configuration, not job state.

### Each unit's wrapper

Both units already sit behind V1's wrappers (`hw/sys_wrap.py`, `hw/vec_wrap.py`): a Job FIFO window, a
Ctrl register bank, the engine's memory port, and a done record into the Resp hub. V2 adds a
descriptor forward to each:

```
Job FIFO window (QueueIn) → desc_rx → the engine's job (its reads and writes over its memory port)
                               │
                               └─ desc ─► fwd ◄── the engine's done
                                           └─► bus writer (mm_stream_writer, credit) → the peer's Job FIFO window
```

- **Systolic wrapper.** Turns an `MM` descriptor into V1's `EngineJobDesc` jobs (`T`, then `U`; or
  `U = K D`), and a `W` descriptor into one more; forwards the next descriptor.
- **Vector-unit wrapper.** Turns a `STEP` descriptor into V1's `VecMemJob`, reads its status, and
  chooses the next hop. Programs are in Shared Mem, as in V1; later, as a build option, kept
  resident (decoding a pass costs about 16 cycles, 42% of a CG job, `vecunit_future.md`).

Everything in the diagram exists in V1 except `desc_rx` and `fwd`: the Job FIFO windows, the memory
ports, the bus writer with credit (`MmCreditStreamIF`), and the crossbar.

### Five rules that make it correct

1. **Write before notify.** `fwd` sends a descriptor only after the memory has acknowledged every
   write of the half-iteration (all B responses). Otherwise the peer reads stale data, and pysim
   will not show that race.
2. **No write into a full queue.** A write into a full window holds the crossbar's write channel and
   stalls other masters. So forwards go through credit, and every credit write is guarded on
   `wait_room()`; without the guard, the RTL deadlocks.
3. **Bound the ring.** If both Job FIFOs are full, each `fwd` waits on the other. The host keeps at
   most `J ≤ min(depth_sv, depth_vs)` chains in flight (V1's `cg_window`); the bound is a constraint
   in `space()`, and pysim asserts it.
4. **Ping-pong what is read and rewritten.** `D`, `X` and `R` have two buffers each, chosen by the
   parity of `iter` (V1's memory plan gives each iteration a buffer of its own).
5. **Errors are fatal and travel to the host.** A failing unit sets `status` and forwards the
   descriptor to `DONE` without doing its work (`docs/guide/systems/command_response.md`, applied to
   a ring).

### Parameters

- **Design:** V1's (`n`, `R`, `C`, `W`, `L`), and what V1 fixes: the Job FIFO depths, the
  interconnect width and burst sizes, the vector unit's register formats and divider.
- **Workload:** V1's (`t`, `form`, `target`, the symbol period), and `J`, the CG chains in flight.

Settle which vector-unit formats are build-time and which run-time before the adaptor declares them.

### Why it separates the arms

- **Chains in flight decide the throughput, and no formula does.** At `J = 1` the units alternate; at
  `J ≥ 2` one chain's products overlap another's vector half, and the bottleneck becomes the slower
  unit or the shared memory. A traditional agent must guess or pay RTL; pysim's per-block activity
  shows it in about a second.
- **The bandwidth trade is real.** Stateless units re-read their operands, so interconnect width,
  burst sizes and unit sizes trade against one another.
- **It scales to more units.** A descriptor with a next-hop field chains N units.

V1's timing is already emergent: the CPU schedules every job, and the engines, the Ingest DMA and the
host share the Shared Mem port (the 2024.1 study's job-level simulator ran 12% short of pysim's
long-term update until it modelled that sharing). So pysim's advantage should show on V1 already. V2
moves the sequencing on chip and takes the host's latency out of the loop, so the bottleneck moves
between the units and the port. The prediction: **pysim's advantage holds as the bottleneck moves.**

## What each phase needs

| phase | needs |
|---|---|
| 1, 2, 3 | the lab machine (Vitis / Vivado 2025.1, `xczu48dr`) |
| 4 | the lab machine; at most 7 days, set by the grid's RTL runs |
| 5, 6 | the lab machine (replay needs no tools) |
| 7 | API access |
| 8 | the lab machine; G2 sized by the same rule |

## Open questions

1. **The host's software time on V2** at high `J`: V1 prices it with the A53 model already; on V2
   it shrinks to each chain's first descriptor and its done.
2. **Where V2's initialization runs:** on the host (V1's Python `init` stage), or as an `INIT` phase on
   the vector unit; and the explicit form's `K` as a phase of its own on the systolic engine.
3. **The vector unit or `CgVectorUnit` as a V2 knob:** 3,447 against 3,581 cycles a job at 2025.1, and
   82.6k against 21.5k LUT. A finding of its own: what programmability costs.

Settled 2026-11-06: `k` is 2 (the lab machine's limit for heavy jobs); the agents' space is the grid,
so no request falls off it.

## Non-goals

- An MCP tool surface. The workspace, the skill and `dselib` are the interface; the service is the
  only boundary.
- Improving the models' forms, or active calibration: later, once the A/B result exists.
- Calibrating the crossbar, Shared Mem or the Ingest DMA (decisions 1 and 2).
- In V1's grid: N_rx 256, the slot at RTL, the dynamic policy, more than one core
  ([The space](#the-space-a-grid-measured-in-a-week)).
- A general DSE algorithm. The agent, or the scripted policy, chooses the points.
