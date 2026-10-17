# Add synthesis and RTL

Start with **MemCopy at 32 and 64 bits, using four 512-byte jobs**. Both settings
already pass Python data checks. The next goal is to synthesize each setting, run
its exact RTL output, and compare results under the same workload. The byte workload
stays fixed while word count changes with width.

[Run the CPU demo](README.md) first. Work in `examples/dse_fir/execution/`, not the
legacy six-tool service. The branch includes upstream `c10da3002a5e51cd56bbfc9538f017d0c849a6c2`; the merge and
MCP SDK 2.x migration are covered in the [test record](../VERIFICATION.md).

## Code to change

- **`designs.py`** owns parameters, fixed conditions and design-specific execution.
  Add build/check logic here. Keep target device, clock and toolchain settings fixed
  for the experiment, rather than allowing the caller to change them per candidate.
- **`service.py::ExecutionService.invoke`** validates requests and saves each attempt.
  Add hardware dispatch here; keep design calculations in the adapter.
- **`../tests/test_execution_service.py`** and **`test_execution_designs.py`** cover
  transport, persistence and parameter propagation. Extend these tests for hardware.

Reuse `examples/mem_copy/mem_copy_build.py`. Its Python and codegen stages forward
bus width, but `_write_scenario_bundles`, `_calibrated_pysim` and `_mem_copy_design`
still use the default width. Trace factories also need candidate binding. Fix those
paths before trusting a two-width hardware comparison. The RTL runner must consume
the returned synthesis artifact, not merely declare an input while resolving a
relative file list against whichever build directory happens to be current.

## What must hold

1. **Build each candidate in its own directory.** Save a manifest with parameters,
   source and artifact hashes, target, clock and tool versions. Extend `get_design`
   identity to include HDL/C++ bodies, includes and build scripts, not just Python.
2. **Run the exact synthesized artifact.** Use the successful synthesis run ID as
   `synthesis_id`. Verify the manifest before RTL execution; reject missing, failed,
   mismatched or changed artifacts. The existing ID check alone does not do this.
   Saved artifact references must be existing files relative to the attempt directory.
3. **Check all copied data and completions.** Verify completion count and echoed
   transaction IDs. Separate `check_mem_copy_xsi_outputs` data/completion checks from
   its fixed cycle-count assertion: new candidates need measured timing, not a known-answer
   timing requirement. Record failures. A simulator exit code or VCD is not a correctness check.
4. **Compare the same timing events.** Python `end_cycles` is simulation end; the
   RTL checker measures last completion. Choose matched boundaries before comparing.
   Report RTL cycles, HLS estimates and host wall time separately; preserve disagreements.
   State event boundaries, for example first accepted command to last valid completion.
5. **Stress and cross-design checks.** Apply upstream hostile policies after the normal
   path works. Then check FIR discovery/Python and both transports through the same four tools.

Keep the four [tool signatures](../EXECUTION_SURFACE.md). Enable hardware capabilities
only after these checks pass. Provisioned vendor tools are required, but installation
alone does not implement the adapters. Do not substitute replay or introduce an
optimizer/new tool interface to make this path work.

## Upstream work to reuse

- [#280](https://github.com/sdrangan/waveflow/pull/280): `HwSystem`, unit harnesses,
  memory harnesses and calibration records.
- [#281](https://github.com/sdrangan/waveflow/pull/281): seeded stalls, delayed responses
  and credit-starvation tests. Add these after the normal path works.

Reuse `harness(module)`, `MemCalibStep` and identity-bound calibration records from
#280, and `add_robustness_steps` from #281 rather than creating parallel infrastructure.
The [simulation-speed study](https://github.com/sdrangan/waveflow/blob/c10da3002a5e51cd56bbfc9538f017d0c849a6c2/docs/evaluation/sim_speed/index.md)
separates setup from per-frame/transaction cost; use that distinction for runtime measurements.

Upstream's Markov composed-timing gate remains quarantined: 1,740 model cycles versus
1,870 RTL cycles. These are upstream observations, not new branch measurements or
proof of MemCopy calibration. Preserve that disagreement.

The merge retains upstream's fixed-point array change and the FIR headroom constraint.
The original pre-integration study remains at `71727ec`; refreshed evidence has new
source/dependency identities. Merging hardware primitives does not enable live adapters.

## Local checks

The [usage guide](README.md#test-or-extend-it) has the CPU and browser commands;
Chrome is needed for browser checks. Vendor-tool checks need the provisioned machine.
Run the demo into a fresh root and keep the raw directory alongside the exported study.

## Hand back

Provide the commit, tool versions, commands, results for both widths, synthesis/RTL
IDs, raw reports/traces, exported review and any failed or unsupported checks. Rerun the CPU and transport tests so FIR still
works. Use fresh study roots after source changes. Done means both widths pass the
declared functional checks through the shared interface, not merely that synthesis produced files.
