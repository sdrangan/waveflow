# Plan: Agentic Architecture Exploration by Composition (dse_arch)

**Status: DRAFT for discussion, written 2026-11-05.** Follows `plans/dse_cg.md` and reuses its service, ledger,
identity, budget and submit machinery. Takes up the architecture exploration that
`plans/paper_cg_dse_vision.md` scoped out ("The fixed architecture"), and the agent split builds on
`plans/multi_agent_flows.md` ("Encapsulation, for agents", "The device as the unit"). Nothing here is built yet.

## 1. Question

`dse_cg` asks whether pysim speeds up *parametric* DSE on a fixed architecture. This plan asks the next question:

> Given a set of algorithms, a workload, and a benchmark, can a team of agents find good **hardware architectures**, meaning kernel partitions, interconnect, memory placement and host interface, by composing calibrated, verified blocks in the Python DES?

Generating one hardware block with an LLM is already known (see Related work). The contribution here is **composition**: an architecture search that runs on Waveflow's block contracts and calibrated system-level simulation, with block implementation as a separate, contract-bound service.

## 2. Core idea: split the agents along Waveflow's verification boundary

Waveflow already verifies and calibrates individual blocks, and gets system timing and resources from the DES. The agent roles follow that boundary.

| Role | Sees | Produces |
|---|---|---|
| **Architect** | Block contracts, workload, benchmark, `dselib` | Architecture graphs, block requests, submissions |
| **Block agent** | One block spec (interfaces, params, golden) | Verified block + calibrated timing/resource model |
| **Verifier** | Block contract + held-out test/calibration points | Accept / reject, model-error report |

The architect never sees HLS source. Block agents never see the system.

### 2.1 Block contract (the unit of exchange)

A block contract is a Waveflow object, not prose:

- Interfaces: typed ports (stream, stream-of-blocks, AXI-MM, register map) with data schemas (`FixedField`, `ComplexField`, ...).
- Parameters: `HwParam`s with legal ranges.
- Function: golden model (Python, bit-exact reference).
- Model: calibrated timing (phases, LT/CT) and resource model valid over a declared **calibrated range**, with error statistics.
- Status: `provisional` (analytic stand-in, no hardware) or `verified`.

### 2.2 Block request

The architect may request a block that does not exist (a fused kernel, a re-partitioned stage, a new interface variant). A request gives the interfaces, golden function (usually a composition of existing stage goldens), parameter ranges and a target (cycles, II, resources). The service charges block development (synth and calibration) to the budget.

While a request is pending, the architect explores with a **provisional model** and re-ranks when the verified block arrives.

## 3. Architecture representation

The architect edits a structured **architecture graph (IR)**, not free-form `HwSystem` code, so the service can validate it, hash it and cache it.

- Nodes: block instances (contract + `HwParam` values), memories (`MemoryMod`), host.
- Edges: typed channels (stream, credit/acked stream, AXI-MM via crossbar), widths, FIFO depths.
- Mapping: algorithm stages → block instances (many-to-one allowed for fused blocks).
- Host interface: register map, descriptor queues, polling or interrupt, DMA.
- Number formats on each edge (accuracy knob).

The service renders the IR to `HwSystem` / `DutTop` and to pysim. **Identity** extends from `dse_cg`: a part's identity is (contract, params), so a block synthesized for one architecture is reused at zero cost in another.

## 4. Algorithms as stage dataflows

Each algorithm is given as a dataflow of stages, with a golden model at every cut point. Then:

- Any legal partition is bit-exact at stage boundaries, by construction.
- Fixed-point formats on edges change accuracy, which the workload measures.
- System functional correctness follows from block correctness plus deterministic interface semantics. Deadlock and backpressure are composition properties that the DES must expose.

## 5. Benchmark suite (wireless)

| Algorithm | Notes |
|---|---|
| CG MIMO detector | V1 (central sequencer) and V2 (descriptor ring) already exist as reference architectures |
| MMSE / Cholesky MIMO detector | Same problem, different algorithm; tests algorithm-architecture co-choice |
| Channel estimation | Same task family as the AI Telco Engineer; candidate input from that loop |
| OFDM receive front end | FFT (SSR), CP removal, equalization; streaming, RF-converter-adjacent |

Workloads: K×N, SNR scenario, iterations, jobs in flight `J`, slot deadline or jobs/s. Objectives: Pareto over {throughput or latency at deadline, LUT/FF/DSP/BRAM, accuracy (BLER/MSE at SNR points)}.

## 6. Evaluation ladder (costs metered by the service)

1. `golden`: accuracy of the algorithm and number formats.
2. `pysim(arch)`: bit-exact, timed, resources from composed block models (with error bars).
3. `block_synth(contract, params)`: per-block synth + calibration (block agents).
4. `sys_synth(arch)`: full system synthesis (non-additive LUT, routing, timing closure).
5. `sys_rtlsim(arch)`: XSI co-simulation; the bit-exact reference and the cycle ground truth.

Submit rule: a submitted architecture must pass 4 and 5.

## 7. Scoring and ground truth

A replay grid cannot cover an open architecture space (open question 2 in `dse_cg` becomes fundamental here). Proposal:

- **Expert catalog (G3):** 10–20 hand-designed architectures per algorithm, each with a parameter sweep, measured on the lab machine (`xczu48dr`). Used for replay and for model-transfer validation.
- **Live verification:** every submission off the catalog is built and measured live, and is charged.
- **Held-out topologies:** calibrate component and interconnect models on part of the catalog; report pysim error on the rest. This is the key technical claim: *block-level calibration transfers to unseen compositions.*

## 8. What is being claimed, and which setting tests it

Waveflow's value has to be separated from the value of having a block library. The plan does this by **holding the library constant** across arms (Section 9) and assigning each block-availability setting to the claim it isolates.

| Setting | Blocks available | Claim tested | Role in the paper |
|---|---|---|---|
| **S1: fixed library, compose only** | Parameterized primitives; no new blocks | **C1:** a calibrated DES (pysim over block contracts) speeds up architecture search | Primary scored A/B |
| **S2: library + new blocks** | Same primitives; block requests allowed | **C2:** the block contract lowers the cost of adding a verified, calibrated block. **C3:** end-to-end, the flow finds better architectures per unit tool time | Secondary scored experiment; headline Pareto result |
| **S3: base Waveflow library only** | Stream utilities, Vitis L1, DSP blocks; most blocks must be written | Mostly LLM block-writing skill, which is already known | Demonstration only; not scored as an A/B |

S3 is not used for the comparison because variance in block writing would swamp the composition signal.

### 8.1 Library design (S1, S2)

- The library holds **primitives, not finished architectures.** No fused CG iteration block and no descriptor-ring subsystem. Otherwise the search is trivial.
- Primitives: `CgVectorCore`, `SystolicCore`, SSR FFT, Vitis L1 FFT, memory reader/writer, stream utilities, crossbar, queue/FIFO blocks, register-map host interface, DMA.
- Each primitive has a calibrated range wide enough for the benchmark workloads.

## 9. Arms and fairness

### 9.1 Arms

- **Waveflow arm:** block contracts (typed interfaces, golden models, calibrated timing/resource models with error stats), architecture IR, `pysim(arch)`, `dselib`, skill, generated `DSE.md`.
- **Traditional arm (representative industry flow):** the **same blocks** delivered as plain HLS IP: source, csynth reports, a short datasheet per block (ports, parameters, latency/II from csynth), plus Vitis L1 blocks. Composition via the same IR rendered to a block design (IPI-equivalent). Python golden models of the algorithm stages (untimed). Evaluations: `golden`, `hls_estimate`, `block_synth`, `sys_synth`, `sys_rtlsim`. May build its own analytic or spreadsheet models.

The arms differ only in the block contracts' calibrated models and pysim. The library, IR, golden models, documentation and budget are identical. A Waveflow win therefore cannot be attributed to the library.

### 9.2 Fairness rules (carried from dse_cg, plus)

1. Library held constant across arms (above).
2. All agents in an arm share one budget; block development is charged to it.
3. Calibration cost is charged to Waveflow, both upfront (library) and per new block (S2).
4. Block agents' models are checked by the verifier on held-out points before they become `verified`.
5. The traditional arm is strong: it reads everything, may build models, and has `hls_estimate`. pysim must beat composed HLS estimates, not only full synthesis.
6. Objectives, budget, `k`, N, model and skills are pre-registered.

## 10. Experiments

- **E0 (smoke, S1):** Give the architect V1's primitives and a throughput objective with `J ≥ 2`. Does it find a decoupled, descriptor-ring-style architecture (compare with V2)? Also checks that the library does not already contain the answer.
- **E1 (primary, S1):** Waveflow arm vs. traditional arm vs. scripted baselines on the benchmark suite. Anytime Pareto curves of best verified objective against tool time, as in `dse_cg`. Workloads chosen so system timing is emergent (contention, `J ≥ 2`, host polling); on purely analytic workloads the traditional arm is expected to keep up, which is reported, not hidden.
- **E2 (secondary, S2):** Same arms with block requests allowed. Report (a) the end-to-end Pareto front and (b) **cost per added block**: tool time and agent time from request to `verified` and calibrated, per arm.
- **E3 (information hiding):** Architect with vs. without access to block internals (HLS source, csynth reports). A small gap supports the claim that Waveflow's contract is a sufficient abstraction.
- **E4 (model trust):** Inject calibrated-range violations; measure how often the architect is misled, with and without error bars.
- **E5 (algorithm input):** Feed algorithms produced by the AI Telco loop; report the joint accuracy–cost front.
- **D1 (demonstration, S3):** Waveflow arm only, from the base library. Qualitative: which blocks the agents write and what the resulting architectures look like.

## 11. Baselines

1. Expert designs (V1, V2, catalog best).
2. Scripted search over the catalog × parameters (`policies.py` style). The agent must beat this, or the result is just parametric DSE over a larger space.
3. Classical partitioning/scheduling (e.g. SDF mapping or MILP) where the problem fits.
4. Single-agent architect that also writes blocks (no role split), in S2.

## 12. Phases

1. **IR + renderer:** architecture graph → `HwSystem` and pysim; V1 and V2 expressed in the IR, bit-exact with existing flows.
2. **Contracts:** wrap the S1 primitives as contracts with calibrated ranges and error stats.
3. **Traditional-arm packaging:** export the same primitives as plain HLS IP with csynth reports and datasheets; IR → block-design renderer. Gate: both arms can build V1 and V2 from the same library.
4. **Service extensions:** topology identity, `sys_synth` / `sys_rtlsim`; then block requests and provisional models (needed for S2 only).
5. **Composition transfer study:** calibrate on catalog subset, validate on held-out topologies. Gate: targets similar to G1 (DSP/BRAM exact; LUT/FF/cycles within ~5–10%).
6. **E0**, then pilots, pre-registration, E1 (S1). Then E2 (S2), E3–E5, D1.

Depends on `dse_cg` Phases 1–3 (service, `dselib`, ledger).

## 13. Open questions

1. ~~How much freedom block agents get.~~ **Resolved:** S1 (fixed primitives) is the primary A/B; S2 (new blocks allowed) is secondary; S3 (base library only) is a demonstration. See Section 8.
2. Granularity of algorithm stages: who defines the cut points, and can the architect propose new cuts?
3. Error-bar form for composed models (per-block residuals propagated through the DES vs. empirical system-level).
4. Host software time on the PS: use the processor model, or measure it?
5. Clock: fixed system clock or a design knob (affects timing closure in `sys_synth`)?
6. Whether to co-optimize algorithm choice (CG vs. MMSE) inside the loop or treat it as an outer loop.

7. In S2, can block agents write new HLS freely, or only compose/fuse existing primitives' code? Affects how much S2 results depend on block-writing skill.
8. Datasheet content for the traditional arm: how much timing detail (csynth latency/II only, or also interface handshake timing) is representative?

## 14. Related work (to expand)

- Single-block / algorithm-to-HLS agents for wireless: A2HCoder / A2H-MAS (arXiv 2508.10904).
- Agentic RTL: MAGE, Spec2RTL-Agent, AIVRIL2.
- LLM-guided accelerator DSE: SECDA-DSE (arXiv 2605.05920, 2606.11117), mostly parametric, AI workloads.
- System-scale agentic architecture: ArchAgent (arXiv 2610.23724), datacenter scale.
- Agentic algorithm discovery for wireless: The AI Telco Engineer (arXiv 2604.19803).
