# MCP blind-test specs

Specifications for `waveflow blind-test`, which runs a fresh AI agent on a spec
with nothing but the Waveflow MCP tools. See
[Blind Testing the MCP Server](../../docs/guide/ai_tooling/blind.md).

| Spec | What it checks |
| --- | --- |
| [tiny_test.md](tiny_test.md) | The harness itself, in well under a minute and about 150k tokens: the server connects, a linked file ([notes.md](notes.md)) is copied along, the agent stops for review, and the approval resumes the same session. |
| [rotate.md](rotate.md) | A full accelerator, written the way a student would: a fixed-point 2D rotation over `stream_inband`'s protocol, through csim, csynth and cosim at two word widths, with an error report and timing figures. Deliberately leaves some choices open (rounding, overflow, rotation direction, where status goes), and asks the agent to list the assumptions it made. The spec has no review stop of its own, but Waveflow's process tells the agent to freeze the spec and stop for approval, so run it with the default single approval. |
| [scale_sum_func.md](scale_sum_func.md) | Two kernels and a host on one AXI-MM bus, the simplest case: one bus width, a bit-exact model with no timing, one job at a time -- but the scale kernel writes the sum kernel's queue over the bus without overrunning it or holding the bus. Names no tooling, so the same spec drives both arms (`--no-waveflow` for the baseline); it asks how far an agent gets building a bus system with and without Waveflow. |

```bash
waveflow blind-test --prompt examples/mcp_test/tiny_test.md
```

The agent does **not** work in this folder. It works in a folder beside the
clone, by default `../waveflow_blind_tests/<spec name>/`, with the report next
to it in `<spec name>.blindtest/`. A folder inside the clone would load the
repository's `CLAUDE.md` into the agent, and the test would no longer be blind.

This directory is not a reference example. It is not in the docs'
Examples list, so the MCP tools never offer it to an agent.
