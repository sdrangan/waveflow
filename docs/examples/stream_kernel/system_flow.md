---
title: The polynomial's system flow
parent: Free-running polynomial
nav_order: 4
---

# The system flow

The kernel is verified at RTL as an [`HwSystem`](../../guide/systems/index.md): a simulation
and one named DUT. Here the DUT is the kernel, and the testbench is **generated from its ports**.

## The harness

```python
def system() -> HwSystem:
    return harness(PolyKernel, workload="test")
```

[`harness`](../../../waveflow/hw/harness.py) builds a fresh copy of the kernel and puts a stand-in at
each port: a stream driver on `s_in` playing the kernel's own `workload("test")`, and a sink on `m_out`
and on `m_status` recording what comes out. Each stand-in has a twin in the C++ testbench library, so the
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
- [`golden`](../../guide/build/xsi_system.md#golden) -- pysim's recordings against `PolyKernel.golden`
  (the `expected()` bursts of the same commands), burst for burst; a mismatch fails the DAG.
- `system_xsi` -- the RTL under XSI, with the stand-ins' C++ twins around it, from the same scenario.
- `compare` -- each sink's recording, RTL against pysim, word for word.

`compare` is the gate: the RTL's headers, results and statuses are the ones pysim's are -- including the
fatal `TLAST_EARLY` and the silence after it -- and pysim's are the golden's.

## Checking pysim against the golden

`compare` checks RTL against pysim; pysim against the golden is checked before it, by the `golden` step
on the DAG. The same check written out, from the harness's sinks: each `StreamSink` keeps what it recorded as `words`, one array per burst, so the check is a
comparison of bursts ([`test_stream_kernel.py`](../../../tests/examples/test_stream_kernel.py)):

```python
sysm = harness(PolyKernel, workload="test")
sysm.run()                                                     # pysim
got = {p: [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[p].words]
       for p in ("m_out", "m_status")}
want_out, want_status = expected(default_cmds())               # from the intents
assert len(got["m_out"]) == len(want_out) and len(got["m_status"]) == len(want_status)
for g, w in zip(got["m_out"] + got["m_status"], want_out + want_status):
    np.testing.assert_array_equal(g, w)                        # bit for bit, burst for burst
```

A burst is what TLAST closed, so comparing burst lists checks the framing too: a header and its results
are two bursts, a short results burst is short, and after the fatal error there are no more. To read a
burst as a message, deserialize it with its schema -- `Status().deserialize(words, word_bw=W)` -- never
by hand.

A build that changed nothing does no work: every system step is fresh while what it reads is unchanged
([freshness](../../guide/build/xsi_system.md#system-freshness)).

Next, [Timing](timing.md): how long the kernel takes, declared and then measured.
