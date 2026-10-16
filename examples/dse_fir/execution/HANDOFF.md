# Synthesis / RTL collaborator handoff

## Start here

- [Research presentation](presentation.html): question, structure, actual Python results, next experiment. Download/open the HTML or serve the repository locally; GitHub does not render it inline.
- [Run the CPU demo](README.md) and [inspect recorded evidence](../evidence/execution_study.json).
- [Execution contract](../EXECUTION_SURFACE.md): four tool signatures and evidence semantics.

The implementation demonstrates real FIR and MemCopy Python execution. It does **not** yet execute synthesis or RTL through the shared service. The existing six-tool FIR replay pilot is separate; do not extend it by mistake.

## Reconcile upstream before hardware integration

At handoff, the tested implementation commit `392a3c76afa22a0d9e6dbb27f3d599b4ed905331` has 639 commits on `main` not in its history. Upstream was inspected at `c10da3002a5e51cd56bbfc9538f017d0c849a6c2`. No merge/rebase was performed, and compatibility with that newer main is **not tested**.

Sundeep's recent merged work is directly relevant:

| Upstream work | Reuse / implication |
|---|---|
| [PR #280: unit harnesses, stages 2–4c](https://github.com/sdrangan/waveflow/pull/280) | `HwSystem`, `harness(module)`, memory harnesses, identity-bound calibration records and `MemCalibStep`. Prefer the maintained primitives over parallel adapter infrastructure. |
| [PR #281: hostile stand-in policies](https://github.com/sdrangan/waveflow/pull/281) | Shared seeded stall/late-response/credit-starvation scenarios and `add_robustness_steps`. Add stress tests after the normal functional path works. |
| [Simulation-speed evaluation](https://github.com/sdrangan/waveflow/blob/c10da3002a5e51cd56bbfc9538f017d0c849a6c2/docs/evaluation/sim_speed/index.md) | Separates setup from per-frame/transaction wall cost. Reuse its measurement discipline rather than interpreting this demo's cold-import timings as speedups. |

Upstream reports successful unit RTL checks, but its Markov composed-timing gate remains quarantined: 1,740 model cycles versus 1,870 RTL cycles. Those are upstream results, not measurements from this branch or proof that MemCopy timing is calibrated.

First make an integration branch with the agreed current-main strategy, reconcile the adapter imports/build APIs, and rerun the CPU/transport gates. Preserve the committed study as historical evidence; generate a new study after source changes.

## Where to extend

| File / path | Current behavior → required work |
|---|---|
| `execution/service.py::ExecutionService.invoke` | Dispatches Python; hardware falls through to `unavailable`. Add hardware dispatch without moving workload logic into the service. `rtlsim` already validates that its ID identifies successful synthesis in the bound experiment. That guard alone is **not** artifact integrity checking. |
| `execution/designs.py::Design` | Owns legal parameters, fixed conditions, metrics and capabilities. Add trusted build/evaluation implementations and enable capabilities only after their gates pass. Extend host conditions for fixed target/clock/toolchain settings; never make them arbitrary candidate fields. |
| `execution/designs.py::get_design` | Current source closure hashes Python and selected dependency versions. Hardware identity must additionally cover HDL/C++ task bodies, includes, scripts, target, clock and tool versions. |
| `examples/mem_copy/mem_copy_build.py` | Existing canonical build path. Reinspect its current-main version; do not assume this branch's older DAG still describes the maintained integration. |
| `tests/test_execution_service.py`, `test_execution_designs.py` | Protect strict transport, durable outcomes and actual candidate propagation. Add the new hardware evidence-contract tests here, with hardware execution gated appropriately. |

Current artifact references must be existing files relative to the attempt directory. A synthesis manifest can be such a file. Use the successful synthesis run ID as the `synthesis_id`; RTL must resolve and verify its manifest, never select a build by whatever happens to exist in a mutable directory.

## First hardware experiment

**MemCopy:** 32-bit and 64-bit buses; four jobs of 512 bytes each. The byte workload stays fixed while word count changes with width.

1. **Synthesis:** propagate the candidate through DUT generation, testbench/scenario generation and trace elaboration. Create an isolated build and record source, parameters, target, clock, toolchain and artifact hashes.
2. **Artifact-bound RTL:** consume the exact successful synthesis artifact. Reject unknown, failed, mismatched or tampered bindings; record failures rather than dropping points.
3. **Functional checks:** compare all destination data, completion count and echoed completion IDs. A simulator exit code or a VCD alone is not correctness evidence. Keep expected cycle counts separate from correctness.
4. **Matched timing:** measure the same event boundaries in Python and RTL (for example first accepted command to last valid completion). Current Python `end_cycles` means simulation end and is not an interchangeable latency measure.
5. **Reports:** distinguish HLS resource/clock estimates, RTL-observed cycles and host wall time. Preserve disagreements and unsupported fields. No physical timing-closure claim without implementation evidence.
6. **Stress and cross-design regression:** apply relevant upstream hostile policies, then verify FIR discovery/Python and both transports still work through the same four tools.

Installing vendor tools alone does not complete these steps. Do not replace unavailable operations with replay. Do not introduce an optimizer or a new tool interface to make the hardware path work.

## Local gate and handback

```sh
uv sync --extra dev
uv run pytest examples/dse_fir/tests -m 'not vitis and not xsi'
uv run --no-project --with playwright python examples/dse_fir/tests/test_execution_visualizer.py
uv run python -m examples.dse_fir.execution.demo --output ./execution-demo-new
```

Use a new output root whenever source, dependencies or conditions change. Retain raw run directories as well as the exported study. Browser checks require Chrome; hardware checks require the provisioned vendor-tool machine.

Return: exact commit and tool versions; commands run; per-candidate correctness and timing/resource results; synthesis/RTL artifact IDs; raw reports/traces; exported review; and any failed or unsupported checks. Completion means both widths pass declared functional checks through the shared interface—not merely that synthesis produced files.
