---
name: waveflow-system
description: Build and verify a Waveflow system -- HwSystem(sim, dut) with a DutTop of devices, a memory and the crossbar, the testbench derived (stand-ins, a SwHost with its C++ twin), each module alone in its harness (the cut), the build DAG (add_system_steps with a prefix per system, run_dag_cli, --through, scenario / pysim / golden / csynth / system_rtl / system_xsi / compare), golden(kind), error scenarios, hostile stand-ins, an exact cycle count, and tests that assert counts so a deadlock or a dropped word cannot pass. Use whenever a Waveflow design is put together into a system, simulated end to end, put on a build DAG, or its tests are written or reviewed.
---

# Waveflow systems: putting it together, and proving it works

A **system** is an `HwSystem(sim, dut)`: one `Simulation`, one module named the **DUT** (a `DutTop` of
devices, memories and the crossbar), and a **testbench** -- everything else, derived, never listed
(`systems/rtl.md`). The same object runs in pysim (`sysm.run()`) and goes on a build DAG to RTL. A kernel
is `waveflow-kernel`, a device `waveflow-device`, a pipeline `waveflow-pipeline`; this skill is the
system around them and the tests that keep them honest.

## The rules (all of them, every time)

1. **Bottom-up, a system per level.** Each module alone in its harness first (`harness(Mod,
   workload=kind)` -- the cut, `systems/pysim.md`), then the whole. Each is an `HwSystem`; each can go
   on the same DAG (rule 6).
2. **The DUT is named, the testbench derived.** `DutTop.of(name, sim, comps=[...], ifs=[...])` holds
   what becomes RTL: devices (`build_mm_device`), memories, the crossbar. The host and stand-ins are
   not listed. `HwSystem(sim, dut, run_until=..., check=..., fresh=...)`; `fresh` rebuilds it, so a
   run never reuses a spent simulation.
3. **Only what has an RTL twin joins an `HwSystem`.** A stream driver, a sink, a memory model, a
   `SwHost` (its C++ class in a header beside the example; `build/sw_threads.md`). A channel model has
   none: that study is pysim only. A `SwHost` must also give `scenario_bursts()` -- what its twin
   replays -- and wait on interrupts, never poll.
4. **A golden from the intents, pure functions.** Expected outputs computed from what each job asks,
   not copied from the stimulus. A module states it as `golden(kind) -> {port: [burst words]}` beside
   `workload(kind)`; the harness's DAG then has a `golden` step (`systems/verification.md`).
5. **Every test counts first.** A deadlock ends the run quietly: every process stops, nothing fails.
   Assert the number of bursts on each output, statuses, jobs answered, against the golden's count --
   then compare word for word, burst for burst. A burst is read with its schema (`deserialize`,
   `read_array`), never by hand.
6. **One DAG, a prefix per system.** `add_system_steps(dag, sysm, work_dir=..., top=..., prefix=...)`
   for each harness and the whole; `run_dag_cli(build_dag, ..., default_through=...)` gives
   `--through`, `--status`, `--force`. `scenario`, `pysim`, `golden` need no toolchain; `csynth`,
   `system_rtl`, `system_xsi`, `compare` need Vitis / Vivado (2025.1), every kernel's HLS body behind
   `kernel_task()`, and every host's C++ twin. **Say which steps you ran and which you could not.**
7. **Errors mid-scenario.** Put the bad job in the middle with jobs after it, and check what the
   golden says it silences: after a fatal error, nothing more is answered on any stream.
8. **Scenarios that reach the edges.** A job longer than any link or queue (more than one burst), a
   length not a multiple of the lanes per word, the smallest job, a value that saturates.
9. **At RTL, `compare` is the gate** (RTL against pysim, word for word, from the same scenario; a host's
   traces byte for byte), plus an **exact cycle count** with the tool version beside it, and hostile
   stand-ins (`add_robustness_steps`, `flows/units_hostile.md`). None proves timing (`systems/calibration.md`).
10. **If Waveflow cannot express it, stop and say so** (`build/xsi_system.md`, *What it does not do
    yet*), rather than wiring around it.

## Which page to read before which step

Read the page **before** writing the code, with `waveflow_get_doc` (MCP) or the `docs/guide/` checkout.

| step | page |
|---|---|
| the system, the DUT, what becomes what; the system flow's steps | `systems/rtl.md` |
| the cut: a module in its own harness | `systems/pysim.md`, `flows/units_harness.md` |
| golden, counts, errors, with a host, hostile, cycle count | `systems/verification.md` |
| every argument of `HwSystem`; the DAG's steps; scenario; what is not built | `build/xsi_system.md` |
| the testbench's RTL twins | `build/bfm.md` |
| a host: `SwHost` threads and its C++ twin | `build/sw_threads.md` |
| hostile stand-ins | `flows/units_hostile.md` |
| timing and resources, at a glance | `systems/calibration.md` |

Worked examples: `stream_kernel` (one kernel, its harness, its DAG), `block_eq` (four systems on one
DAG, a prefix each), `markov` (two devices, a memory, a `SwHost`, the cycle count), `memcpy` (memory
behind a pipeline). If a name is not on those pages, `waveflow_search` it -- **do not guess an API name.**

## The shape

```python
def make_system(kind="test") -> HwSystem:
    sim, clk = Simulation(), Clock(freq=200e6)
    ...                                            # kernels, build_mm_device each, memory, host
    xbar = AXIMMCrossBarIF(...); ...               # bind masters / slaves, assign_address_ranges
    ...                                            # an IrqIF per queue the host waits on
    dut = DutTop.of("pair_dut", sim, comps=[dev_a, dev_b, mem], ifs=[xbar])
    return HwSystem(sim, dut, run_until=host.done, check=lambda s: host.results,
                    fresh=lambda: make_system(kind))

def build_dag() -> BuildDag:
    dag = BuildDag()
    add_system_steps(dag, harness(DevA, workload="test"), work_dir=WORK, top="dev_a_top", prefix="a_")
    add_system_steps(dag, make_system("test"), work_dir=WORK, top="pair_top", prefix="pair_")
    return dag

def test_pair(kind):
    got, want = make_system(kind).run(), golden(jobs_for(kind))
    assert len(got) == len(want)                   # the count first
    ...                                            # then every field, every word
```

## Before you finish, check your own work

- [ ] Each module alone in its harness, then the whole; each an `HwSystem`.
- [ ] The DUT names only what becomes RTL; every testbench part has (or is said to need) an RTL twin.
- [ ] The golden is from the intents; every test asserts counts before values.
- [ ] An error sits mid-scenario with jobs after it; the scenario reaches the edges (rule 8).
- [ ] Would your tests fail if a device stopped after its second job? If it wrote one word fewer?
- [ ] The DAG has a prefix per system; you ran what needs no toolchain and named what you did not run.
