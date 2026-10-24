---
title: The equalizer's system flow
parent: Block equalizer
nav_order: 6
---

# The system flow

Each stage is verified **alone** before the composite -- [the cut](../../guide/systems/pysim.md#the-cut),
step by step. Every system is a [harness](../../guide/flows/units.md): the module as the DUT of its own
[`HwSystem`](../../guide/build/xsi_system.md#the-system), a stand-in generated at each port. The single-kernel
version of this page is [the polynomial's system flow](../stream_kernel/system_flow.md); what is new here
is several systems, one per module, on one DAG.

## Four systems {#four-systems}

| system | the DUT | its stand-ins | the scenario | prefix |
|---|---|---|---|---|
| `harness(BlockEq)` | the composite | a driver on `s_in`; sinks on `m_out`, `m_status` | `default_cmds()` | -- |
| `harness(Gain)` | `gain` | a driver on `s_in`; a sink on `m_g` | `default_cmds()` | `gain_` |
| `harness(Apply)` | `apply` | drivers on `s_in` and on `s_g`; sinks on `m_out`, `m_status` | `default_cmds()` | `apply_` |
| `harness(BlockEq, workload="early")` | the composite | the same as the composite's | `early_cmds()` | `early_` |

A module's `workload(kind)` is what its drivers play. `Apply.workload` drives `s_g` with the `g` messages
the golden says `gain` sends, [`link_expected`](golden.md#intent-stimulus-and-expected-outputs):

```python
def workload(self, kind: str = "test") -> dict:
    cmds = default_cmds() if kind == "test" else early_cmds()
    g = [StreamBurst(b, True) for b in link_expected(cmds)]
    return {"s_in": stimulus(cmds), "s_g": g}
```

So `apply` is tested before `gain` exists, and `gain` alone is checked against the same messages: the two
stages meet at a link both have been held to separately.

## One DAG

[`block_eq_build.py`](../../../examples/block_eq/block_eq_build.py) puts the four on one build DAG:

```python
def build_dag(work_dir=WORK_DIR) -> BuildDag:
    dag = BuildDag()
    dag.add(BlockEqCodegenStep(name="codegen"))
    add_system_steps(dag, harness(BlockEq, workload="test"), work_dir=work_dir, top=TOP)
    add_system_steps(dag, harness(Gain, workload="test"), work_dir=work_dir, top=GAIN_TOP, prefix="gain_")
    add_system_steps(dag, harness(Apply, workload="test"), work_dir=work_dir, top=APPLY_TOP, prefix="apply_")
    add_system_steps(dag, harness(BlockEq, workload="early"), work_dir=work_dir, top=TOP, prefix="early_")
    return dag
```

- **`codegen`**, once for all four: the stream helpers, a header per schema with framed readers and
  writers, the float32 lane routines (`ArrayUtilsStep(Float32, [W])`), the broadcast's body
  (`StreamBroadcastStep`), and three tops with their csynth scripts -- `block_eq`, `eq_gain`, `eq_apply`
  -- each from its module's ports by `composite_top_spec`.
- **[`add_system_steps`](../../guide/build/xsi_system.md#running-it)** for each system, its step names
  prefixed: [`csynth`](../../guide/build/xsi_system.md#csynth),
  [`system_rtl`](../../guide/build/xsi_system.md#system-rtl), [`scenario`](../../guide/build/xsi_system.md#scenario),
  [`pysim`](../../guide/build/xsi_system.md#pysim), [`system_xsi`](../../guide/build/xsi_system.md#system-xsi),
  [`compare`](../../guide/build/xsi_system.md#compare) (and the opt-in resource and Vivado steps).

Three tops, four systems: the `early_` system has no csynth step of its own. Its DUT is the composite,
so it runs on the `block_eq` RTL that `csynth` produced, with its own scenario. Each run has its own work
directory, named by its prefix and top (`gain_eq_gain_top`, `apply_eq_apply_top`, `block_eq_top`,
`early_block_eq_top`).

To run one system, name its last step:

```bash
python -m examples.block_eq.block_eq_build --through gain_compare   # gain alone, at RTL
python -m examples.block_eq.block_eq_build --through apply_compare  # apply alone, at RTL
python -m examples.block_eq.block_eq_build                          # the composite (default: compare)
python -m examples.block_eq.block_eq_build --through early_compare  # the composite, early TLAST
```

A build that changed nothing does no work: each step is fresh while what it reads is unchanged
([freshness](../../guide/build/xsi_system.md#system-freshness)).

## The gates {#the-gates}

All in [`test_block_eq.py`](../../../tests/examples/test_block_eq.py):

- **The golden on the worked examples**: the twelve result floats, by their bits
  ([The worked examples](golden.md#the-worked-examples)).
- **pysim against the golden**, burst for burst and bit for bit: the composite on both scenarios, `gain`
  alone (its `m_g` against `link_expected`), `apply` alone (its outputs against `expected`).
- **Errors are fatal**: in each scenario's expected outputs the last status is the error -- `BAD_NOISE` in `default_cmds()`,
  `TLAST_EARLY` in `early_cmds()` -- every status before it is `OK`, and fewer statuses than commands
  come out.
- **RTL against pysim** (`compare`, `-m xsi`), every recording word for word and TLAST for TLAST, for each
  of the four systems: `gain` alone, `apply` alone, the composite, and the composite on the early TLAST.
  The test also asserts which ports were recorded, so a run that recorded nothing cannot pass.

How pysim is checked against the golden -- the sinks' recorded bursts -- is on
[the polynomial's system flow](../stream_kernel/system_flow.md#checking-pysim-against-the-golden). The
check is the same here, once per system.

This example declares no timed phases, so its pysim runs are not calibrated; for timing a kernel in its
harness, see [the polynomial's timing](../stream_kernel/timing.md).
