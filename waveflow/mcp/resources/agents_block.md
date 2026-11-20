## Waveflow

This project designs hardware with **Waveflow** (<https://github.com/sdrangan/waveflow>), a
Python-native hardware design platform: one Python source is the single source of truth for the
simulation, the Vitis HLS kernel, its testbench, the firmware and the docs. Waveflow is recent and
specialised, so assume you do not know its API: look every name up before using it.

**Where to look things up.** The Waveflow MCP server (`waveflow_*` tools) searches and reads the
Waveflow guide and its reference examples. Without the server, the same tools run in a shell:

| MCP tool | shell |
|---|---|
| `waveflow_search` | `waveflow kb --text search "<query>"` |
| `waveflow_browse` | `waveflow kb --text browse [section]` |
| `waveflow_get_doc` | `waveflow kb --text doc <path>` (e.g. `systems/index.md`) |
| `waveflow_list_examples`, `waveflow_get_example` | `waveflow kb --text examples`, `waveflow kb --text example <name> [--file <f>]` |
| `waveflow_find_usage` | `waveflow kb --text usage <Symbol>` |
| `waveflow_list_frames`, `waveflow_get_process` | `waveflow frames --text`, `waveflow process [frame] --text` |

**If neither works** -- `waveflow` is not installed, or it says no Waveflow checkout was found (for
example in a cloud session) -- clone Waveflow beside this project and install it editable, then
use the shell commands above (the guide is then also plain files in `../waveflow/docs/guide/`):

```bash
git clone https://github.com/sdrangan/waveflow.git ../waveflow
pip install -e ../waveflow
```

**How to work.**
- To build an accelerator, choose the architecture, then follow its process
  (`waveflow_list_frames`, `waveflow_get_process`) before writing anything. The design flow starts at
  the guide's *Flow Overview*, `systems/index.md`; the default protocol is
  `systems/command_response.md`.
- Copy the patterns of the closest reference example rather than inventing your own.
- The skills in `.claude/skills/` and `.agents/skills/` (`waveflow-*`) hold the rules for data
  schemas and HLS kernel bodies: load the matching one before that step.
- Anything Waveflow generates is never hand-edited. Messages and sample bursts are serialized through
  their schema or the generated array utilities, never packed into words by hand. If Waveflow cannot
  express something, stop and say so rather than working around it.
- Python simulation needs only Python. Vitis HLS / Vivado (2025.1) are needed only for synthesis
  and RTL simulation.
