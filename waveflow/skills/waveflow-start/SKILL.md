---
name: waveflow-start
description: Start any task that uses Waveflow, the Python-native hardware design platform (HwModule, DataSchema, pysim, Vitis HLS kernels, accelerators, the `waveflow` command) -- how to look up its API and guide (MCP tools or the same in a shell), what to do when Waveflow is not installed or the guide is not found (clone and install it), and how to begin a design. Use first whenever the user mentions Waveflow, asks to build or simulate hardware with it, or a project's AGENTS.md says it uses Waveflow.
---

# Starting with Waveflow

Waveflow is a Python-native hardware design platform: one Python source is the single source of truth
for the simulation, the Vitis HLS kernel, its testbench, the firmware and the docs. It is recent and
specialised, so **assume you do not know its API**: look every name up before using it, and read the
page before the step.

## 1. Make sure you can look things up

The Waveflow MCP server (`waveflow_*` tools) searches and reads the Waveflow guide and its reference
examples. Without the server, the same lookups run in a shell and return the same content:

| MCP tool | shell |
|---|---|
| `waveflow_search` | `waveflow kb --text search "<query>"` |
| `waveflow_browse` | `waveflow kb --text browse [section]` |
| `waveflow_get_doc` | `waveflow kb --text doc <path>` (e.g. `systems/index.md`) |
| `waveflow_list_examples`, `waveflow_get_example` | `waveflow kb --text examples`, `waveflow kb --text example <name> [--file <f>]` |
| `waveflow_find_usage` | `waveflow kb --text usage <Symbol>` |
| `waveflow_list_frames`, `waveflow_get_process` | `waveflow frames --text`, `waveflow process [frame] --text` |

**If neither works** -- `waveflow` is not installed, or it says no Waveflow checkout was found (common
in a cloud session) -- clone Waveflow beside the project and install it editable (Python 3.10+):

```bash
git clone https://github.com/sdrangan/waveflow.git ../waveflow
pip install -e ../waveflow
waveflow kb --text browse        # lists the guide and the examples: the lookups work now
```

The guide is then also plain files in `../waveflow/docs/guide/`. Do not write Waveflow code from
memory while the lookups are broken: fix them first. Setup details: `ai_tooling/mcp_setup.md`.

## 2. Begin a design

- **From an algorithm with no architecture yet** (an equation in a paper, "make this hardware"), plan
  it first: the `waveflow-architecture` skill (or `systems/library.md`, the blocks that exist and what
  each costs). Its plan names the blocks; each block then takes the steps below.
- To build an accelerator, **choose the architecture, then follow its process** before writing
  anything: `waveflow_list_frames` (the menu), then `waveflow_get_process(frame)`. If no frame fits,
  pick the closest reference example and follow the generic process (`waveflow_get_process()`).
- Read what is being built before how: the guide's four concept pages, in order --
  `systems/index.md` (*Waveflow Architecture*: the three levels of simulation, the layer around the
  DUT), `systems/kernels.md` (*Kernels*: a `FreeRunMod` on streams only, leaf or composite),
  `systems/devices.md` (*Devices and the DUT*: adaptors, devices, the HLS top and sub-top) and
  `systems/command_response.md` (the default protocol). Then the steps: `systems/flow.md` (*Flow
  Overview*). Every term is in `systems/glossary.md`.
- Copy the patterns of the closest reference example rather than inventing your own;
  `waveflow_find_usage` shows a name used in real code.

## 3. Which skill covers which piece

The other `waveflow-*` skills hold the rules for one piece each: load the matching one before that
step. If a skill is not installed, read its page instead.

| the piece | skill | or read |
|---|---|---|
| the plan, before any code: requirements, alternatives, library blocks, what fits | `waveflow-architecture` | `systems/library.md` |
| a message: a `DataSchema`, fixed point, serializing it in Python and HLS | `waveflow-schema` | `schema/index.md` |
| a kernel in Python: its streams, the protocol, `run_iter`, its harness test | `waveflow-kernel` | `systems/kernels.md`, `systems/hwmodule.md` |
| a leaf kernel's HLS body: loops, the lane loop, pragmas, state and reset | `waveflow-kernel-hls` | `vectorization/hls/loop_optimization.md` |
| a device: memory readers and writers, a host's queues and registers, credit, BRAM, the HLS top | `waveflow-device` | `systems/devices.md` |
| a composite kernel: stages, internal streams, broadcast, a token per job | `waveflow-pipeline` | `systems/streams.md` |
| the system: `HwSystem`, the build DAG, RTL simulation, tests that count | `waveflow-system` | `systems/rtl.md`, `systems/verification.md` |

## 4. Rules that hold everywhere

- Anything Waveflow generates is never hand-edited: change the Python source and regenerate.
- Messages and sample bursts are serialized through their schema or the generated array utilities,
  never packed into words by hand.
- If Waveflow cannot express something, stop and say so rather than working around it.
- Python simulation needs only Python. Vitis HLS / Vivado (2025.1) are needed only for synthesis and
  RTL simulation; say so rather than skipping a step silently when they are missing.
