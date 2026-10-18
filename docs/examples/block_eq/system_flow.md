---
title: The equalizer's system flow
parent: Block equalizer
nav_order: 3
---

# The system flow

Each stage is verified **alone** before the composite -- [Simulating in Python](../../guide/systems/pysim.md#the-cut),
step by step. Every one of them is a harness: the module as the DUT, a stand-in generated at each port.

| system | the DUT | its stand-ins | the scenario | prefix |
|---|---|---|---|---|
| `harness(Gain)` | `gain` | a driver on `s_in`; a sink on `m_g` | `default_cmds()` | `gain_` |
| `harness(Apply)` | `apply` | drivers on `s_in` and on `s_g` (the `g` messages, from the golden); sinks on `m_out`, `m_status` | `default_cmds()` | `apply_` |
| `harness(BlockEq)` | the composite | a driver on `s_in`; sinks on `m_out`, `m_status` | `default_cmds()` | -- |
| `harness(BlockEq, workload="early")` | the composite | the same | `early_cmds()` | `early_` |

`apply` alone is driven with the `g` messages the golden says `gain` sends (`link_expected`), so it is
tested before `gain` exists -- and `gain` alone is checked against the same messages.

[`block_eq_build.py`](../../../examples/block_eq/block_eq_build.py) puts the four on one DAG: the example's
`codegen` (the schemas with their framed readers and writers, the float32 lane routines, the broadcast's
body, three tops -- the composite and each stage), then `add_system_steps` for each system.

## The gates

- **pysim against the golden**, bit for bit: each stage alone, the composite, and the composite's errors
  -- `BAD_NOISE` in `default_cmds()`, `TLAST_EARLY` in `early_cmds()` -- with nothing answered after them.
- **RTL against pysim** (`compare`, `-m xsi`), every recording word for word and TLAST for TLAST: `gain`
  alone, `apply` alone, the composite, and the composite on the early TLAST.

All six from [`test_block_eq.py`](../../../tests/examples/test_block_eq.py). How pysim is checked against the
golden -- the sinks' recorded bursts -- is on
[the polynomial's system flow](../stream_kernel/system_flow.md#checking-pysim-against-the-golden).
