---
title: Reading a harness's recordings
parent: Developing module by module
grand_parent: Modules
nav_order: 2
audience: python
snippets: run
api: [harness, HwSystem, StreamSink, StreamDriver, MemoryMod, read_burst_bundle, add_system_steps]
summary: "What a harness records and where. Each stream sink records vectors/<port>, the memory stand-in its final arena as vectors/mem: HwSystem.recordings(), compared word for word RTL against pysim. At RTL a sink's recording also has cycles.bin, the cycle each word arrived, and each stream input's driver records vectors/<port>_acc, every word the DUT accepted with the cycle it did (HwSystem.acceptances(), RTL only). In pysim the recordings are bundles under the scenario root and the sinks' words in memory (sysm.stand_ins[port].words). Through the system flow: <work>/pysim_run and <work>/xsi_run hold each run's vectors, <work>/pysim_traces and <work>/traces the gathered recordings, <work>/compare.json the verdict. mem_copy's harness at RTL (Vivado 2025.1): 32 command words accepted, the last at cycle 2361; the last completion at 2908."
---

# Reading a harness's recordings

Every stand-in reads and writes **burst bundles** (a folder of `words.bin`, `bounds.bin`, `meta.json`;
[`burst_io`](../../../waveflow/utils/burst_io.py)), relative to the scenario's root, so a pysim run and
an RTL run started from the same root read the same bytes and record in the same places.

## What is recorded

| recording | recorded by | contents | compared RTL against pysim |
|---|---|---|---|
| `vectors/<port>` of a stream output | its `StreamSink` | every word the module wrote; at RTL also `cycles.bin`, the cycle each one arrived | yes, on its words |
| `vectors/mem` | the memory stand-in | the arena as the run left it | yes |
| `vectors/<port>_acc` of a stream input | its `StreamDriver` (`accept_bundle`) | at RTL only: every word the module accepted, and `cycles.bin`, the cycle it did | no |

`sysm.recordings()` lists what is compared, `sysm.acceptances()` the acceptance bundles
([`HwSystem`](../../../waveflow/hw/hw_system.py)). Acceptance is the input side's timing -- when the
module took each word, the twin of when its outputs arrived -- so a harness run is timed at every port
from the stand-ins alone, with no waveform. pysim does not write it: its times are the simulation's
own, and pysim's timing is checked against the RTL by [calibration](units_calibration.md), not here.

## In pysim

`write_scenario(root)` writes the inputs -- each driver's bundle, the memory's image -- and the run's
bound (`vectors/run.json`), and anchors the stand-ins at `root`; the run records beside them:

```python
import tempfile
from pathlib import Path

from examples.mem_copy.mem_copy import MemCopy
from waveflow.hw.harness import harness
from waveflow.utils.burst_io import read_burst_bundle

sysm = harness(MemCopy, workload="test")
print(sysm.recordings(), sysm.acceptances())
with tempfile.TemporaryDirectory() as root:
    sysm.write_scenario(root)
    sysm.run()
    done = read_burst_bundle(Path(root) / "vectors" / "s_done")
    mem = read_burst_bundle(Path(root) / "vectors" / "mem")[0]
print(len(done), "completions;", mem.size, "words of memory")
print(len(sysm.stand_ins["s_done"].words), "bursts in the sink")
```

```text
('vectors/mem', 'vectors/s_done') ('vectors/s_cmd_acc',)
16 completions; 6160 words of memory
16 bursts in the sink
```

`sysm.run()` alone runs from a temporary root; a sink's words are then still in memory,
`sysm.stand_ins[port].words`, one array per burst.

## Through the system flow

`add_system_steps(dag, harness(...), work_dir=..., top=...)` runs each side from its own copy of the
scenario and gathers the recordings ([the steps](../build/xsi_system.md#running-it)). Under
`<work_dir>/<prefix><top>/`:

| path | what |
|---|---|
| `scenario/` | the scenario, written once |
| `pysim_run/vectors/` | pysim's copy of it, and everything pysim recorded |
| `xsi_run/vectors/` | the RTL run's copy, and everything the RTL testbench recorded -- the acceptance bundles too, with `cycles.bin` beside each sink's and driver's words |
| `pysim_traces/`, `traces/` | the compared recordings, gathered from each run, one directory per recording (`vectors/s_done` -> `s_done`) |
| `compare.json` | the mismatches, `[]` on a pass |

A cycle is read straight from the binary, little-endian `uint64` per word:

<!-- snippet: skip -->
```python
import numpy as np

vdir = work / "xsi_run" / "vectors"
accepted = np.fromfile(vdir / "s_cmd_acc" / "cycles.bin", dtype="<u8")   # when each command word was taken
arrived = np.fromfile(vdir / "s_done" / "cycles.bin", dtype="<u8")       # when each completion landed
```

mem_copy's harness at RTL (Vitis/Vivado 2025.1): its 16 commands are 32 words, accepted from cycle 2,
the last at cycle 2361 -- the driver offers a word every cycle, so the spread is the kernel's
back-pressure -- and the last completion
lands at 2908 ([`test_mem_copy_unit_xsi.py`](../../../tests/examples/test_mem_copy_unit_xsi.py)).
