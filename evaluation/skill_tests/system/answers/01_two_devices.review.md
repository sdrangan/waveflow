# Review sheet: system/01_two_devices

For the reviewer, not the agent (no spec links this file). Fill one sheet per run. First run:

- `python -m pytest <trial folder> -q`: the trial's own tests (G1).
- `python evaluation/skill_tests/system/check_variants.py <trial folder>`: the trial's tests as given,
  then against a scaler that stops after its second job (`deadlock`) and one that writes one word fewer
  per output (`drop`). Wanted: PASS, FAIL, FAIL (E1, E2).
- `python evaluation/skill_tests/lint_schema_use.py <trial folder>`: hand-packing lines (G2).

The trial is started with the given devices:

```bash
waveflow blind-test --prompt evaluation/skill_tests/system/01_two_devices.md \
    --files evaluation/skill_tests/system/given_01/scale_msgs.py \
    --files evaluation/skill_tests/system/given_01/scale_dev.py \
    --files evaluation/skill_tests/system/given_01/checksum.py \
    --files evaluation/skill_tests/system/given_01/devices.py \
    --folder ../waveflow_blind_tests/system01_<arm> --message "..." [--skills waveflow/skills/waveflow-system]
```

(`_variants.py` is never given; `check_variants.py` copies it in for its own runs and removes it.)

Run: ____________  arm: [ ] MCP only  [ ] MCP + skill   model: ________  tokens in/out: ________
Skill loaded (from summary.md): [ ] yes, at tool call # ___   [ ] no

Score each item **2** (as the guide does it), **1** (partly), **0** (not done / done otherwise).
The reference is `ref_01/` (`system.py`'s and `build.py`'s docstrings and `DESIGN.md` state every
choice below).

## A. The system (`systems/rtl.md`, *The system*; `build/xsi_system.md`)

| # | item | the pattern | score |
|---|---|---|---|
| A1 | an **`HwSystem(sim, dut, ...)`** whose DUT is a `DutTop` of the two devices, the memory and the crossbar | a bare `Simulation` run by hand is 0; an `HwSystem` with the host inside the DUT is 0 | |
| A2 | the testbench is **derived**: the host is not listed; a `SwHost` with `scenario_bursts()`, or a stated reason it cannot have its C++ twin yet | | |
| A3 | each device built by **`build_mm_device`** from its own `mm_views`, ranges from `device.ranges(base)`, `assign_address_ranges`, the crossbar with the host **and** ScaleDev's two memory masters | | |
| A4 | the host waits on **interrupts**: an `IrqIF` per queue it waits on, endpoints with `irq=` | polling a register or a queue level is 0 | |
| A5 | `run_until` the host's completion, a `check` returning what the host collected, `fresh` (or an equivalent rebuild) | | |
| A6 | the host's outstanding work is bounded by what the queues hold (one job at a time, or a stated window) and Checksum's input queue holds the longest job, or the host waits for room | | |

## B. The build DAG (`systems/rtl.md`, *The system flow*; `build/xsi_system.md`, *Running it*)

| # | item | the pattern | score |
|---|---|---|---|
| B1 | **`add_system_steps`** for each device's harness and the whole system on **one** DAG, a `prefix` each | the whole system only is 1 | |
| B2 | the script runs through **`run_dag_cli`** (`--through`, `--status`) with a default that needs no toolchain | a hand-rolled runner is 1 | |
| B3 | the steps that need no toolchain (`scenario`, `pysim`, each harness's `golden`) were **run**; the trial says so | | |
| B4 | the steps not run (`csynth`, `system_rtl`, `system_xsi`, `compare`) are **named, with why**: Vitis / Vivado 2025.1, each kernel's HLS body, the host's C++ twin | silently omitted is 0 | |

## C. The golden and the scenario (`systems/verification.md`)

| # | item | the pattern | score |
|---|---|---|---|
| C1 | the system's expected results computed **from the intents** (the given `scale` and `checksum` functions on the inputs), not read back from a run | | |
| C2 | the scenario: at least four jobs, one longer than 1024 samples, one not a multiple of four | | |
| C3 | the error **mid-scenario** with jobs after it, and the test checks nothing after it is answered | an error as the last job is 1 | |
| C4 | each device alone **against its own `golden`** (its harness's `golden_outputs()`, the DAG's `golden` step, or its `golden(kind)` directly) | | |

## D. Choices stated (`DESIGN.md`)

| # | item | score |
|---|---|---|
| D1 | the address map, and whole-word buffers (the scaler writes the last word whole) | |
| D2 | how many jobs the host keeps in flight, and why no queue overflows | |
| D3 | what is not built and would be needed at RTL: the kernels' HLS bodies, the host's C++ twin; nothing worked around | |

## E. Tests that catch a broken device (`systems/verification.md`, *Every gate asserts counts*)

| # | item | the pattern | score |
|---|---|---|---|
| E1 | `check_variants.py`: the **deadlock** variant fails the trial's tests | | |
| E2 | `check_variants.py`: the **drop** variant fails the trial's tests | | |
| E3 | every test **counts first**: results, statuses or bursts, against the golden's count | values only is 1 | |
| E4 | every output field checked: status code, saturation count, every sample, the checksum | | |
| E5 | the given run passes (`check_variants.py`'s first line) | | |

## G. Gates

| # | item | score |
|---|---|---|
| G1 | the trial's own tests pass | |
| G2 | no hand-packing (the lint) | |

**Total: ___ / 48**
