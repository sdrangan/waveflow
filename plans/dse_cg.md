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

The agents' space is the product of these values, restricted to the four systolic shapes (the
adaptor's `R` and `C` with that constraint), and every legal point is measured:

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
12 minutes at RTL against about a second in pysim (step 2c). That is where pysim should pay.

**What measuring it costs (measured at 2025.1 in step 2c, `costs.py`):**

| work | count | slot-hours | basis |
|---|---|---|---|
| part builds (Vivado) | 24 parts | about 2.3 | 346 s a part (median of the six points) |
| csynth | 21 kernels | about 0.3 (3.5 if every root compiles its tops) | 56 s a kernel; a root's tops about 6.5 min |
| RTL runs | 768: 160 new builds, 608 further runs | about 166 (193 if every run pays its build) | 902 s a new design's run, 744 s a further run; each run re-elaborates its top, 2-3 min |
| pysim and `golden` | every point; 8 accuracy tables | about 1 | 1.1 s a run; 216 s a table |
| **total** | | **about 169-196** | **about 4 days at two jobs at once** |

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

**1f. The devices' timing in the system, calibrated (decided 2026-11-07, after 1a's gates).** 1a's
gates passed pysim's end within 5% only at one bank and the BRAM's rate (+0.03%); at two banks
-6.1%, with the one-cycle memory -8.8% (one bank) and -12.4% (two), every endpoint's data equal.
Three causes, from each job's dispatch and Resp-pop times in the RTL runs' operation logs against
pysim's:
- a gather (the systolic feeder's coalesced read of a tile's panels) held a memory that serves one
  transaction at a time for all its bursts, so the host's reads waited a whole gather (up to 4k
  cycles) where the RTL makes them wait one burst. At one bank this alone lengthened pysim's run by
  about 5%, which hid the next two causes; at RTL a second bank gains nothing. Fixed in the framework:
  each burst holds the slave on its own (`AXIMMCrossBarIF.read_gather`).
- the systolic engine's read port idles between its bursts at RTL longer than pysim's 2.2 cycles
  (`TILE_BURST_CYCLES`, fitted to the tasks' pysim, not the RTL): about 16 cycles a burst with the
  one-cycle memory and 8-11 with the BRAM's, 176 bursts a job of the long-term update
  (`docs/examples/systolic/timing.md`: the `m_axi` adapter's gaps and the feeder's per-tile command).
- the vector device's wrapper (program loads, decoder, memory streams, writer) costs about 540-730
  cycles a job more at RTL at either rate: the unit's own calibration covers its job interval, not
  the wrapper.

The crossbar's model stays as it is. Each device is measured alone on Shared Mem as the system builds
it, traced, at both rates (`devsys.sys_system`/`vec_system(shared=True)`, `device_dag(shared=True)`);
the gaps are modelled as the traces show them and fitted there. *Gates:* 1a's five points within 5%,
then step 2c's six.

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

## Progress log

| date | step | what happened | evidence |
|---|---|---|---|
| 2026-11-07 | 1a | **Decision 2 realized without a memory outside the DUT.** `system_top` refuses a memory on the crossbar outside the cut ("an off-chip memory ... is not wired by system_top yet"), and the Ingest DMA's views cannot leave it either. So the modelled parts stay in the RTL simulation as simulation models -- Shared Mem as the framework's behavioural memory RTL, the DMA as its HLS RTL -- and are never synthesized nor counted (Shared Mem's block RAM by formula, the DMA's none): decision 2's substance, not its C++ form. New options of `SysParams`, each defaulting to the system as it was: `formats="design"` (`design_ranges`: drops 1-10, `t` up to 10, both targets; the vector unit's shift bound fixed at `DESIGN_S_MAX` 5), `mem_banks` 1/2 (bank 0: programs, `G'`, the ring, `Ĥα`, `K`; bank 1: the vectors; each planned by `plan_memory` on its own), `mem_rate` 2/1, `slot` (False: the long-term update alone). Framework additions, existing behaviour unchanged: `PipelinedMemoryMod` and `mm_bram_port_p1.v` (a read word a cycle, simulated memories only; its own class and leaf, so no existing record moves), `BramViewP1`/`BRAM_VIEWS` and the lookup in `system_top`, `read_gather` across slaves in `AXIMMCrossBarIF` (a gather that spans banks is read slave by slave), `synth_sub_ooc`/`synth_top_ooc` factored out of `synth_ooc_link`, `estimate_system` pricing a child module by its own model when it has no record, and `VecUnit.get_rm` (`vecunit_resources`). The C++ host twin starts its O-RU thread only when there is a slot (a thread waiting for ever keeps the XSI run from ending: the first `lt_` gates ran to the cycle limit and failed with every job, response and bus burst equal to pysim's). | `tests/examples/test_dse_cg_ltbf.py` (5 passed: no HLS change for `t`, `target`, banks, rate, slot; `W` and `form` change exactly the parts the grid says; the long-term run alone; end to end from a Sionna drop, bit-exact, SINR loss 1.33 dB median on drop 1 sector 1); fast suite 5,625 passed, 3 known failures |
| 2026-11-07 | 1b, 2a | `LtbfCgMemAdaptor`: `sys_params` (the default point is AC9's design with the options), `build_system`, roots per HLS configuration, `identities` (content: each sub-top's Verilog and its HLS sources), `hls_identities`, `sim_identity` (build and run), `rebuilds` -- computed, and equal to the grid table's prediction -- `architecture` (point-invariant: Shared Mem's banks one node, the crossbar's ports a size), `context`. **`mem_rate` changes the RTL** (its memory leaf), so a new value costs an elaboration, though no synthesis: 128 RTL builds, not 64, for the resizing rule. | `rebuilds()` = {R, C, form: sys; W: sys, vec; L: vec; banks: xbar; mem_rate, cg_window: none} |
| 2026-11-07 | 1d | `golden`: the fixed-point chain with the design's formats against exact float LTBF over drops 11-100, rank 1, cached per (`W`, `form`, `target`). At [-6, 14] it reproduces the study (`W` 16, `t` 3: 1.537 dB median); at [-6, 3] the envelope formats cost `W` 12 accuracy (`t` 3: 0.253 dB against the study's 0.069), `W` 16 almost none (0.024 against 0.023). | `hw/build/dse/golden/` (local) |
| 2026-11-07 | 1e | **pysim's resources**: `estimate_system` now prices the systolic engine (its tasks' models) and the vector unit (`vecunit_resources`) inside the wrappers; at the default point sys 42.5k LUT / 157 DSP / 24 BRAM18 against csynth's 38.6k / 157 / 24, and at 8 x 8, `W` 12: 46.5k / 283 / 30 against 53.4k / 283 / 30. Both unit models fit **csynth's** estimate, so pysim's resources track `hls_estimate`, not Vivado's. The wrappers' glue (the vector device's decoder, memory streams, writer and control: about 50k LUT and 52 DSP at the default), the hub and the crossbar have no model and read `UNCALIBRATED`: pysim underestimates the vector device by about a third until step 4's integration term. | `LtbfCgMemAdaptor.estimate` |
| 2026-11-07 | 3 | The service (live backend), `dselib`'s two transports, `DSE.md` generated (`brief.build_blocks`), costs from `costs.py` (filled by step 2c). | `tests/examples/test_dse_cg_service.py` (9 passed), `test_dse_cg_brief.py` (2 passed) |
| 2026-11-07 | 2a | What any adaptor needs moved into the base (`examples/dse_cg/adaptor.py`): `BUILT`, `MODELLED` and `TOP` as class attributes; `legal_with` (a point completed to a legal one, defaults first), `node_of`, `spec`, `architecture_at` and the parametric `architecture` (checked at each parameter's extremes), and `rebuilds` computed from identities. The LTBF adaptor keeps only what is LTBF's (`node_of` groups Shared Mem's banks into one node). The tool methods' stubs say the adaptor runs its own build flow. | `tests/examples/test_dse_cg_ltbf.py` (the architecture at every extreme, `rebuilds` equal to the grid table) |
| 2026-11-07 | 3 | The service's command line (`python -m examples.dse_cg.service --live`, which prints the `DSE_SERVICE` to export; the replay backend is step 5). **A failed tool call is a row**: `LiveBackend` returns `failed: <error>` charged with the time it took, the ledger keeps only `ok` rows, so asking again retries; an `rtlsim` whose synthesis failed is a `failed: synthesis` row. `DSE.md`'s module table renders the docstrings' literals as Markdown. The step-3 gate on the live backend: `dselib` through the six points of `points.py` (every call `ok`, `rtlsim` bit-exact, golden and pysim the same accuracy, a repeat free, a workload change a new run on the same build, a simulation-only change no new part). Two races fixed on the way: `synth_sub_ooc` wrote each sub-top's Verilog to one path for every point, so two processes could store one point's netlist under the other's key -- now a directory per content -- and `rtl_against_pysim` read the symbol endpoint a run without a slot never writes. | `test_dse_cg_service.py` (11 passed); `test_dse_cg_live.py` (`-m xsi`, after step 2c) |
| 2026-11-07 | 1a | **The gates at 2025.1** (AC9's design, long-term update alone unless noted; RTL cycles, pysim's error): one bank, BRAM rate 1,611,880 (+0.03%); two banks, BRAM rate 1,618,954 (-6.11%); one bank, one-cycle memory 1,189,582 (-8.81%); two banks, one-cycle 1,187,716 (-12.42%); end to end (one bank, BRAM rate, the slot) 5,488,089 (-2.42%, passed). Every Job FIFO, Ctrl and symbol word, every Resp per source and every data burst equal at all five. At RTL a second bank gains nothing and the one-cycle memory 26%; pysim gave the bank 5.7% and the rate 33%. An RTL run of the update takes about 16 minutes alone (13 of simulation). | `tests/examples/test_ltbf_cg_mem_dse_xsi.py::test_modelled_memory_at_rtl` (2 passed, 3 failed on the timing check) |
| 2026-11-07 | 1f | The user's decision: calibrate before going on (the choices were to widen the gate or model only the fixed costs). The causes and the plan are 1f's. The gather fix is in (`read_gather`, a burst a hold): pysim's bank effect falls from 5.7% to 1.0%, and what is left is uniform: -4.9% (one bank) and -6.3% (two) at the BRAM rate, -12.1% and -12.6% with the one-cycle memory. The device harnesses take Shared Mem as an option for the traced measurements. | each job's dispatch and Resp pop in the gates' RTL operation logs (`report.json`) against pysim's |
| 2026-11-07 | 1f | **What the RTL traces show, and the framework's answers.** A trace of a system run (AC9, Shared Mem at the BRAM's rate) shows every HLS master reading and writing in 16-beat bursts (HLS's default burst length), Shared Mem serving one at a time with 2n + 2 cycles a back-to-back read burst against pysim's 2n + 1, and the vector unit's writer waiting for each burst's response before the next (21 cycles a 16-beat burst against pysim's 17). pysim had read a tile's panels or a 256-word transfer as one transaction, and let an early-anchored write whose anchor had passed into memory without occupying it (79 of the update's 240 such writes, 31,936 words). In the framework now: an HLS master declares its burst length (`MMIFMaster.axi_burst`, 16 for the systolic feeder, `MemRStream` and `MemWStream`), and on a slave that serves one transaction at a time each burst holds it on its own; an anchored write holds such a slave for its words; a slave may declare a measured turnaround (`MMIFSlave.turnaround`, a memory's from a `mem_turnaround` record filed beside its timing, `fit_turnaround`), released after each transaction in the background. Both new attributes stay out of the module identity, so no record moves (`tests/calib/test_key_stability.py`). pysim's error now: -1.3% and -2.9% at the BRAM's rate (one and two banks), -7.3% and -7.9% with the one-cycle memory; the turnarounds and the writers' response wait are measured next, at 2025.1. | `tests/hw/test_serialized_bursts.py` (5 passed) |
| 2026-11-07 | 1f | **Gated: pysim within 1.5% of the RTL at every memory setting.** Traced at 2025.1 (the systolic and vector devices alone on Shared Mem, `device_dag(shared=True, trace=True)`): Shared Mem's turnaround is one cycle a back-to-back read at either leaf (34 cycles a 16-beat burst against pysim's 33; 18 against 17), filed beside its timing (`hw/build.py`: `file_mem_turnaround`, and `add_turnaround`/`TurnaroundStep` so a machine's flow measures it once, as `add_memcal` does the timing; `calib/` is local). With the one-cycle memory the engine is bound by its **A loader**, which writes its block an element a cycle (`systolic_engine_load_a_task`'s `ELEMS` loop): 128 cycles a 16 x 8 panel against 108 for its words; pysim now charges it in all three of the engine's models (task, flat, tile) -- the engine's own mixed `test` workload at 128 bits went from 23% short of its RTL to 8%. The long-term update: one bank +0.55% / two banks -1.12% at the BRAM's rate, -0.96% / -1.42% with the one-cycle memory; the end-to-end run with the slot -0.68%, no overrun. **Known limit, outside the DSE's timing:** the vector device's last CG step and equalizer chunk run 77-86% longer at RTL than pysim in its harness, so pysim's real-time slot is too fast (a symbol's credit returns after 245-288k cycles in pysim against 249-409k at RTL); the slot's end is set by the O-RU's pace, so it lands within 0.7%. `test_system_equals_the_fixed_chain` paces the O-RU at AC9's 1 ms: at 500 µs the RTL's own lags pass the two-symbol ring. | `test_serialized_bursts.py`, the systolic and LTBF suites; the gates' RTL runs against pysim at HEAD |
| 2026-11-07 | 2c | **The six points, measured** (Vitis HLS / Vivado 2025.1, the lab machine; RTL cycles, pysim's error at HEAD): defaults 1,611,880 (+0.55%), smallest 1,696,468 (-0.38%), largest 851,953 (-1.24%), R_changed 1,610,968 (+0.61%), sim_only 1,189,423 (-0.95%), workload_only 1,177,773 (+0.72%); every `rtlsim` bit-exact. `costs.py`: `golden` 216 s, `pysim` 1.1 s, `hls_estimate` 56 s a kernel (18 s for the hub to 280 s for the vector unit), `synth` 346 s a part, `rtlsim` 902 s for a new design's RTL and 744 s a further run. Every run re-elaborates its top (2-3 min: a top per run's tag), which the service does not charge to a workload change. **What the RTL says about the space:** at these settings the update is bound by memory or by the A loader, not the array -- 4 x 8 and 8 x 8 give the same cycles, `banks` and `cg_window` change nothing, the one-cycle memory saves 26%, `t` 2 27%; the shapes, `W`, `form` and `L` move resources. **Resizing rule:** 24 parts, 21 kernels, 160 RTL builds, 768 runs: 169 slot-hours (196 if every run pays its build), x 1.3 = 220-255 against 336 -- the grid stands; adding `t` 5 first (384 runs about 1.5 times as long) would take it to about 413. | `examples/dse_cg/points.py`; `hw/build/dse/points/*.json` (local) |
| 2026-11-07 | 4 | **G1 started.** Pre-registered first (`g1_prereg.py`): the grid as it stands, what is measured, pysim's scoring targets, and step 2c's six points as the only ones an integration term may be fitted on; then the agents' three objectives, chosen by the user before any G1 result was examined (`prereg.OBJECTIVES`: minimize LUT s.t. T_upd <= 5 ms at t 3 and median loss <= 0.1 dB at [-6, 3] dB; minimize T_upd at t 3 s.t. LUT <= 100k and loss <= 0.5 dB; minimize DSP s.t. T_upd <= 4 ms at t 2 and loss <= 0.3 dB). `grid.py` measures it in two processes split by the systolic part, resumable. The **integration term** (`integration.py`), fitted on the six points: the units' models track csynth, Vivado packs the systolic part into 55% of its LUTs and 73% of its FFs and maps its blocks to LUTRAM (2 BRAM18 at every shape), the vector device carries twice the unit model's DSPs less one, the hub and the crossbar are their syntheses; on the six points the integrated totals are within 3% LUT, 1% FF, 3 DSP, BRAM exact (in-sample: G1 scores it). The systolic DSP may miss the 'exact' target by 2-3 at some shapes, and pysim prices the two forms of the systolic part alike where Vivado differs by about 1,300 LUT. | `examples/dse_cg/grid.py`, `g1_prereg.py`, `integration.py` |
| 2026-11-07 | 5 | **Replay and the sandbox, written** (while G1 measures). `service.ReplayBackend(design, table)` answers every evaluation from G1's table at the tool time it was measured -- a part's synthesis and a kernel's csynth by identity, an RTL run by its simulation identity, `golden` and `pysim` by the key the service caches them under; anything else is `not_in_space` -- and `python -m examples.dse_cg.service --replay <table>` serves it. `grid.py table` consolidates G1's rows; `grid.py finish` adds pysim and `golden` over the grid. `brief.build_sandbox(design, arm, dir)`: the skill and `DSE.md` for the arm, and every file `DSE.md` links at its repository path (a documentation page with its section), 740 KB, no `CLAUDE.md`; the two arms' sandboxes differ in `DSE.md` and the skill only, by the `arm:waveflow` blocks, and every link resolves inside. `run_agent.py` is step 7. | `test_dse_cg_brief.py` (3), `test_dse_cg_service.py` (14) |
