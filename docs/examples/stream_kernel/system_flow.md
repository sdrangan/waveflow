---
title: The polynomial's system flow
parent: Free-running polynomial
nav_order: 4
---

# The system flow

The kernel is verified at RTL as an [`HwSystem`](../../guide/flows/concurrent_system.md): a simulation
and one named DUT. Here the DUT is the kernel, and the testbench is **generated from its ports**.

## The harness

```python
def system() -> HwSystem:
    return harness(PolyKernel, workload="test")
```

[`harness`](../../../waveflow/hw/harness.py) builds a fresh copy of the kernel and puts a stand-in at
each port: a stream driver on `s_in` playing the kernel's own `workload("test")`, and a sink on `m_out`
and on `m_resp` recording what comes out. Each stand-in has a twin in the C++ testbench library, so the
same harness runs in pysim and at RTL. There is no testbench code in the example.

`workload(kind)` is where the scenario lives:

```python
def workload(self, kind: str = "test") -> dict:
    cmds = {"test": default_cmds, "calib": calib_cmds}[kind]()
    return {"s_in": stimulus(cmds)}
```

## The steps

[`stream_kernel_build.py`](../../../examples/stream_kernel/stream_kernel_build.py) puts the flow on a
build DAG. After the example's own `codegen` (the headers, the lane routines, the top), every step is
the framework's (`add_system_steps`, [The system flow](../../guide/build/xsi_system.md)):

- [`csynth`](../../guide/build/xsi_system.md#csynth) -- Vitis HLS on the generated top; fresh while its
  source stamp matches.
- [`system_rtl`](../../guide/build/xsi_system.md#system-rtl) -- the Verilog top. With no crossbar, the
  kernel's three streams are the top's ports.
- `scenario` and `pysim` -- the stand-ins' input written once, and the system run in pysim from it.
- `system_xsi` -- the RTL under XSI, with the stand-ins' C++ twins around it, from the same scenario.
- `compare` -- each sink's recording, RTL against pysim, word for word.

`compare` is the gate: the RTL's results and responses are the ones pysim's are, and pysim's are the
golden's ([`test_stream_kernel.py`](../../../tests/examples/test_stream_kernel.py)).

A build that changed nothing does no work: every system step is fresh while what it reads is unchanged
([freshness](../../guide/build/xsi_system.md#system-freshness)).

Next, [Timing](timing.md): how long the kernel takes, declared and then measured.
