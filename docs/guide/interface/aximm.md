---
title: MM Interfaces
parent: Interfaces
nav_order: 3
audience: python
api: [MMIFMaster, MMIFSlave, AXIMMCrossBarIF, DirectMMIF, AXIMMProtocol, assign_address_ranges]
summary: "Memory-mapped interfaces in the SimPy model — MMIFMaster/MMIFSlave endpoints, the AXIMMCrossBarIF (FULL/LITE, address routing) and DirectMMIF, and read/write/read_schema/read_array."
---

# Memory-Mapped (MM) Interfaces

Waveflow provides two memory-mapped interface types that share a common pair of generic endpoints:

| Class | Role |
|---|---|
| `MMIFMaster` | Master (initiator) endpoint — issues `read` / `write` |
| `MMIFSlave` | Slave (target) endpoint — handles `rx_read_proc` / `rx_write_proc` |
| `AXIMMCrossBarIF` | Multi-master × multi-slave AXI-style crossbar |
| `DirectMMIF` | Point-to-point connection (BRAM / local scratchpad) |

The endpoints are **independent of the interconnect**.  A component declares an `MMIFMaster` or `MMIFSlave` without knowing whether it will be wired to a crossbar or a direct link — that decision is made at the top level.

---

## Endpoints

### MMIFSlave

```python
from waveflow.hw.aximm import MMIFSlave

slave_ep = MMIFSlave(
    sim=sim,
    bitwidth=32,
    rx_write_proc=self.on_write,   # called on each write transaction
    rx_read_proc=self.on_read,     # called on each read transaction
    latency_per_word=3.0,          # cycles per word (used by AXIMMCrossBarIF LITE)
)
```

**`rx_write_proc(words, local_addr) -> ProcessGen[None]`**  
Called with the transferred word array and the local address of the first word.  For LITE crossbar slaves this is called once per word with a one-element array.

**`rx_read_proc(nwords, local_addr) -> ProcessGen[Words]`**  
Called to retrieve data; the generator's return value must be a numpy array of shape `(nwords,)`.

```python
def on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
    yield self.env.timeout(0)   # model peripheral access latency here
    return np.array(
        [self._mem.get(local_addr + i, 0) for i in range(nwords)],
        dtype=np.uint32,
    )
```

### MMIFMaster

```python
from waveflow.hw.aximm import MMIFMaster

master_ep = MMIFMaster(sim=sim, bitwidth=32)
```

**Raw word transfers:**

```python
yield self.process(master_ep.write(words, global_addr))

proc = self.env.process(master_ep.read(nwords, global_addr))
yield proc
data = proc.value   # numpy array of shape (nwords,)
```

Or via `yield from` inside a `run_proc`:

```python
data = yield from master_ep.read(nwords, global_addr)
```

**Schema convenience methods** (no boilerplate serialization needed):

```python
# Write / read one schema instance
yield from master_ep.write_schema(cmd_hdr, addr=CMD_ADDR)
cmd = yield from master_ep.read_schema(CmdHdr, addr=CMD_ADDR)

# Write / read a typed array
yield from master_ep.write_array(samples, Float32, addr=DATA_ADDR)
arr = yield from master_ep.read_array(Float32, count=nsamp, addr=DATA_ADDR)
# arr is np.ndarray[float32] for FloatField/IntField element types
```

---

## AXIMMCrossBarIF

Multi-master × multi-slave AXI-style crossbar with address-based routing.

### Address-based routing

Each slave is assigned a byte-address range `[base_addr, base_addr + size)` via `assign_address_ranges()`.  When a master calls `write(words, global_addr)`, the crossbar decodes the address, computes `local_addr = global_addr - slave.base_addr`, and calls `rx_write_proc(words, local_addr)`.  A `RuntimeError` is raised for unmapped addresses.

### Protocol: FULL vs LITE

The protocol is set **per slave at bind time**, not on the endpoint constructor:

```python
xbar.bind("slave_0", mem_ep)                               # FULL (default)
xbar.bind("slave_1", reg_ep, protocol=AXIMMProtocol.LITE)
```

| Value | Transfer model | Typical use |
|---|---|---|
| `AXIMMProtocol.FULL` | One burst call for all `nwords` | DDR, block RAM, DMA buffers |
| `AXIMMProtocol.LITE` | One call per word, auto-incremented addresses | Configuration registers |

For LITE slaves, a multi-word write is split into `nwords` single-word transactions automatically.  The master does not need to know the slave's protocol.

### Latency model

All cycle counts are divided by `clk.freq` to produce seconds.

| Path | Formula |
|---|---|
| FULL write | `(latency_init + nwords) / clk.freq` |
| FULL read | `latency_init/f + slave_access + (latency_read_return + nwords)/f` |
| LITE write | `nwords × latency_per_word / clk.freq` |
| LITE read | `nwords × latency_per_word / clk.freq` |

### Construction

```python
from waveflow.hw.aximm import AXIMMCrossBarIF

xbar = AXIMMCrossBarIF(
    sim=sim,
    clk=clk,
    nports_master=2,
    nports_slave=2,
    bitwidth=32,
    latency_init=2.0,           # wire cycles, forward direction
    latency_read_return=2.0,    # wire cycles, return direction (FULL reads)
    byte_addressable=True,      # True = AXI byte addresses (default)
)
```

Endpoint names: `master_0` … `master_{n-1}` and `slave_0` … `slave_{m-1}`.

---

## DirectMMIF

Point-to-point interconnect: one master, one slave, no address translation.  The master's address is passed directly to the slave callback as `local_addr`.  This models a component wired directly to a BRAM or local register file.

```python
from waveflow.hw.aximm import DirectMMIF

direct = DirectMMIF(
    sim=sim,
    clk=clk,
    latency_write=0.0,          # cycles before rx_write_proc is called
    latency_read=0.0,           # cycles on the read request leg
    latency_read_return=0.0,    # cycles after rx_read_proc returns
    byte_addressable=False,     # False = word addresses (BRAM convention)
)
direct.bind("master", master_ep)
direct.bind("slave",  slave_ep)
```

Endpoint names: `master` and `slave`.

---

## Full example

```python
from __future__ import annotations
from dataclasses import dataclass
import numpy as np

from waveflow.hw.aximm import (
    AXIMMCrossBarIF, AXIMMProtocol,
    MMIFMaster, MMIFSlave,
    AXIMMAddressRange, assign_address_ranges,
)
from waveflow.hw.clock import Clock
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


@dataclass
class MemBank(SimObj):
    """Simple word-addressed SRAM (FULL, burst)."""
    def __post_init__(self) -> None:
        super().__post_init__()
        self._mem: dict[int, int] = {}
        self.slave_ep = MMIFSlave(
            sim=self.sim, bitwidth=32,
            rx_write_proc=self.on_write, rx_read_proc=self.on_read,
        )

    def on_write(self, words, local_addr: int) -> ProcessGen[None]:
        for i, w in enumerate(words):
            self._mem[local_addr + i * 4] = int(w)
        yield self.env.timeout(0)

    def on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.env.timeout(4 / self.sim._clk_ref.freq)
        return np.array(
            [self._mem.get(local_addr + i * 4, 0) for i in range(nwords)],
            dtype=np.uint32,
        )


@dataclass
class RegFile(SimObj):
    """Configuration registers (LITE, one register per word)."""
    def __post_init__(self) -> None:
        super().__post_init__()
        self._regs: dict[int, int] = {}
        self.slave_ep = MMIFSlave(
            sim=self.sim, bitwidth=32,
            rx_write_proc=self.on_write, rx_read_proc=self.on_read,
            latency_per_word=3.0,
        )

    def on_write(self, words, local_addr: int) -> ProcessGen[None]:
        self._regs[local_addr] = int(words[0])
        yield self.env.timeout(0)

    def on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.env.timeout(0)
        return np.array([self._regs.get(local_addr, 0)], dtype=np.uint32)


@dataclass
class CPU(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.master_ep = MMIFMaster(sim=self.sim, bitwidth=32)

    def run_proc(self) -> ProcessGen[None]:
        env = self.env

        # Write 4 words to MemBank, then read back
        words = np.array([0xA0, 0xA1, 0xA2, 0xA3], dtype=np.uint32)
        yield self.process(self.master_ep.write(words, 0x0000))

        proc = env.process(self.master_ep.read(4, 0x0000))
        yield proc
        assert np.array_equal(proc.value, words)

        # Write 2 config words to RegFile (auto-split into 2 LITE transactions)
        cfg = np.array([0xCAFE, 0xBEEF], dtype=np.uint32)
        yield self.process(self.master_ep.write(cfg, 0x1000))

        proc = env.process(self.master_ep.read(2, 0x1000))
        yield proc
        assert np.array_equal(proc.value, cfg)


sim = Simulation()
clk = Clock(freq=100.0)

mem  = MemBank(sim=sim)
regs = RegFile(sim=sim)
cpu  = CPU(sim=sim)

xbar = AXIMMCrossBarIF(
    sim=sim, clk=clk,
    nports_master=1, nports_slave=2, bitwidth=32,
    latency_init=2.0, latency_read_return=2.0,
)
xbar.bind("master_0", cpu.master_ep)
xbar.bind("slave_0",  mem.slave_ep)                           # FULL (default)
xbar.bind("slave_1",  regs.slave_ep, protocol=AXIMMProtocol.LITE)

assign_address_ranges(
    [mem.slave_ep, regs.slave_ep],
    [(0x0000, 0x1000), (0x1000, 0x0010)],
)

sim.run_sim()
```

---

## Quick reference

```python
from waveflow.hw.aximm import (
    MMIFMaster, MMIFSlave,
    AXIMMCrossBarIF, DirectMMIF,
    AXIMMProtocol, AXIMMAddressRange,
    assign_address_ranges,
)
```

| Operation | Code |
|---|---|
| Create master ep | `MMIFMaster(sim=sim, bitwidth=32)` |
| Create slave ep | `MMIFSlave(sim=sim, bitwidth=32, rx_write_proc=..., rx_read_proc=...)` |
| Create crossbar | `AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=M, nports_slave=N, ...)` |
| Create direct link | `DirectMMIF(sim=sim, clk=clk)` |
| Bind master (crossbar) | `xbar.bind("master_0", master_ep)` |
| Bind FULL slave | `xbar.bind("slave_0", slave_ep)` |
| Bind LITE slave | `xbar.bind("slave_1", slave_ep, protocol=AXIMMProtocol.LITE)` |
| Bind direct | `direct.bind("master", master_ep); direct.bind("slave", slave_ep)` |
| Assign address ranges | `assign_address_ranges([s0, s1], [(0x0000, 0x1000), (0x1000, 0x10)])` |
| Write | `yield self.process(master_ep.write(words, global_addr))` |
| Read | `data = yield from master_ep.read(nwords, global_addr)` |
| Read schema | `obj = yield from master_ep.read_schema(SchemaType, addr)` |
| Write schema | `yield from master_ep.write_schema(obj, addr)` |
| Read array | `arr = yield from master_ep.read_array(Float32, count=n, addr=DATA_ADDR)` |
| Write array | `yield from master_ep.write_array(np_array, Float32, addr=DATA_ADDR)` |
