# Plan: resources for a whole system -- kernels, RTL leaves, vendor IP and the top

**Status:** drafted 2026-10-11; Stages 1-7 built on branch `system-resources` the same day -- see
[Results](#results-2026-10-11).  Follows `plans/interconnect_platform_model.md`
(stages 1-3, merged in PR #249: the crossbar measured out of context, as module-store records) and
`plans/system_dag.md` (a system's whole flow on one `BuildDag`).  Worked case: **markov**; the same
steps serve **mm_fir**, the other design built by `add_system_steps`.

## Motivation

A Vitis kernel's resources are close to solved:

- `InspectSynthStep` (`waveflow/build/resource_steps.py`) reads a csynth report, attributes it to the
  kernel's modules (`waveflow/calib/synth_report.py`), and files `hls_estimate` records into the
  platform's module store;
- every module has a model (`get_rm`, default: a lookup against the store), and `compose(top)` rolls
  them up with a confidence per module (`EXACT` / `INTERPOLATED` / `EXTRAPOLATED` / `UNCALIBRATED`);
- the incremental build re-runs a top's csynth, and so its attribution, only when that top changed.

A **system** is not a kernel.  markov's generated top, `markov_top.v`, holds fifteen instances:

| kind | instances | measured today |
|---|---|---|
| HLS kernels | `u_gen`, `u_chain`, `u_u_fwd_wr`, `u_u_crd_wr` | csynth writes a report per top; markov's DAG never reads it |
| hand-written leaves (`waveflow/build/rtl/`) | 3 x `axi_slave_front`, `mm_queue_in` x2, `mm_queue_out`, `mm_credit_in`, `mm_bram_port`, `bram_t2p`, `mm_sync_fifo` | no |
| vendor IP | `u_xbar` (`axi_crossbar`) | only standalone, by `examples/xbar_profile` |
| glue, not an instance | each multi-view adaptor's address decoder, generated inline | no |

Nothing in the flow synthesizes the system top, so **a system's resource total is unknown**, and an
estimate composed from today's models silently omits everything but the kernels.  Two things about
the missing parts shape the design:

- **Most of the RTL is not a module.**  markov uses no `rtl_module()`.  Every hand-written leaf comes
  from an *interface or an adaptor* (`build_mm_device` views, a stream channel's FIFO, the crossbar);
  even the two bus writers -- HLS tops with csynth reports -- are brought by an interface
  (`MmCreditStreamIF`).  `compose()` walks modules only (`walk_modules`), so it cannot see them.
- **There is no list of what is in the top.**  Instance names are built inside the render functions
  (`_leaf_inst`, `_front_inst`, `render_adaptor_slot` in `mm_adaptor_gen.py`); `rtl.json` is a flat
  file list that names all eight leaf sources whether or not the design uses them.

## The target

What an agent (or a person) doing design-space exploration over a system should be able to ask for,
in milliseconds and without a toolchain:

```text
markov  gen.ntap=32  chain.depth=128  xbar=4x3/SAMD/64            platform zu7ev_bfm_250mhz
                         LUT     FF  BRAM  DSP   how            measured by
  gen  (MarkovGen)      2,140  3,050    0    4   EXACT          hls_estimate
  chain (MarkovChain)   3,900  4,210    2    0   EXACT          vivado_synth (in markov_top)
  u (MmCreditStreamIF)    640    720    1    0   EXACT          hls_estimate + vivado_synth
  xbar (AXIMMCrossBarIF)1,190    690    0    0   INTERPOLATED   vivado_impl (ooc)
  gen adaptor             310    280    0    0   EXACT          vivado_synth (ooc)
  mem (MemoryMod)           0      0    1    0   EXACT          vivado_synth (in markov_top)
  system integration      150    120    0    0   EXTRAPOLATED   --
  total                 8,330  9,070    4    4   weakest: EXTRAPOLATED (system integration)
  measured total        8,410  9,150    4    4   vivado_synth, 2026-10-11   gap +1.0% LUT
```

(Numbers illustrative.)  The rows are **pysim objects** -- modules *and* interfaces -- each the sum
of the RTL instances that realize it.  *How* a number was obtained and *what it measures* are separate
columns: an exact lookup of an HLS estimate can still be ~2x off on LUT.  This report is the
acceptance test of the plan.

## Requirements: keep the DSE agent surface open

`plans/mcp_fir.md` designs the DSE agent surface (six tools, a `dse` registry profile, a fidelity
ladder, five commitments).  This plan does **not** build or name tools; it builds the library they
would wrap, and must not foreclose them -- nor the alternative of an agent working in the full
workspace, which a group-meeting objection argued may do better (see [Not in scope](#not-in-scope)).
So every capability is a **library function first**, a CLI with `--json` second, and an MCP tool
only by the MCP plan's decision; no logic lives in a front door.

| requirement | from `mcp_fir.md` |
|---|---|
| **Estimating is a pure function of (design, parameters)**: elaborate, install models, compose. Milliseconds, no toolchain, no prior build. | `predict_resource(params)`, the ~10 ms rung |
| **The estimate is data, row by row**: counters, confidence, source tier, interval and support where fitted, `extrapolating`, `model_version` | `predict_resource`'s payload |
| **Failures are rows**: a part with no data is `UNCALIBRATED`, never zero; a configuration that misses timing is a record with `met: false` | commitment 1 |
| **Content-addressed, idempotent measurement**: every measurement is keyed by the identity of what it measures; a repeat is a cache hit | commitment 2 |
| **Cost before the call**: records carry `cost_seconds`; an estimate's weakest rows say what measuring them would cost | commitment 3 |
| **Prediction beside measurement**: the report keeps the composed prediction and the measured total and flags the gap | commitment 4 |
| **No silent narrowing**: every RTL instance appears; the ones with no model say so | commitment 5 |
| **Prediction reads what measurement wrote** | implicit in refit-as-records-land |
| **Measurement accepts a batch**, through `SweepRunner` | `synth` takes a list |

How a design is *named* in a CLI or tool call (a string such as `examples.markov.markov:MarkovSystem`
plus parameter overrides) is the MCP plan's to fix; this plan only requires that elaboration can
resolve one.

## Where each number comes from

| kind | measured by | scope | cost | identity |
|---|---|---|---|---|
| HLS kernel | csynth report (`hls_estimate`) | per task module, plus the kernel's integration term | free (csynth already ran) | the module's structure key (`module_key`) |
| HLS kernel, upgraded | system synthesis, in context (`vivado_synth`) | per kernel instance | shared: one run for the whole system | same key, `context` = the system top |
| hand-written leaf | system synthesis, in context; or out-of-context synthesis (library) | per instance / per configuration | shared / ~1-2 min per configuration, once | Verilog module + parameters + **source digest** |
| vendor IP | same, two ways | per instance / per configuration | shared / ~4 min (crossbar) | the IP config's hash (`AxiXbarConfig.identity()`) |
| system integration | system synthesis: total - sum(instances) | the inline decoders and cross-boundary optimisation | shared | the system top's identity |
| the whole system | system synthesis (total) | everything | ~5-15 min, opt-in | the system top's identity |

The source digest is what keeps a leaf's records honest: editing `mm_queue_in.v` changes its key, so
old numbers become stale rather than silently wrong (the same rule as `ModuleStore`'s provenance check).

**In-context vs out-of-context.**  One system synthesis measures every instance *in the design it
belongs to*, including optimisation across boundaries; it is the truth for that design.  The
out-of-context library measures a configuration once and is reused by every design, which is what
prediction before a build (DSE) needs.  Both file under the same key, distinguished by a `context`
field in the payload; `best()` prefers in-context for the design it came from.

## The instance inventory

`SystemTopSpec.instances() -> tuple[RtlInstance, ...]`, derived from the spec it already holds
(`kernels`, `mi` slots and their views, `nets`, `xbar`):

```python
RtlInstance(inst="u_chain_qu", module="mm_queue_in", kind="leaf",       # kernel | leaf | ip
            params={"W": 64, "DEPTH": 128}, sources=(Path(".../mm_queue_in.v"),),
            identity=ModuleIdentity(...), owner=<the pysim object it realizes>)
```

The single source for:

- the **renderers** -- `render_system_top` takes instance names from it instead of building them, so
  the inventory and the Verilog cannot disagree;
- the **file lists** -- XSI and synthesis compile exactly the sources the instances use (today
  `rtl.json` lists all eight leaves);
- the **join** to Vivado's hierarchical report, by instance name;
- the **store keys**, and the **report's grouping** by owner (module or interface).

## The DAG

markov's DAG today, outer steps (each `|-` an inner step):

```text
codegen -> csynth ------------------> system_rtl -------------> system_xsi -> compare
             |- CsynthStep x 4 tops      |- XbarIpStep               |- harness, build, run
                                         |- SystemTopStep
scenario -> pysim --------------------------------------------------------------^
```

With this plan:

```text
codegen -> csynth -+-> kernel_resources                        (seconds; always)
                   +-> system_rtl -+-> system_xsi -> compare
                       (+inventory) +-> system_synth           (minutes; opt-in: --through)
                                    +-> leaf_resources         (lookup; OOC synth on a miss, opt-in)
kernel_resources + leaf_resources (+ system_synth) -> resource_report
```

**Outer steps** for anything with its own cost or that a user would target (`--through
system_synth`); inner steps only for pieces of one job.  `kernel_resources` could equally be an inner
sibling of each `CsynthStep`; it is outer so `--through kernel_resources` works.

### `kernel_resources` -- one step for every Vitis kernel

The framework `InspectSynthStep`, generalized and wired into systems:

- **`CsynthTopsStep` exposes each top's report** (`<top>_proj/solution1`) and its `synth_seconds`;
  today an inner `CsynthStep` produces only `rtl_<top>`.
- **Host-activated attribution.**  `attribute_resources` claims rows by each module's `kernel_task()`
  prefix; a host-activated kernel has none, so today its whole report would become integration and no
  module record would be filed.  Rule: a top with no `kernel_task()` claims the top row as its own
  record (loop rows as breakdown); the unitemized `control_s_axi` / `m_axi` adapters stay integration.
- **Absorb the two local copies.**  `examples/regmap/simp_fun_build.py` and
  `examples/stream_inband/poly_build.py` each define a local `InspectSynthStep` that writes the loop
  table and fails on II > 1.  That gate becomes an option of the common step (`require_ii1=True`), the
  local copies go, and those examples start filing records.
- **The bus writers** (`mm_queue_writer_64_128`, `mm_credit_writer_64`) are `MmStreamWriter` tops;
  check that attribution maps them (`writer_rtl` naming), and record their owner as the interface.

### `system_synth` -- the system top in Vivado

One out-of-context synthesis of the system top, optionally placed and routed (`impl=False` default):

- **Sources**: the crossbar by `read_ip` on its `.xci` (`generate_synth_checkpoint false`, as
  `synth_axi_xbar` does), the inventory's leaf sources, each kernel's `syn/verilog/` output (all files,
  not only `*.v` -- a kernel with a ROM ships `.dat` init files), and the top.
- **Hierarchy**: Vivado's default `-flatten_hierarchy rebuilt` optimises across boundaries and rebuilds
  the hierarchy for reporting, so per-instance numbers come from the design one would ship -- the
  trade `plans/rtl_module.md` worried about (`KEEP_HIERARCHY` at a QoR cost) is not needed unless the
  attribution proves too coarse.
- **Report**: `report_utilization -hierarchical`, parsed by an extension of
  `waveflow/utils/vivado_report.py`; rows joined to the inventory by instance name.  An inventory
  instance with no row **raises** (the `UnmappedModuleError` rule: a dropped instance makes the glue
  look expensive).
- **Records**: per instance, `vivado_synth` (or `vivado_impl`) with `context` = the system top;
  one `integration` record for the system identity (total - sum of instances; negative is legal and
  not clamped, as for kernels); the total; with `impl`, routed slack and `met`.
- **Cached** under the system top's identity (inventory identities + the top's text), so a re-run
  with nothing changed starts no Vivado.
- **The part.**  This machine's Vivado cannot synthesize the RFSoC's `xczu48dr`; the step takes the
  platform's part and the stand-in practice of `examples/xbar_profile` applies (a platform named for
  the part measured on).

### `leaf_resources` -- the out-of-context library

For each leaf and IP instance: look its identity up in the store; on a miss, either report it
`UNCALIBRATED` (default) or, with `measure=True`, synthesize that configuration out of context and file
it.  The crossbar already has this (`synth_axi_xbar`); generalize it to `synth_rtl_ooc(sources, top,
params)` for the hand-written leaves.  Batched through `SweepRunner`.  Optional for a measured design;
needed for predicting one.

### `resource_report` -- the system estimate, and the check

`estimate_system(sysm, platform) -> SystemEstimate`:

- **Rows**: kernels from `compose()` over their modules; every non-kernel instance from its model or
  a store lookup by identity; grouped by `owner` into pysim objects; plus the system integration term
  (lookup by system identity, else `UNCALIBRATED`).
- **Per row**: counters, confidence, **source tier**, and where fitted the interval, support,
  `extrapolating` and `model_version` -- `ResourceEstimate.to_json()` grows to the `predict_resource`
  payload shape.  `weakest()` carries what measuring each weakest row would cost.
- **The check**: when `system_synth` has run, the measured total beside the prediction, with the gap.
  It is the area counterpart of `compare` -- with a stated tolerance rather than byte equality.
- **Interfaces get a model hook later.**  First cut: non-kernel instances are lookups by identity.
  When the crossbar has a fitted model (`plans/interconnect_platform_model.md` stage 5), the
  interface that owns it supplies it, through a hook mirroring `get_rm` on interface classes -- so a
  crossbar shape that was never synthesized is `INTERPOLATED`, not `UNCALIBRATED`.

### Store consistency: prediction reads what measurement wrote

Today `InspectSynthStep` writes to the build's platform (`config.platform_info.dir`, often a
work-tier platform) while a model's `get_rm` may read another store (vecmult's reads the committed
library).  Rule: **a build writes the work tier; lookups read work tier first, then the published
library** -- the order `platform_fallback_path()` already resolves.  Fitted models stay frozen until
refit and republished (`load_or_fit` is deliberately not refit-on-read); lookups see new records
immediately.

## Stages

One PR, one commit per stage.  Each stage keeps `pytest -m "not vitis and not xsi"` green and the
system gates unchanged (mm_fir 618 / 611, markov 1870).

**Stage 1 -- the instance inventory.**  `RtlInstance`, `SystemTopSpec.instances()`, leaf identities
(module + params + source digest); the renderers take names from it; `rtl.json` lists only used
sources.  Toolchain-free.  Gate: for markov and mm_fir, the inventory's instance names equal those
parsed from the rendered top; every instance has an owner; `-m xsi` gates unchanged.

**Stage 2 -- Vitis kernels in the system flow.**  `CsynthTopsStep` exposes reports; the common
`InspectSynthStep` with host-activated attribution and the II gate; `kernel_resources` in
`add_system_steps`; regmap and stream_inband ported.  Gate: markov files `hls_estimate` records for
its four tops, owners recorded; fir_block / vecmult attribution numbers unchanged; regmap's II gate
still fails a II > 1 loop.

**Stage 3 -- store consistency.**  Write work tier, read work-then-published.  Gate: a record filed by
a build is returned by the next lookup for that key; a published fitted model's prediction does not
move when a record lands.

**Stage 4 -- `system_synth`.**  Hierarchical parser, the step, records, cache.  Gate (Vivado, on the
stand-in part): markov's run attributes **every** inventory instance; sum(instances) + integration ==
total exactly; a second run starts no Vivado; each kernel now has a `vivado_synth` record beside its
`hls_estimate`, and the gap between them is recorded in this plan.

**Stage 5 -- `resource_report` and the estimate.**  `SystemEstimate`, the extended JSON, grouping by
owner, the predicted-vs-measured check; a CLI (`--json`) over the library function.  Gate: markov's
report lists all fifteen instances under their owners plus the integration row; before Stage 6 the
leaves read `UNCALIBRATED` (never zero); after `system_synth` the measured total sits beside the
prediction.

**Stage 6 -- the out-of-context leaf library.**  `synth_rtl_ooc`, `leaf_resources` with
`measure=True`, batched.  Gate: markov's leaves measured once, then a re-run starts no Vivado; the
composed prediction (no `system_synth` record used) against the measured total, gap recorded.

**Stage 7 -- docs.**  A *Systems* page beside `docs/guide/resource/composite.md` (the same three-term
decomposition one level up); correct composite.md's "you get this on every build" (true only where the
DAG has the step); the missing branch in `docs/guide/flows/concurrent_flowsteps.md`'s diagram
(interfaces -> vendor IP -> RTL top) and a sentence on where RTL-side resources come from; markov's
pages gain the report.

## Results (2026-10-11)

Built as planned, with these decisions and findings:

- **Stage 1.**  Names and parameter lists come from the spec objects both the renderer and
  `instances()` read (`view.inst`, `leaf_params`, `front_inst`, `StreamNet.fifo_inst`, `XBAR_INST`);
  rendered tops byte-identical.  The crossbar instance's `module` is the IP's generated name
  (`xbar_markov_4x3`), what Vivado reports.  A bus writer's owner is found by walking kernel endpoints
  for the `MmCreditStreamIF` whose bus masters include it -- a back-pointer would have moved keys.
- **Stage 2.**  A top with no kernel task and no children claims its one function row (the writers:
  680 of 1531 LUT; the `m_axi` adapter, 851, stays integration) or else the top row (`simp_fun`).
  Two walk fixes, neither moving a key: a credit endpoint whose halves are on the boundary is on it
  (`markov_chain`), and a childless top's ports are all boundary (`PolyAccel` alone -- caught by
  `test_poly_demo -m vitis`).
- **Stage 3.**  `Platform.dir` stays the *read* home (a published fitted model's artifact loads from
  it); `write_dir` is the primary root's copy, created with the manifest on the first write.
  `LayeredModuleStore` answers per `(key, target)` from the first tier holding a record.
- **Stage 4.**  Per-instance records file under a new target, `instance`, not `resource`: a
  composite kernel's whole instance is not its own cost, and `resource` is where a per-module lookup
  reads.  They carry the system's identity, and the estimate matches in-context records on it (not on
  the top's name: a stale run of the same top describes another design).  The crossbar IP is
  regenerated for the synthesis part (the simulation IP is the RFSoC's, which this Vivado cannot
  synthesize).
  - **First run (xc7z020, 10 ns, 653 s):** total 18593 LUT / 40130 FF / 2 BRAM18.  **`bram_t2p`
    inferred no BRAM** (512 x 64 -> 8769 LUT / 32832 FF; its front +3625 LUT of read mux): both ports
    were written in one `always` block, which Vivado's true-dual-port template does not match.
  - **`bram_t2p` recoded** with one `always` block per port (read-first, `ram_style = "block"`, the
    collision check under `translate_off`).  All 168 XSI gates unchanged.  OOC 512 x 64: 4 BRAM18 /
    2 LUT (was 83194 LUT / 32896 FF; 58 s, was 1249 s).
  - **Markov now (116 s):** total **6287 LUT / 7187 FF / 4 BRAM18**; worst slack after synthesis
    -0.70 ns at 10 ns.
  - **Attribution granularity under `rebuilt` (the open question below) -- answered.**  With block
    RAM, Vivado dissolved `u_mem_mem` and `u_mem` into the top: no rows, and the RAMB36 counted in no
    child.  `KEEP_HIERARCHY` on every instance keeps the rows but costs +25% LUT / +30% FF (markov
    7829 / 9314) -- a design nobody would ship -- so it is an option, off by default.  Instead an
    instance with no row is priced by the primitives under its name prefix (`wf_prims`: REF_NAME
    census, LUTRAM / BRAM weighted as `report_utilization` counts them); flattening keeps cell names.
    The census and the report's site-level LUT count differ slightly: markov's integration reads
    -36 LUT.
  - **Gap, hls_estimate vs in-context synthesis (LUT):** markov_gen 2585 / 1193, markov_chain
    4423 / 2032, queue writer 1531 / 811, credit writer 1097 / 508 -- csynth overestimates LUT ~2x;
    FF within ~30%.
- **Stage 5.**  The kernel row prefers this system's in-context record (the plan's example table
  shows `chain` as `vivado_synth (in markov_top)`), then a class model, then the `hls_estimate`
  records.  Views roll up into their adaptor's row.  CLI: `python -m waveflow.build.system_estimate
  <module:callable> --platform NAME [--composed] [--json]`.
- **Stage 6.**  Not a `SweepRunner`: the leaves are a heterogeneous list of configurations, not a
  grid.  `measure_instances` gives what the runner would -- failure isolation (a failed
  configuration is a row), and per-identity caching (a re-run synthesizes nothing).
  - **Gap, composed prediction vs measured total:** LUT +89.9% (11938 / 6287), FF +12.5%, BRAM
    +50% (6 / 4).  With in-context records the estimate reproduces the total exactly (the gate).
  - LUT: the kernels' hls_estimates, 5092 of the 5651-LUT excess; the crossbar OOC 1292 vs 849.
    BRAM: out of context `bram_t2p` has both ports live (two RAMB36); in markov port B is tied idle
    and one RAMB36 serves.  (Before the recode the composed gap was +412% LUT, nearly all the
    flip-flop memory's tied-off port: 83194 LUT OOC vs 8769 in context.)

**New open questions (from the results):** key a leaf on its tie-offs (an idle port is a
different configuration -- `bram_t2p` with port B idle), or price a tied-off instance in context
only; and calibrate the hls_estimate -> vivado_synth LUT ratio, which dominates the composed gap.

## Not in scope

- **The agent surface** -- tool names, the `dse` profile, `get_dse_context`, the design-naming
  convention: `plans/mcp_fir.md`.
- **The crossbar's fitted model**: `plans/interconnect_platform_model.md` stage 5; this plan only
  leaves the hook for it.
- **Tools vs workspace.**  A group-meeting objection: a fixed tool set saves tokens but may
  "lobotomize" an agent that would do better with the full workspace.  An empirical question; the
  experiment (tools only / workspace only / both, scored on the `mcp_fir.md` agentic test, spend taken
  from `waveflow.events` spans rather than the agent, scorer outside its reach) belongs to the MCP
  plan.  This plan's obligation is that the library carries every guarantee, so all three arms call
  the same functions.

## Open questions

- **Crossbar identity for area.**  `AxiXbarConfig.identity()` includes the address windows, so a design's
  own crossbar never matches a census record of the same shape.  Measure one shape under two address
  maps; if area does not move, drop the windows from the area key.
- **Route by default?**  `system_synth` with `impl=True` doubles the cost and gives closure, including the
  port-to-port paths an out-of-context crossbar cannot time.  Proposed default: synthesis only.
- **Attribution granularity under `rebuilt`.**  If cross-boundary optimisation smears small leaves into
  their neighbours, the integration term absorbs it; decide whether that is acceptable or whether leaf
  instances get `KEEP_HIERARCHY`.
- **What the system integration term is keyed on** for prediction: the system identity (exact, never
  generalizes) or a boundary signature like `InterfaceResourceModel`'s (decoder count and widths).
  Start with the identity; generalize when a second system is measured.
