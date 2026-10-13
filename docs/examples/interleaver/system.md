---
title: The system
parent: Composite kernel interleaver
nav_order: 5
summary: "The testbench graph that ran in pysim, with one more fact stated: which part of it is the design. InterleaverInbandSystem cuts the graph at the interleaver kernel; from that the system flow derives the Verilog top around the synthesized kernel, the harness of the participants' BFM models answering its ports, and the scenario, so the RTL run cannot drift from the model it is checked against."
---

# The system

The [DUT codegen](./codegen_dut.md) lowered the design graph to an RTL top that is **free-running**
(`ap_ctrl_none`): it owns its two `m_axi` bundles and has no `ap_ctrl` start/done handshake. Vitis
C/RTL cosim can only drive a kernel it can *call*, so it cannot drive this top at all. Instead the
RTL is driven cycle by cycle through [XSI](../../guide/build/bfm.md) by a C++ harness, and the
harness is derived from a **system**.

A system is the [testbench graph](./testbench.md) plus the **cut**: which modules are the design
under test. For the interleaver the cut is the kernel alone. The command driver, the completion
sink, the memory, and the crossbar that models the two bundles' contention in pysim are testbench.

## `InterleaverInbandSystem`

The system is a [`GraphSystem`](../../guide/build/xsi_system.md#graph-system) in
`examples/interleaver/interleaver_inband_sim.py`. It states only its own four parts:

```python
@dataclass
class InterleaverInbandSystem(GraphSystem):
    sizes: tuple = (256,)
    n_cycles: int = 4000

    def graph(self) -> InterleaverInbandTB:        # the testbench graph pysim runs
        self.runner = InterleaverInbandSim(sizes=tuple(self.sizes), name="xsi_tb",
                                           n_cycles=int(self.n_cycles))
        return self.runner.tb

    def dut_modules(self, g) -> list:              # the cut: the kernel alone
        return [g.dut]

    def write_inputs(self, root) -> None:          # the scenario: InterleaverInbandSim's one writer
        self.runner.write_scenario(root)

    def check(self) -> "InterleaverInband":        # the golden, on a pysim run
        return self.runner.check()
```

The scenario is parameterized by the job-size tuple `sizes`, one `InterleaverCmd` per job, of
possibly different lengths (variable-length gather). The RTL gate runs twelve jobs of 64.

## What the system flow derives

`examples/interleaver/interleaver_inband_system_build.py` hands the system to `add_system_steps`,
which derives three things.

**The system top.** The csynth'd `interleaver_inband` kernel sits inside it. `s_cmd` and `s_done`
become AXI-Stream ports, and the two `m_axi` bundles (`m_in` reads `P` and `X`; `m_out` writes `Y`)
become AXI4 master groups.

**The harness.** Each testbench participant is answered by its own declared BFM model
(`bfm_model()`):

| pysim participant | XSI model | answers |
|---|---|---|
| `StreamDriver` | `AxisMaster` | `s_cmd`: offers each `InterleaverCmd` word |
| `MemoryMod` | `FlatMemory` + `AxiMmReadSlave` / `AxiMmWriteSlave` | the two `m_axi` groups |
| `StreamSink` | `AxisSlave` | `s_done`: always ready, keeps the echoed descriptors |

Behind the two bundles the memory expands to three C++ objects: one `FlatMemory` arena holding `P`
and `X` (and capturing `Y`), plus a slave per bundle serving it. In pysim the crossbar is one
interface; at RTL each bundle is a port group of its own. The models come from the framework's BFM
library and know nothing about the interleaver. Each obeys the same `sample` / `update` / `drive`
phase split as pysim's lifecycle, so a beat decided from values sampled *before* the rising edge is
applied *after* it.

**The scenario.** `InterleaverInbandSim.write_scenario` writes it once. It is the same writer that
`check()`'s pysim run uses:

- **`vectors/s_cmd`**: the serialized `InterleaverCmd` words the `AxisMaster` streams in;
- **`vectors/mem_in`**: the arena with `P` and `X` placed at each job's offsets;
- **`vectors/golden`**: the expected `Y = X[P]`, for the check (not consumed by the C++);
- **`vectors/run.json`**: the run's cycle bound.

There is **no golden in the C++**. The models record what crossed the cut: the memory dumps its arena
(`vectors/out`, so the captured `Y` regions), and the sink its completions (`vectors/s_done`, with
the cycle each one landed). The system flow compares both recordings, RTL against pysim, word for
word. `check_xsi_outputs` then applies the golden to the RTL's: per job, the captured `Y` region
equals `golden` bit-exact, and `s_done` carries one echoed descriptor per job.

This is the same shape as `mem_copy`, and its [system page](../memcpy/system.md) walks the
derivation in more detail.

## Next

[RTL simulation](./rtlsim.md): run the system flow, the twelve-job gate, and the deadlock check
only RTL can give.
