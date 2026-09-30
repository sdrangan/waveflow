---
title: Headless Testing
parent: Developers
nav_order: 1
has_children: false
---

# Headless Testing Mode

As described earlier, Waveflow is being equipped with an [MCP server](../ai_tooling/mcp_setup.md) to enable agentic assistance in building Waveflow code.  Most users will use this MCP in a **workspace** such as Claude Code or VS Code.  In these environments, the Waveflow MCP tools will be combined with the workspace tools along with the user context.  For example, given a prompt to build a new Waveflow hardware object, VS Code can inspect existing objects and use other context in addition to the Waveflow MCP tools for fetching examples.

In testing the MCP agentic system, it is often useful to evaluate the agent in a **headless mode** to simulate a new user with a specified context.  For this purpose, developers have access to the `headless.run_session` command.  In this command, the user runs:

```python
import waveflow.mcp.headless as headless

result = headless.run_session(
    task=task,
    output_path=output_path,
    model=model,
    mode=model,
    work_dir=work_dir,
    verbose=True,
 )

```

where

- `task`:  The prompt for the desired Waveflow task
- `output_path`:  A path for the output including the response and various meta data
- `model`:  The model, e.g, `gpt-5.4`
- `mode`:  Either `workspace` or `headless`
- `work_dir`:  A directory for the prompt to operate on.  
- `verbose`:  If set, the function will print outputs as the work progresses.


In `headless` mode, the MCP is created with tools to read, modify, and write files in this directory.  So, the `work_dir` directory can be used to simulate a context of existing files.  In particular, that directory can be left empty to simulate an absolutely new user providing an initial writing task.   Note that the normal `workspace` mode MCP does not have file read / write tools since these tools are generally already available in the workspace.  For example, VS Code can already inspect, read and write files.


## Example

As an example, the following python code will:

- Create an empty directory `test_workspace`
- Give the LLM a prompt to create a schema for a command for a convolutional kernel and write that schema in a file, `conv1d.py`
- The LLM is given a *headless* MCP meaning it has access to the search and read tools for the guide and the examples, plus file read and write tools

When executed, LLM will use MCP tools, including the search and read tools for the guide and the examples, to build the desired schema.

```python
import waveflow.mcp.headless as headless

from pathlib import Path
import os
import json

# Create a test workspace directory to write the file
work_dir = Path.cwd() / "test_workspace"
work_dir.mkdir(exist_ok=True)


task = "Create a schema for a 1D convolution command with the coefficients in the command and the input and output buffers as memory addresses.  Write the file in conv1d.py in the current directory."
model='gpt-5.4'
mode = "headless"

print('Running headless session with the following prompt:\n')
print(task)

output_path = os.path.abspath("headless_output.txt")
result = headless.run_session(
    task=task,
    output_path=output_path,
    model=model,
    mode=mode,
    work_dir=work_dir,
    verbose=True,
 )
print(result["final_response"])

print(f"\nSession report written to: {result['output_path']}")
print(json.dumps(result["token_totals"], indent=2))
```