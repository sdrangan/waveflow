---
title: Python simulation
parent: A memory-mapped FIR
nav_order: 2
has_children: false
summary: "The whole system in pysim, in three wirings with one host class: each view on its own crossbar slot, all four behind one adaptor port, and the host joined straight to the kernel. Bit-exact through a mid-stream tap switch in all three, including one inside a packet; a config committed after its packets is waited for; the wrong-tag negative control is exposed by the responses. Cycle counts against RTL, and the three model fixes -- found by lining up both backends' bus operations -- that bring pysim within 4.4% of it."
---

# Simulation

## The system

`MmFirSystem` in [`mm_fir.py`](../../../examples/mm_fir/mm_fir.py) wires the whole design, in one of
three ways. The kernel and the host are the same in all three.

### Over the bus (`link="mm"`, the default)

The four views are the adaptor's pysim twins — each an `HwModule` with a bus port (`s_mem`) on one
side and ordinary streams on the other — and the kernel is joined to them with plain `StreamIF`s:

```python
        self.regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=FirCfg, status_type=FirStatus,
                                    mem_dwidth=DW, clk=clk)
        self.qin = MemSlaveWStream(name="qin", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.qout = MemSlaveRStream(name="qout", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.qresp = MemSlaveRStream(name="qresp", sim=sim, mem_dwidth=DW, depth=RDEPTH, clk=clk)
        self._stream("k_cfg", self.regs.m_cfg, fir.s_cfg, self.regs.ncfg)
        self._stream("k_stat", fir.m_status, self.regs.s_status, 8)
        self._stream("k_in", self.qin.m_out, fir.s_in, QDEPTH)
        self._stream("k_out", fir.m_out, self.qout.s_in, QDEPTH)
        self._stream("k_resp", fir.m_resp, self.qresp.s_in, RDEPTH)
        views = [self.regs, self.qin, self.qout, self.qresp]
```

Two of those depths are not free choices, and the views check them when the simulation starts:

- **`k_in`, `k_out` and `k_resp` have their queues' depth.** The stream channel a queue drives *is* its FIFO — in
  RTL there is one FIFO, inside the leaf — so its depth is the queue's.
- **`k_cfg` holds exactly one config packet** (`regs.ncfg`, 5 words). The RTL register bank has one
  snapshot register; a deeper channel in pysim would accept a second commit that RTL stalls.

Then the bus side. By default each view gets its own crossbar slave port:

```python
            slaves = [v.s_mem for v in views]
            ranges = [(REGS, 0x1000), (QIN, 0x1000), (QOUT, 0x1000), (QRESP, 0x1000)]
```

and with `one_front=True` all four go behind one `MemSlaveAdaptor` port, at the same addresses:

```python
            self.adaptor = MemSlaveAdaptor(name="fir_mm", sim=sim, mem_dwidth=DW, views=views)
            slaves, ranges = [self.adaptor.s_mem], [(REGS, self.adaptor.span())]
```

The crossbar is an `AXIMMCrossBarIF` with `latency_init = 4`, of which `latency_travel = 2` — the
per-transaction cost measured through AMD's `axi_crossbar` at RTL, and how much of it overlaps the
slave's current work (see [How close is pysim's timing?](#how-close-is-pysims-timing)). The host's
master keeps one read and one write in flight and paces its transactions 2 cycles apart, as the C++
host does. Last, the host gets its endpoints from the address map, by view
name:

```python
        self.slave_map = (self.adaptor.slave_map() if self.one_front
                          else MemSlaveMap.from_views(views))
        mm = BoundMemSlaveAdaptor(self.slave_map, self.host.m, poll_cycles=self.host.poll_cycles)
        self.host.cfg = mm.stream_master("regs")
        self.host.qin = mm.stream_master("qin")
        self.host.qout = mm.stream_slave("qout")
        self.host.qresp = mm.stream_slave("qresp")
        self.host.status = mm.status("regs")
```

### Direct (`link="direct"`)

No bus, no views: the host's endpoints are joined straight to the kernel's. The configuration and the
samples are plain streams, and the status is a `LatestValueIF` — a channel that keeps only the latest
complete message, as the register bank's status half does:

```python
        host.cfg = StreamIFMaster(name="host_cfg", sim=sim, bitwidth=DW, has_tlast=True)
        host.qin = StreamIFMaster(name="host_qin", sim=sim, bitwidth=DW, has_tlast=True)
        host.qout = StreamIFSlave(name="host_qout", sim=sim, bitwidth=DW, has_tlast=False)
        host.qresp = StreamIFSlave(name="host_qresp", sim=sim, bitwidth=DW, has_tlast=False)
        host.status = LatestValueIFSlave(name="host_status", sim=sim)
        self._stream("k_cfg", host.cfg, fir.s_cfg, FirCfg.nwords_per_inst(DW))
        self._stream("k_in", host.qin, fir.s_in, QDEPTH)
        self._stream("k_out", fir.m_out, host.qout, QDEPTH)
        self._stream("k_resp", fir.m_resp, host.qresp, RDEPTH)
        self.status_if = LatestValueIF(name="k_stat", sim=sim, schema_type=FirStatus, bitwidth=DW,
                                       clk=self.clk)
```

Direct is what the system would be with no bus at all, so it is the reference the memory-mapped runs
are checked against: same outputs, same status.

`run()` ends the simulation when the host program finishes (`run_sim(until=self.host.done)`), because
a polling kernel would otherwise keep it running forever.

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

| run | bit-exact vs `fir_golden` | status `nsamp / ncfg` | response mismatches | cycles |
|---|---|---|---|---|
| one view per crossbar port | yes | 200 / 2 | 0 | 734 |
| four views behind one adaptor (`one_front=True`) | yes | 200 / 2 | 0 | 792 |
| direct (`link="direct"`) | yes | 200 / 2 | 0 | 348 |
| config committed 32 samples late (`lag=32`) | yes | 200 / 2 | 0 | 786 |
| wrong tag (`stale_tag=True`) — the negative control | **no** | 200 / **1** | **7** | 734 |

**The late commit is waited for.** The host sends the packets that need config 2 and only then
commits it. Those packets wait in queue in — their header names config 2, and the kernel will not
filter them without it — so the output is still exact.

**The last row gives the others their meaning.** The host tags every packet with config 1 while
meaning config 2 after the switch. The kernel obeys the tag: it never takes config 2 (`ncfg = 1`),
the output equals the golden for taps A throughout, and the seven responses after the switch each
report `cfg_seq = 1` where the host expected 2. Without this run, an empty mismatch list could mean
the echo works or that it checks nothing.

**One front is slower than one port per view**, because the host's writer and reader overlap behind
separate fronts and take turns behind one. That serialization is the
[ordering guarantee](../../guide/interface/axi_mm/slave.md#ordering), and this is its price.

The tests are [`tests/examples/test_mm_fir.py`](../../../tests/examples/test_mm_fir.py): one tap set,
switches at samples 16, 96 and 101, the late commit and the wrong tag, each in all three wirings; a
check that the three wirings give the same outputs from the same host; the schedule's cuts and tags;
and the message sizes.

## How close is pysim's timing?

RTL measures **768** cycles with one view per slot and **783** behind one front
([RTL simulation](rtlsim.md#results)). pysim says **734** (−4.4%) and **792** (+1.1%), and ranks the two
shapes the same way.

It did not start there: pysim said 536 and 874, the second shape *slower* where RTL had them nearly
equal. The gap was found by logging every bus operation the pysim host issues — kind, address,
length, start, end — and lining it up, per process, with the operations the XSI testbench prints.
Three model gaps came out, each now a setting in
[`mm_fir.py`](../../../examples/mm_fir/mm_fir.py):

| found | the fix | setting |
|---|---|---|
| Behind one front, every switch between a read and a write cost pysim 2 cycles more than RTL; read-after-read matched exactly. The crossbar's 4-cycle latency is 2 cycles of travel plus 2 at the front, and the travel overlaps what the front is serving. | charge the travel before taking the front | `XBAR_TRAVEL = 2` → `AXIMMCrossBarIF.latency_travel` |
| pysim let the writer's and the reader's polls — both reads — travel together; the C++ master has one read and one write in flight. | limit the master | `HOST_MAX_OUTSTANDING = 1` → `MMIFMaster.max_outstanding` |
| The C++ host presents each transaction 2 cycles after its process's previous one finished; the pysim host, at once. | the host's pacing | `HOST_ISSUE_CYCLES = 2` → `MMIFMaster.issue_cycles` |

The first describes the crossbar; the other two describe the host — the C++ testbench host, here —
so the XSI testbench derives its master from the same `HOST_MAX_OUTSTANDING`. The
[AXI crossbar](../../guide/interface/axi_mm/crossbar.md#matching-pysim-to-it) page has them for any design.

What is left is small and understood: when a read and a write reach the front together, RTL's front
alternates and pysim serves whichever process asked first; and pysim's kernel hands a whole packet's
results over at once, where RTL produces one per cycle, so the RTL reader sometimes finds 2 ready and
pops 2, then 14.

The views alone track RTL to within 2 cycles per operation once the crossbar's `latency_init` is set
to the measured 4 ([`tests/hw/test_mm_queue.py`](../../../tests/hw/test_mm_queue.py),
[`tests/hw/test_mm_regbank.py`](../../../tests/hw/test_mm_regbank.py)).

The one place pysim is structurally coarser: an operation that waits on a full queue, or on a config
packet the kernel has not taken, is released up to one packet early in pysim, because a pysim stream
hands over a whole packet in one event where RTL drains it a word per cycle.

Next: [Code generation](codegen.md).
