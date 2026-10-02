---
title: Python simulation
parent: A memory-mapped FIR
nav_order: 2
has_children: false
summary: "The whole system in pysim: the kernel, the three adaptor views and the host on an AXIMMCrossBarIF, wired as in the RTL. Bit-exact through a mid-stream tap switch, including one inside a packet; the late-host negative control reports the miss and matches 'switched where it arrived'. The same system with the three views behind one adaptor port behaves identically. pysim predicts 709 cycles where RTL measures 857 / 823, and the difference is the C++ host's own pacing."
---

# Simulation

## The system

`MmFirSystem` in [`mm_fir.py`](../../../examples/mm_fir/mm_fir.py) wires the whole design. The three
views are the adaptor's pysim twins — each an `HwModule` with a bus port (`s_mem`) on one side and
ordinary streams on the other — and the kernel is joined to them with plain `StreamIF`s:

```python
        self.regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=FirCfg, status_type=FirStatus,
                                    mem_dwidth=DW, clk=clk)
        self.qin = MemSlaveWStream(name="qin", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.qout = MemSlaveRStream(name="qout", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.fir = MmFir(name="fir", sim=sim, clk=clk)
        self.host = FirHost(name="host", sim=sim, x=list(self.x), plan=list(self.plan), pkt=self.pkt,
                            lag=self.lag, clk=clk)
        for name, m, s, depth in (
            ("k_cfg", self.regs.m_cfg, self.fir.s_cfg, self.regs.ncfg),
            ("k_stat", self.fir.m_status, self.regs.s_status, 8),
            ("k_in", self.qin.m_out, self.fir.s_in, QDEPTH),
            ("k_out", self.fir.m_out, self.qout.s_in, QDEPTH),
        ):
            si = StreamIF(name=name, sim=sim, clk=clk, bitwidth=DW, depth=depth)
            si.bind(ep_name="master", endpoint=m)
            si.bind(ep_name="slave", endpoint=s)
```

Two of those depths are not free choices, and the views check them when the simulation starts:

- **`k_in` and `k_out` have the queues' depth.** The stream channel a queue drives *is* its FIFO — in
  RTL there is one FIFO, inside the leaf — so its depth is the queue's.
- **`k_cfg` holds exactly one config packet** (`regs.ncfg`, 5 words). The RTL register bank has one
  snapshot register; a deeper channel in pysim would accept a second commit that RTL stalls.

Then the bus side. By default each view gets its own crossbar slave port:

```python
            slaves = [self.regs.s_mem, self.qin.s_mem, self.qout.s_mem]
            ranges = [(REGS, 0x1000), (QIN, 0x1000), (QOUT, 0x1000)]
```

and with `one_front=True` all three go behind one `MemSlaveAdaptor` port, at the same addresses:

```python
            self.adaptor = MemSlaveAdaptor(name="fir_mm", sim=sim, mem_dwidth=DW,
                                           views=[self.regs, self.qin, self.qout])
            slaves, ranges = [self.adaptor.s_mem], [(REGS, self.adaptor.span())]
```

The crossbar is an `AXIMMCrossBarIF` with `latency_init = 4` — the per-transaction cost measured
through AMD's `axi_crossbar` at RTL. `run()` ends the simulation when the host program finishes
(`run_sim(until=self.host.done)`), because a polling kernel would otherwise keep it running forever.

## Running it

```python
from examples.mm_fir.mm_fir import MmFirSystem, fir_golden
from examples.mm_fir.mm_fir_xsi import PKT, PLAN, scenario_x

x = scenario_x()                      # 200 int16 samples
sysm = MmFirSystem(x=list(x), plan=PLAN, pkt=PKT)
y = sysm.run()
```

`PLAN` is the RTL gate's scenario: taps `[3, -1, 4, 1, -5]` from sample 0, then
`[2, 7, 1, -8, 2, 8, 1, -8]` from sample **101** — deliberately not a multiple of the 16-sample
packet, so the host has to cut a packet at the switch.

## Results

| run | bit-exact vs `fir_golden` | status `nsamp / ncfg / late` | cycles |
|---|---|---|---|
| one view per crossbar port | yes | 200 / 2 / 0 | 709 |
| three views behind one adaptor (`one_front=True`) | yes | 200 / 2 / 0 | 709 |
| late host (`lag=32`) — the negative control | **no** | 200 / 2 / **1** | 709 |

The third row is the one that gives the first two their meaning. A host that commits the second
config 32 samples after its `apply_at` produces output that does **not** match the plan — and it
matches the golden for "the second taps took effect at sample 133", exactly where the config arrived.
The kernel counted the miss. Without this run, a pass in the first row could mean the protocol works
or that it was never exercised.

The two topologies take the same 709 cycles in pysim because the host keeps one transaction
outstanding at a time, so there is nothing for a single port to serialize. (At RTL they differ by 34
cycles — see [RTL simulation](rtlsim.md).)

The tests are [`tests/examples/test_mm_fir.py`](../../../tests/examples/test_mm_fir.py): one tap set,
switches at samples 16, 96 and 101, the late host, and the config bounds.

## How close is pysim's timing?

pysim predicts **709** cycles; RTL measures **857** with one view per slot and **823** behind one
front. Most of that is not the adaptor:

- The views alone track RTL to within 2 cycles per operation once the crossbar's `latency_init` is set
  to the measured 4 ([`tests/hw/test_mm_queue.py`](../../../tests/hw/test_mm_queue.py),
  [`tests/hw/test_mm_regbank.py`](../../../tests/hw/test_mm_regbank.py)).
- Per 16-sample packet RTL takes ~57 cycles and pysim ~51. The host issues four bus operations per
  packet, and the C++ host model starts each operation two cycles after the previous one ends — the
  testbench's own pacing, which the pysim host does not have.

The one place pysim is structurally coarser: an operation that waits on a full queue, or on a config
packet the kernel has not taken, is released up to one packet early in pysim, because a pysim stream
hands over a whole packet in one event where RTL drains it a word per cycle. This scenario never fills
a queue, so it does not show here.

Next: [Code generation](codegen.md).
