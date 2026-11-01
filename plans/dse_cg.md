# Plan: does pysim speed up design-space exploration? An agentic A/B on CG

**Status: drafted 2026-10-30, refocused 2026-10-31. Design plan, no code.** Replaces
`plans/mcp_fir.md` (last committed version in git history). The FIR pilot and the MCP tool surface
are dropped: the agent works in a workspace, drives a small Python library, and the experiment
compares two versions of that library.

**Version and part: everything at 2025.1.** Vitis / Vivado 2025.1 on both the laptop and the lab
machine, part `xczu48dr`, which the lab machine is now licensed to synthesize. No 2024.1 record is
used anywhere in this plan: not as calibration, not as ground truth, and not as a cost. The
`CgDetector` campaign's 1,440-build brute force was measured at 2024.1, so it is re-measured
([P1](#v1-the-experiment-on-cgdetector)) rather than replayed. Every recorded number comes from the
lab machine. Costs (tool-seconds) also come from its timing events, never from the laptop, which
uses `xczu7ev` as a stand-in for any synthesis it runs.

**Depends on [`calib_2025_1.md`](calib_2025_1.md) Stages 0–4:** the 2025.1 platform, and the
systolic unit, CG vector unit and vector unit recalibrated and scored once on fresh held-out sets.
That plan validates each unit alone. This plan validates the composed system, then uses it.

## The claim

The claim to prove: **adding pysim, a bit-exact simulation with calibrated, loosely-timed cycles
and resources, makes the whole design-space exploration faster than a flow that has only an untimed
bit-exact simulation plus RTL.**

| | traditional flow | Waveflow flow |
|---|---|---|
| **untimed bit-exact simulation** (the "systems team") | ✓ exact accuracy, fast; no timing, no resources | ✓ same |
| **pysim** (calibrated, loosely timed) | — | ✓ exact accuracy; approximate cycles, per-block activity and resources, each with a confidence |
| **synthesis + RTL simulation** (the "hardware team") | ✓ measured resources and cycles; slow | ✓ same |

Everything else is held identical: the agent, the harness, the brief, the hints, the budget, the
caching rules, the parallelism, and the ground truth. The only difference between the arms is one
function in the agent's library and one section of its brief that describes that function.

The result is a curve: the best **verified** design against the tool-hours charged so far, for each
arm. Verified means synthesized and RTL-simulated in both arms. The headline numbers are the
tool-hours each arm needs to reach a design within ε of the ground-truth optimum, and the quality
each arm reaches at a fixed budget.

## Why a workspace and a library, not MCP tools

Coding agents do their best work with a rich brief, general tools (a shell, files, Python) and a
documented library they drive by writing code. A loop over a hundred points is then one script, not
a hundred tool calls, and the agent can do its own analysis on the full data. The blind tests showed
the same thing: an agent writes code cheaply. So both arms get that setup. The one boundary that
remains is the expensive rungs, synthesis and RTL simulation. These go through a service that charges
their cost and enforces the budget, so neither arm can get around the price of a synthesis.

## The two vehicles

**V1: `CgDetector`, which exists today** (`examples/mimo_cg/hw/`). It has an untimed bit-exact golden
(`mimo_cg_fixed.py`, swept over 27 scenarios), cycle and resource models (`models.py`), and a campaign
driver (`campaign.py`) that measures a grid unattended. At 2024.1 the models scored DSP and BRAM
exact, LUT and FF within 0.9% and 2.3%, and cycles within 1.0%. Those scores have to be earned again
at 2025.1 ([P1](#v1-the-experiment-on-cgdetector)). V1 needs no new hardware. It does need lab-machine
time to re-measure its grid.

The catch: its timing is nearly analytic (counted trip counts plus a fitted overhead per tile), so a
traditional agent can write a decent cycle formula of its own. On V1, pysim's advantage should come
mostly from resources and from pruning early. That is still worth showing, and it is honest.

**V2: the descriptor-ring system**, built by this plan ([below](#v2-the-descriptor-ring-system)):
systolic array + vector unit + crossbar, sharing memory outside the DUT, with several jobs in flight.
Its timing is **emergent**: the units contend for memory, and the overlap depends on the number of
jobs in flight and on queue depths. No formula gives it. The prediction is that pysim's advantage is
larger here than on V1.

Two vehicles make a stronger result than one: **pysim's advantage grows as timing becomes less
analytic.** V1 comes first, because it costs no new hardware and gives the first result.

## The agentic flow (identical in both arms)

### The sandbox

Each run starts in a fresh directory containing:

- `CLAUDE.md`, **the brief**: the task, the system, the parameters and constraints, the metrics, the
  objective and budget, the library's API, and a few generic hints. It is generated from the code
  ([the brief generator](#the-brief-generator)). Both arms get the same text, except for the one
  section that documents `pysim`.
- `docs/` and `src/`: the system's architecture and module pages, and the Python and HLS source of its
  modules, copied in. A real hardware team reads the source, and a traditional agent may use it to
  derive its own analytic estimates.
- `cgdse/`, **the library**: the arm's API, a thin client to the service.

It does **not** contain the waveflow repository, the calibration records, or any ground truth.
Otherwise the traditional arm could import pysim or read the models, and either arm could read the
answers.

### The library

```python
from dselib import space, golden, hls_estimate, synth, rtlsim, wait, results, budget, submit  # both
from dselib import pysim                                                          # Waveflow only
# (superseded in detail by examples/dse_cg/skill/dse/dselib; this plan is to be slimmed to cite it)
hls_estimate(points) -> job      # csynth per HLS kernel: estimated resources, II, latency if computable

space()                          # parameters (kind, values), constraints, workloads; as in the brief
golden(params, workload)         # untimed bit-exact: {"mse_db": ..., "ber": ...}; no cycles, no area
pysim(params, workload)          # {"mse_db", "cycles_per_solve", "per_block": {busy, stalled_by_cause,
                                 #   occupancy}, "resources": {"lut": {"est", "level"}, ...}}
synth(points) -> job             # asynchronous; wait(job) -> rows {"params", "lut", "ff", "dsp",
                                 #   "bram", "ii", "status", "cost_s", "cached"}
rtlsim(points, workload) -> job  # wait(job) -> rows {"cycles_per_solve", "bit_exact",
                                 #   "per_block", "status", "cost_s", "cached"}
budget()                         # charged so far, remaining, jobs running
submit(params, report)           # ends the run; params must have been synthesized and RTL-simulated
```

`golden` and `pysim` return the same accuracy, because both are bit-exact. What `pysim` adds is
cycles, per-block activity and resources, all approximate and labeled as such.

### The service and the ledger

`synth`, `rtlsim`, `golden` and `pysim` are calls to a service running outside the sandbox. The
ledger lives there, so editing the client cannot change it. The service:

- **charges each call its recorded cost**, taken from the lab machine's timing events:
  - `synth`: csynth per changed unit, Vivado for the DUT top when asked;
  - `rtlsim`: elaboration plus simulation for a design point; simulation alone for a workload point
    on an already-built design (incremental XSI);
  - `golden` and `pysim`: their measured run time.

  The charge is the same in both arms;
- **caches by identity**: a unit already synthesized costs nothing again, and the same reuse applies
  in both arms, as it would for a real team;
- **runs at most `k` jobs at once** (the lab machine's worker slots), so the ledger tracks both
  tool-hours and wall-clock time to a decision;
- **returns failures as rows**: a missed II, a failed timing closure, a deadlock (with the cycle it
  stopped at), or an illegal point is a result, never an exception;
- **enforces the budget** and `submit`'s verification rule.

**Backends.** *Replay* answers from a frozen ground-truth table with simulated latency: the V1 brute
force, or V2's grid. *Live* runs the real flow on the lab machine. Every scored run uses replay, so
the arms see the same world and the optimum is known. Live mode exists to spot-check that replay
behaves like the real thing.

### The hints

The same short list in both briefs, written once, for example:

- synthesis and RTL simulation are expensive, so plan before spending them;
- workload parameters are cheap to vary on a built design;
- verify your final answer.

Pilot runs may tune the hints, but only in ways that apply to both arms. The tuned text is frozen
before the scored runs.

### The harness

The Claude Agent SDK (or `claude -p`, headless) runs in the sandbox. It may use file and shell tools
inside the sandbox and has no network access except the API and the service. It uses the same model
and settings in both arms. Load the `claude-api` skill before writing it. Each run records its
transcript, its token counts, and the service's ledger.

## Fairness rules

1. **One difference.** The arms differ in `pysim` and in its section of the brief. A diff of the two
   sandboxes shows exactly that.
2. **Calibration is charged to Waveflow.** pysim's models cost calibration builds: the units'
   builds in `calib_2025_1.md`, plus the system's integration builds in P1 and P8, all counted from
   the 2025.1 timing events. Results are reported twice: with that cost charged up front to every
   Waveflow run, and amortized over the tasks. The first is the conservative headline.
3. **A strong baseline.** The traditional arm has the source and docs and may build its own analytic
   models. Industrial teams do that with spreadsheets. A baseline that is not allowed to think would
   be a straw man. Both arms also have **`hls_estimate`**: the Vitis HLS csynth report for each HLS
   kernel (estimated resources, II, and latency where the compiler can compute one), the quick look a
   real hardware team takes before synthesizing. It covers the HLS kernels only; the hand-written
   adaptors and the vendor crossbar have no estimate. It is charged its recorded csynth time, cached
   per kernel identity, and comes free with G1, because csynth runs before every synthesis. So pysim
   must beat the HLS compiler's estimates, not only full synthesis; it should, because the compiler
   sees each kernel alone, and pysim sees the whole DUT, the glue, and the interaction between parts.
4. **Pre-registered.** Committed before any scored run: the objectives, ε, the budget, `k`, N runs per
   arm, the model id, and the brief text.
5. **Several tasks.** Three or four objectives (for example, different accuracy targets and
   cycle-budget constraints), so that no single answer can be learned, and so that the result does not
   hang on one lucky optimum.

## Agent-free policies alongside the agents

The vision asks that the core result not depend on an agent, because an agent adds variance. So each
arm also gets a **scripted policy**:

- **Traditional:** sweep accuracy with `golden`; keep the points that meet the accuracy target; rank
  them by a written analytic cost proxy, refined by `hls_estimate` on the leading candidates;
  synthesize and RTL-simulate them in that order until the
  constraints are met and the objective stops improving.
- **Waveflow:** run `pysim` over the whole grid; keep the predicted-feasible points; synthesize and
  RTL-simulate the top-k by predicted objective, widening k while the measurements disagree with the
  predictions.

The scripted policies give the agent-free comparison. The agents show that the advantage survives an
autonomous explorer, and they report what the agents did with the extra information.

## Measurements

| measure | what |
|---|---|
| **anytime curve** | best verified objective against charged tool-hours; median and spread over runs |
| **time to target** | tool-hours (and wall-clock with `k` workers) to the first verified design within ε of the optimum |
| **quality at budget** | the verified objective at the budget, against the optimum |
| **spend** | number of syntheses and RTL simulations, and the share of them spent on designs that failed a constraint |
| **agent cost** | tokens and agent wall time (secondary) |
| **errors** | final reports that contradict the ledger (a number never measured, a predicted number presented as measured) |

The ground truth for the optimum comes from the replay table. For V1, `fidelity.py`'s pre-registered
decisions can also score each run's choice the way they scored the models.

## The brief generator

`waveflow/dse/brief.py` builds the sandbox from the code. The code stays the single source of truth,
and the two arms come from one generator with a flag.

The **design adaptor** supplies what the code cannot say:

```python
class DseDesign(Protocol):
    name: str
    platform: str
    def space(self) -> ParamGrid: ...                  # build axes + workload axes
    def legal(self, params) -> str | None: ...         # None, or the reason
    def build(self, params) -> HwSystem: ...           # the system at this point
    def golden(self, params, workload) -> dict: ...    # untimed, bit-exact
    def pysim(self, params, workload) -> dict: ...     # calibrated: cycles, per block, resources
    def context(self) -> dict: ...                     # hand-written: description, architecture
                                                       # (text + doc paths), metrics, workloads,
                                                       # objectives, constraints, hints
```

From it, `build_sandbox(design, arm, objective, out_dir)` writes the sandbox:

1. **Modules.** Build the system at default parameters and walk the module tree. For each module, the
   class, its docstring, whether it is a synthesis unit and inside the DUT, and its `HwParam`s. Its
   source file (`inspect.getsourcefile`) and HLS sources are copied to `src/`.
2. **Docs.** Map classes to guide pages by scanning the docs' front-matter `api:` lists (e.g.
   `crossbar.md` lists `AxiXbar`). The scan goes in `waveflow/mcp/knowledge/corpus.py`, so `waveflow
   kb` gains it too. The adaptor adds the architecture pages. Everything is copied to `docs/`.
3. **Parameters.** Merge the adaptor's `space()` (build axes become `kind: design`, workload axes
   `kind: workload`) with the bound `HwParam`s, for type, default and description.
4. **`rebuilds`, computed.** For each design parameter, build the system at the default and at one
   other value, compare the synthesis units' identities (`identify_instance`), and list the units that
   changed. The brief then says exactly which units a change re-synthesizes, which is what the service
   will charge. A design parameter that changes nothing is reported as an adaptor bug.
5. **Constraints** are parsed and checked at the defaults, with the same evaluator as `legal()` and
   the service.
6. **Costs.** For each rung, the recorded cost per point for this design, taken from timing events,
   goes into the brief.
7. **The arm flag** includes or omits the `pysim` section of `CLAUDE.md`, and `pysim` in `cgdse`.
8. **Snapshot.** The generated brief for each arm is committed beside the adaptor. A fast test
   regenerates both and fails on any difference, and checks that the two differ *only* in the `pysim`
   section. A code change that alters what an agent is told then arrives as a diff in review.

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

The DUT is the systolic array, the vector unit, their front ends and the crossbar. Its resources are
measured on the whole DUT top, attributed per instance. The host and the shared memory sit outside it
and are not counted.

**Decided 2026-10-31: sequencing on chip, by descriptor passing.** There is no central sequencer. The
job's descriptor travels between the two units through memory-mapped queues, and each unit does one
half-iteration per descriptor: the SALSA pattern, where units are configured by the messages passing
through them. This option was chosen over the host sequencing every step (host latency inside every
iteration) and over direct streams for `P` and `S` (the interconnect would carry little), because it
gives the DSE the most to decide.

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
memory at an address the descriptor names. That is what lets several jobs be in flight. It also keeps
the systolic array clear of a known problem: state held across commands makes csynth run out of
memory (`systolic_characterization.md`). The cost is memory traffic. `A` (`K×K`) is re-read every
iteration, and the vector unit reads four `K×N` operands and writes three per iteration. That traffic
is exactly what the interconnect parameters trade against.

Programs and formats are **configuration, not job state**. The vector unit's three programs are the
same for every job of a given shape.

### Each unit's front end

Both units are stream kernels. `SystolicUnit` takes `A` and `B` inline in a `LinalgHeader` request,
and the vector unit takes three programs and its matrices on streams. So each unit sits behind a
front end made of free-running tasks: `CgDetector`'s `cg_cmd_rx` / `cg_load` / `cg_store` pattern,
moved onto a bus.

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
  - **Programs, first version:** fetched from shared memory at an address the host sets at
    configuration time. This needs no change to the vector unit.
  - **Programs, later, as a build option:** kept resident in the unit. Decoding a pass costs about 16
    cycles, 42% of a CG job (`vecunit_future.md`), and fetching the programs every half-iteration
    adds traffic on top of that.

Everything in the diagram exists except `desc_rx` and `fwd`:

- the queue windows `MemSlaveWStream` / `MemSlaveRStream` (the `mm_queue_in` / `mm_queue_out`
  leaves, with IRQ thresholds);
- the stream adaptors `MemRStream` / `MemWStream`;
- the bus writer with credit (`MmCreditStreamIF`, as in markov's `u_fwd_wr` / `u_crd_wr`);
- the crossbar.

### Five rules that make it correct

1. **Write before notify.** `fwd` sends a descriptor only after the memory has acknowledged every
   write of the half-iteration (all B responses; `CgDetector` uses a zero-length write's echo for
   this). Otherwise the peer reads stale data. pysim will not show that race.
2. **No write into a full queue.** A write into a full window holds the crossbar's write channel, and
   that stalls *other* masters' memory traffic. So forwards go through credit, and every credit write
   is guarded on `wait_room()`; without the guard, the RTL deadlocks.
3. **Bound the ring.** The two queues form a cycle: if both are full, each `fwd` waits on the other
   and nothing drains. The host keeps at most `J ≤ min(depth_sv, depth_vs)` jobs in flight, counting
   outstanding jobs against the done queue. The bound is one of the brief's constraints, and pysim
   asserts it.
4. **Ping-pong what is read and rewritten.** `P` and `rz` are read and rewritten in the same
   vector-unit pass, so each has two buffers, chosen by the parity of `iter`. `X` and `R` can be
   updated in place only if the vector unit reads each block before writing it back. Until that is
   shown, they get two buffers as well.
5. **Errors are fatal and travel to the host.** A unit that fails (a bad dimension, a vector-unit
   status error, a bus SLVERR) sets `status` and forwards the descriptor to `DONE` without doing its
   work. The host learns of the error from the status, never from a timeout. This is the
   command-response rule (`docs/guide/systems/command_response.md`), applied to a ring.

### Parameters

- **Design parameters** change the RTL, so a new point costs synthesis:
  - systolic: `R`, `C`, `L`, `form` (3- or 4-multiply), `word_bits`, `sob_depth`;
  - vector unit: lanes, `word_bits`, `VLMAX`, queue depths, register formats (`CgFormats`), the
    divider choice;
  - interconnect: data width and the adaptors' burst sizes;
  - the descriptor queues' depths.
- **Workload parameters** are command fields, so a new point costs only simulation: `K`, `N`, the
  iteration count, the SNR scenario, and the number of jobs the host keeps in flight (`J`).

Before the adaptor declares the vector unit's formats, settle which are build-time and which are
run-time, because that moves each one between the two classes.

### Why it separates the arms

- **Jobs in flight decide the throughput, and no formula does.** With `J = 1` the units alternate,
  and each is busy at most half the time. With `J ≥ 2`, one job's matrix multiply overlaps another's
  vector half. The bottleneck then becomes the slower unit or the shared memory, depending on the
  parameters. A traditional agent must either guess this or pay RTL to find out. pysim's per-block
  activity report shows it in about a second.
- **The bandwidth trade is real.** A stateless unit re-reads its operands every half-iteration, so the
  interconnect width, the burst sizes and the units' sizes trade against one another.
- **It scales to more units.** A descriptor with a next-hop field chains N units: the SALSA tile
  picture.

## Phases

### V1: the experiment on `CgDetector`

**P0. Prerequisite: the units at 2025.1** (`calib_2025_1.md` Stages 0–4). Not this plan's work,
but nothing below starts until each unit's held-out score has been reported. Port `models.py` to the
2025.1 platform: its per-block forms stay, and its coefficients come from the recalibrated units.

**P1. How good are the system-level predictions? Validate first, and measure the ground truth in the
same pass.** Before any experiment, find out whether pysim's composed predictions hold for whole
systems at 2025.1. If they do not, the A/B comparison would measure a bad model, not the method.

1. **Pre-register** (one commit, before any build): a grid **G1** of whole-`CgDetector` design
   points, stratified over the design parameters, with each point's workload values; the 6–10 points
   used to fit the **integration term** (the cost of the whole beyond the sum of its units), drawn at
   random and excluded from scoring; the acceptance targets; and the objectives that P3/P4 will use.
   Size G1 to what the lab machine can run in about a day. *Proposed: about 300 points*, against the
   1,440 at 2024.1, which took 57 tool-hours.
2. **Measure** G1 with `campaign.py` on the lab machine: csynth resources and RTL cycles per job for
   every point. Then fit the integration term on its builds only.
3. **Score once**, under `calib_2025_1.md`'s rule (score once; a miss is reported, not refit):
   - **resources:** DSP and BRAM, expected exact; LUT and FF, mean and worst error;
   - **cycles per solve:** mean and worst error;
   - **confidence:** the share of points the models marked `EXTRAPOLATED` or `UNCALIBRATED`, and
     whether those are where the errors are;
   - **decisions:** for each pre-registered objective, is the design pysim picks within 10% of the
     measured optimum? This is the score that predicts whether pysim will help an explorer.
     `fidelity.py` scored it the same way at 2024.1.
4. **Targets (proposed, to be fixed at pre-registration):** DSP/BRAM exact; LUT/FF mean ≤ 5%; cycles
   mean ≤ 5%; at least 90% of decisions within 10%. A miss stops the plan here: fix the model's form
   and draw a fresh held-out grid. The old one has been seen.
5. **G1 becomes V1's replay table.** Every point measured for validation is also a ground-truth point,
   so the experiment needs no further synthesis. The agents' space is exactly G1, which keeps both arms
   in a world that replay can answer.

The validation report is a result in its own right: the vision's make-or-break experiment at 2025.1,
made before any agent sees the system.

**P1b. The service, the ledger and the replay backend; the `cgdse` client for both arms.** This can be
built while P1's builds run, against a few G1 points as they arrive. Gates:
- the ledger charges identical calls identically in both arms;
- a cached point costs nothing;
- an illegal point and a failed build come back as rows;
- `submit` refuses an unverified design;
- for `pysim` and `golden`, the service returns exactly what `models.py` and the golden return.

**P2. The brief generator and the `CgDetector` adaptor.** Gates:
- both arms' sandboxes generate;
- the diff between them is exactly the `pysim` section;
- every design parameter's `rebuilds` is non-empty;
- the snapshot is committed.

**P3. The scripted policies on V1:** the first, agent-free result (anytime curves for both arms, with
calibration charged and amortized). This is already a paper figure.

**P4. The agents on V1.** The harness; pilot runs (two per arm) to tune the shared hints; freeze and
pre-register; then N = 10 runs per arm for each objective. Commit the ledgers, transcripts and the
analysis script as a record.

### V2: the descriptor-ring system

**P5. The system in pysim, bit-exact.** The `CgDesc` schema; the two front ends, with the vector
unit's programs fetched from memory; the host. Gates:
- `X` equals `linalg.cg`'s fixed-point golden, bit for bit, over the `steady` workload and three `K`;
- every job completes at `J = 1, 2, 4`, with job counts asserted;
- the host refuses a `J` above the ring's capacity;
- an injected error reaches the host as a status.

Before the system, run each front end alone in its unit harness, against a memory stand-in and a
peer-queue stand-in. That gives P7 its calibration units.

**P6. The per-block activity report**, pysim and RTL in one schema. Roll up the timed phases'
per-firing `stalled` flags (`waveflow/hw/timed_phase.py`) and the channel occupancy into busy cycles,
stalled cycles by cause, and occupancy per block. Read the same report from an XSI trace (trace
manifest + `waveflow/utils/trace.py`). Gate: on `CgDetector` and `markov`, the two reports agree
within calibration error and name the same dominant stall. Then on V2: each unit is busy at most about
half the time at `J = 1`, and more at `J = 2`. This is what `pysim` returns per block; it is useful
beyond this plan, so it gets a guide page.

**P7. V2 at RTL, then calibration.**
- The system flow (`add_system_steps`): the front ends as free-running kernels, queue windows, bus
  writers, the crossbar, an XSI trace gate, and the DUT top through `system_synth`. Gates:
  - bit-exact against pysim at `J = 1` and `J = 2`;
  - in the VCD, no descriptor beat before the last write response of its half-iteration;
  - a DUT-top total with per-instance attribution.
- Calibrate what P0 did not cover, each alone in its unit harness: the two front ends (`desc_rx`,
  `fwd`), and the crossbar at V2's widths if the census lacks them. The systolic unit and the vector
  unit come recalibrated from `calib_2025_1.md`.

**P8. V2's system-level validation and ground truth: P1 again, on V2.** Pre-register a grid **G2** of
about 200 design points (times the workload values), its integration-term builds, the targets and the
objectives. Measure G2 on the lab machine, fit the integration term, score once, and then use G2 as
V2's replay table. Expect 15–30 tool-hours: V2's builds are larger than `CgDetector`'s, and every
point needs RTL runs at several `J`. Choose G2 so that jobs in flight and memory contention decide the
optimum; otherwise V2 repeats V1. The scores for **cycles** are the ones to watch here. On V1 timing is
nearly analytic; on V2 it is emergent, and that is where a loosely-timed model is most likely to miss.

**P9. The experiment on V2.** P3 and P4 again: the same harness, generator, hints and analysis.

## What each phase needs

| phase | needs |
|---|---|
| P0 | lab machine (`calib_2025_1.md`) |
| P1 | lab machine, about a day of builds for G1 |
| P1b–P3 | laptop |
| P4 | API access |
| P5, P6 | laptop |
| P7 | Vitis on the laptop (`xczu7ev` stand-in for synthesis); lab machine for the recorded numbers |
| P8 | lab machine, 15–30 tool-hours |
| P9 | API access |

P0 (lab machine) and P5 (laptop) can start at the same time, and P1b can be built while P1's builds
run. V1's result does not wait for V2.

## Open questions

1. **Csynth or Vivado resources?** The V1 campaign scored csynth numbers. Use the same source in both
   arms and for both vehicles. *Leaning:* csynth for the experiment; Vivado for a few spot checks,
   reported separately.
2. **`k`, the parallel worker slots.** These change wall-clock time, not tool-hours. Report both, with
   `k` fixed at what the lab machine runs (to be measured).
3. **Out-of-grid requests.** Replay can answer only points in its table. Should the space shown to
   agents be exactly the table, or should an out-of-table request come back as an "unavailable" row?
   *Leaning: exactly the table*, so the two arms always see the same world.
4. **The host's software time on V2.** The host is outside the loop, but at high `J` its job-issue
   rate may limit throughput. It could be priced with the CPU model (`waveflow/cpu/`, Cortex-A53).
5. **Where the initialization runs on V2:** on the host, as an `INIT` phase, or as a `GRAM` phase too.
   Each choice moves work between the host and the DUT.
6. **`X` and `R` in place**, once it is shown that the vector unit reads each block before writing it
   back.
7. **The vector unit or `CgVectorUnit` as a V2 knob.** CG runs on either one: 3,447 against 3,581
   cycles a job at 2025.1, and 82.6k against 21.5k LUT. Exposing the choice is cheap, and it yields a
   finding of its own: what programmability costs.

## Non-goals

- An MCP tool surface for DSE. The workspace and the library are the interface. The service is the
  only boundary.
- Improving the models' forms, or active calibration. Both are later work, once the A/B result exists.
- A general DSE algorithm. The agent, or the scripted policy, chooses the points. The experiment
  measures how much pysim helps it choose.
