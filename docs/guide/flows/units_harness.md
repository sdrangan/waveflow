---
title: The harness
parent: Developing module by module
grand_parent: Modules
nav_order: 1
audience: python
snippets: run
api: [harness, memory_harness, HwSystem, StreamDriver, StreamSink, CreditSink, CreditSource, MemoryMod, MmScript, add_system_steps]
summary: "harness(module) makes any module the DUT of its own HwSystem: a fresh copy of the module (its class and build parameters) on a new Simulation, with a stand-in at each port chosen by the port's type, each with an RTL twin. A stream input gets a StreamDriver (AxisMaster at RTL), a stream output a StreamSink (AxisSlave), a credit stream a CreditSink or CreditSource, and the module's memory-master ports (MMIFReadMaster, MMIFWriteMaster) one shared MemoryMod on a zero-latency crossbar (one FlatMemory arena at RTL, served to each m_axi bundle by AxiMmReadSlave or AxiMmWriteSlave). A memory's own slave port is harnessed by memory_harness and a scripted MmScript master. The scenario is the module's: workload(kind) returns {input port: bursts} and, under one memory port, the memory's initial image. The harness runs through the system flow with add_system_steps: mem_copy's harness matches pysim word for word at RTL and finishes at 2908 cycles (Vivado 2025.1)."
---

# The harness

A **harness** is any module made the DUT of its own [`HwSystem`](../systems/rtl.md#the-system):

```python
from examples.mem_copy.mem_copy import MemCopy        # the example's kernel
from waveflow.hw.harness import harness               # the framework's

sysm = harness(MemCopy, workload="test")              # an HwSystem: the kernel, a stand-in per port
for port, stand_in in sysm.stand_ins.items():
    print(port, type(stand_in).__name__, stand_in.name)
```

```text
m_in MemoryMod memory
m_out MemoryMod memory
s_cmd StreamDriver s_cmd_driver
s_done StreamSink s_done_sink
```

[`harness`](../../../waveflow/hw/harness.py) takes a module class or an instance. The DUT is a **fresh
copy**: the module's class and its build parameters (`HwParam` fields), on a new `Simulation`. So a
module inside a larger system is harnessed without disturbing it, and the copy is a root, as a system's
DUT must be ([the `HwSystem` rules](../systems/rtl.md#the-system)). `sysm.stand_ins` maps each boundary
port to the testbench participant that faces it.

## A stand-in per port type

The testbench is generated from the module's boundary, port by port. Each stand-in is a pysim object
with an RTL twin -- its `bfm_model()` -- so the same harness runs in pysim and under XSI:

| the module's port | stand-in (pysim) | RTL twin (XSI) |
|---|---|---|
| a stream input (`StreamIFSlave`) | [`StreamDriver`](../../../waveflow/simulation/stream_tb.py), playing `vectors/<port>` | `AxisMaster` |
| a stream output (`StreamIFMaster`) | [`StreamSink`](../../../waveflow/simulation/stream_tb.py), recording `vectors/<port>` | `AxisSlave` |
| a credit stream it produces into | [`CreditSink`](../../../waveflow/simulation/credit_tb.py): takes everything, reports credit | `CreditSink` |
| a credit stream it consumes | [`CreditSource`](../../../waveflow/simulation/credit_tb.py): plays bursts when the window has room | `CreditSource` |
| its memory-master ports (`MMIFReadMaster`, `MMIFWriteMaster`) | **one** [`MemoryMod`](../../../waveflow/hw/memory.py) behind all of them, on a zero-latency crossbar | one `FlatMemory` arena, served to each `m_axi` bundle by `AxiMmReadSlave` or `AxiMmWriteSlave` |

The C++ classes are in [`waveflow/build/xsi/xsi_bfm.h`](../../../waveflow/build/xsi/xsi_bfm.h); which
one answers which kind of port is the table `BFM_DUALS` ([BFM models](../build/bfm.md)).

A credit port's window is compiled into the producer's RTL, so the module states it --
`credit_links = {port: {"depth": .., "crd_every": ..}}` -- and the harness refuses to guess.

### The memory stand-in

A kernel's memory-master ports all face **one memory**, as its `m_axi` bundles do at RTL: `MemCopy`
reads its sources on `m_in` and writes its destinations on `m_out`, in the same arena. The memory is a
`MemoryMod` at byte address 0; at RTL it is the `FlatMemory` its `bfm_model()` declares, constructed once
and handed to each bundle's slave model -- the memory [mem_copy's system](../../examples/memcpy/system.md)
puts behind the same two bundles.

- **Its image is in the workload**, under one of the memory ports: words from byte address 0, and its
  length is the arena's size. With no image the arena is 4096 words of zeros. The ports share one
  memory, so an image under two of them is refused.
- It loads the image from `vectors/mem_init` and dumps the arena it ends with to `vectors/mem`, a
  recording compared RTL against pysim ([Reading a harness's recordings](units_recordings.md)).
- Its ports must share one width, of 8 to 64 bits. A bare `MMIFMaster` (no direction) runs in pysim, but
  has no RTL lowering: the direction is the port's type, so declare a read or a write master.
- It is polite: the RTL slave models have no stall policy
  ([Hostile stand-ins](units_hostile.md)).

Other port kinds -- a device's AXI-MM slave group, an interrupt -- have no stand-in: a device is
harnessed with its host on a direct link ([the polynomial on a bus](../../examples/stream_kernel_mm/composition.md)).

### A memory's own harness

A memory's **slave** port is the other way around: the memory is the DUT and its host is a script.
`memory_harness(make, script)` puts the memory `make(sim)` builds behind a direct link, driven by
[`MmScript`](../../../waveflow/simulation/mm_tb.py), a scripted AXI-MM master (C++ twin `MmScript`)
that records its read data and when each read and write ran. Markov's memory is harnessed this way
([`markov_memory`, `mem_script`](../../../examples/markov/markov.py)).

## The scenario: `workload(kind)`

The module supplies its scenarios, so a harness needs no testbench code. `workload(kind)` returns
`{input port: bursts}` -- each burst a [`StreamBurst`](../../../waveflow/utils/burst_io.py) -- and,
under one memory port, the memory's image. `kind` names the scenario: `"test"` for verification,
`"calib"` for [calibration](units_calibration.md). `MemCopy`'s is the XSI gate's sixteen copies
([`mem_copy.py`](../../../examples/mem_copy/mem_copy.py)):

<!-- snippet: skip -->
```python
def workload(self, kind: str = "test") -> dict:
    w = int(self.mem_dwidth)
    cmds = [StreamBurst(np.asarray(CopyCmd(src_off=j.src_off, dst_off=j.dst_off,
                                           n_words=j.n_words, tx_id=k).serialize(word_bw=w),
                                   dtype=np.uint64))
            for k, j in enumerate(XSI_JOBS)]
    image, _golden = copy_arena(XSI_JOBS, w)
    return {"s_cmd": cmds, "m_in": image}
```

A one-off scenario is passed as the dict itself: `harness(MemRStream, workload={"s_cmd": [...],
"m_mem": image})` ([`test_harness_memory.py`](../../../tests/hw/test_harness_memory.py)). A workload
that names a port the module has no input of is refused.

## Through the system flow

A harness's DUT is a root, so `add_system_steps` runs it through the whole
[system flow](../build/xsi_system.md#running-it): csynth, the RTL run, and the comparison of the
stand-ins' recordings, RTL against pysim. mem_copy's DAG adds its kernel's harness beside its system
([`mem_copy_system_build.py`](../../../examples/mem_copy/mem_copy_system_build.py)):

<!-- snippet: skip -->
```python
add_system_steps(dag, harness(MemCopy, workload="test"), work_dir=work_dir, top="mem_copy_unit",
                 tcls={"mem_copy": Path("mem_copy.tcl")}, prefix="unit_")
```

```bash
python -m examples.mem_copy.mem_copy_system_build --through unit_compare
```

At RTL (Vitis/Vivado 2025.1) the harness's memory and completions match pysim word for word, and the
last completion lands at cycle **2908** -- the number [mem_copy's system](../../examples/memcpy/rtlsim.md)
gates on, since the testbench is the same: a driver, a sink and one memory behind both bundles
([`test_mem_copy_unit_xsi.py`](../../../tests/examples/test_mem_copy_unit_xsi.py)).
