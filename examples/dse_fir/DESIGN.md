# FIR agent surface: executable experiments, replaceable hosts

This implements the replay-first M0–M3 slice of [`plans/mcp_fir.md`](../../plans/mcp_fir.md).
The target is the project's [AI substrate](../../docs/overview/aiharness.md), not a new
agent framework. FIR is a pilot for the [CG DSE vision](../../plans/paper_cg_dse_vision.md):
exact fixed-point functional evaluation, approximate calibrated performance, sparse
expensive validation. It does not implement CG, RF orchestration, or live toolchain jobs.

```text
Hermes HTTP → server MCP      Pi extension        other MCP hosts
          |                       |                     |
          +--------- typed tools / JSON CLI / MCP -------+
                                  |
                     DseService (ordinary Python)
                    /             |              \
          immutable contract   SQLite evidence   declared budgets
                    \             |              /
                        FIR evaluator + backend
                       /                       \
         FirCompute.filter_block       canonical resource models
           framework serializers       committed HLS corpus replay
```

## Ontology, without a parallel graph database

- **Candidate**: one legal hardware parameter assignment; identity is its canonical JSON hash.
- **Experiment**: objective, constraints, evaluation protocol, platform, budgets, executable
  source identities, and backend/corpus identity. Immutable within a store directory.
- **Observation**: candidate × operation × experiment, including status, provenance, cost,
  wall time and result. A failed or unavailable operation is an observation, not missing data.
- **Decision**: a reproducible projection over observations. Feasibility is unknown until
  quality and replayed resources exist; predictions cannot establish measured feasibility.
- **Host session**: not part of experiment identity. A restarted CLI, MCP server, or agent
  resumes the same experiment directory without resetting its budget.

The contract is Pydantic; transport wrappers do not implement domain behavior. Plugins
select or expose capabilities rather than inventing resource numbers or reimplementing FIR.
Large future waveform artifacts should be referenced, not stuffed into model context.
The current bounded FIR results retain coefficients/diagnostics with their observation.

## Engineering defaults for this pilot

These are explicit, configurable experiment settings, not new project-wide research claims:

1. Constrained scalar objective: maximize normalized signal-domain stopband rejection,
   with passband SNDR, kernel throughput, DSP and LUT limits. Preserve raw observations
   so another decision rule can be evaluated without overwriting evidence.
2. Fixed architecture, host-selected filter spec and stimulus protocol. The agent cannot
   change its evaluator, budget or constraints halfway through an experiment.
3. Coefficients retain the hardware's shared sample format. Offline scale selection is
   constrained against the declared input peak; no change to `examples/fir_block` or its
   corpus. Separate coefficient format remains another workstream's hardware decision.
4. Replay corpus support is checked explicitly. Unsupported settings are not zero-resource
   designs and are not silently mapped to the nearest known point.
5. Expensive-query budget counts each uncached candidate, including failed/unavailable
   attempts; duplicate calls cost zero. A batch never silently drops members. Replay
   query units are not actual toolchain wall time or model-token cost.

## Numerical claims we deliberately do not make

The old plan's assertion that Layer 2 rejection must be below Layer 1 attenuation is not
a theorem: a worst-frequency response and a waveform-weighted mean-power statistic have
different denominators and aggregation rules. They remain separate diagnostics.
Gain normalization, input peak, seeds, warmup, sample count and evaluator identity are
part of the experiment. A change creates a new experiment, not an improvement to an old row.

`throughput_samp_per_cyc` is a closed-form compute-kernel rate assuming II=1, not an
end-to-end memory/board measurement. HLS estimates are not post-route utilization or
achieved physical timing. Resource regression support/residuals are not calibrated
confidence intervals; unknown uncertainty stays unknown.

## Scope and authority

The DSE profile exposes six domain tools, not authoring, arbitrary file access, shell,
network, or board commands. This narrows the domain interface; it is not a sandbox for
an agent host that independently exposes privileged tools. The scored Hermes run must
expose only these server-registered domain tools. The benchmark launches an isolated
Hermes home and gateway; request-level tool fields are not an authority boundary.

SQLite transactions cover the short local replay/evaluation call, yielding atomic
budget/caching behavior across processes. There are no external synthesis side effects
to recover. Live synthesis needs durable leased jobs, attempt IDs, cancellation and an
external-effect reconciliation protocol before this boundary can claim live support.
An agent disconnect is not a hardware cancellation mechanism.

## Extension seams

- Add a domain operation at the ordinary Python service boundary, then derive adapters.
- Replace the resource backend through its small protocol, retaining evidence kinds and
  explicit capability discovery. The current protocol is for bounded local operations.
- Custom Python evaluators require an explicit versioned `evaluator_identity`;
  hosts must update it when behavior or dependencies change. Backend identities
  likewise belong to the trusted host, never to model-controlled tool arguments.
- Package tactics and metric interpretation as portable skills; enforce authority and
  budgets in code, not prompts.
- Use Pi's upstream extension API for tools and presentation. Leave model integration,
  session compaction and interaction to Pi/Hermes; do not fork them.
- ACP is agent/editor interaction, not hardware tool transport. An external compatible
  Pi ACP adapter can host the extension; it is not reimplemented here.
- Let CG expose the next real abstraction. Resource composition includes integration
  overhead; timing under feedback/contention is not generally the sum of block latencies.

## Acceptance evidence

Lean tests cover numerical correctness/headroom, replay provenance, prediction uncertainty,
transactional identity/budgets, CLI/MCP parity and package wiring. The deliberate model
benchmark records the exact Hermes route/provider/model, calls, persisted observations,
known grid optimum, and equal-budget random/grid baselines. A successful tool loop is
not evidence that the model is a better optimizer; the score and limitations decide that.
