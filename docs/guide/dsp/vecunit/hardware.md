---
title: Vector unit hardware
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 8
summary: "How the vector unit becomes hardware: the per-configuration header that hls.py generates on vecunit_core.h, the build steps (VecUnitHeaderStep, VecUnitCodegenStep, build_dag), and the tests -- the interpreter against an exact-rational reference, pysim against the interpreter, and the XSI RTL gates."
---

# Vector unit hardware

## Building and testing it

`waveflow/vecunit/hls.py` generates, per configuration, a header `wf_vecunit_<id>.h`: a
`wf_vec_cfg_<id>` struct with the union and exact types, the format tables, the shared quantizers
and the dividend-format divide, and the three task bodies, on the configuration-independent
`waveflow/build/vecunit_core.h` (the instruction and pass checks, slot decode, operand resolution,
the matrix load and store). `VecUnitHeaderStep` writes those headers for a set of configurations
(`vecunit_headers_dag(cfgs)` adds the schemas' and lanes' headers they include), and
`VecUnitCodegenStep` the headers, the unit's free-running top and its csynth script.
`waveflow.vecunit.build.build_dag(unit, workload=kind)` is the unit in its harness through the system
flow -- `VecUnitCodegenStep`, then csynth, pysim, the XSI RTL run and the comparison:

```bash
python -m examples.vecunit_cg.harness --root /tmp/vu_cg --through compare --workload steady
python -m waveflow.vecunit.report /tmp/vu_cg    # job cycles at RTL, csynth's resources
```

The tests: `tests/vecunit/` (every instruction against an exact-rational reference, random programs
chained against serial, the assembler's rules, the pysim unit against the interpreter) and the RTL
gates `pytest -m xsi tests/vecunit/test_unit_xsi.py` (a generic configuration on random jobs, CG and
the matched filter, each RTL run bit-exact against pysim). Co-simulation cannot judge these
free-running tasks with run-time stream-of-blocks locks; the XSI flow can.
