---
title: RTL simulation
parent: A memory-mapped FIR
nav_order: 4
has_children: false
summary: "The whole system as RTL under XSI: AMD's axi_crossbar, the hand-written adaptor leaves, and the csynth'd mm_fir kernel in one generated Verilog top, driven by the pysim host program written against C++ endpoints, with an address map generated from the pysim system. Two adaptor shapes — one view per crossbar slot, and all four behind one front with a generated decoder — both bit-exact against the numpy golden through a mid-stream tap switch, every response checked, no polling (the host sleeps on the queue views' interrupts), at 618 and 611 cycles."
---

# RTL simulation

Everything here is in [`examples/mm_fir/mm_fir_xsi.py`](../../../examples/mm_fir/mm_fir_xsi.py), and
the gate that runs it is [`tests/examples/test_mm_fir_xsi.py`](../../../tests/examples/test_mm_fir_xsi.py):

```
python -m examples.mm_fir.mm_fir_build                  # the kernel's csynth, once
pytest tests/examples/test_mm_fir_xsi.py -m xsi          # both topologies, bit-exact + cycles
```

or from Python, `run_xsi("one_front", work_dir)` returns the run's output.

## What is simulated

This page is where all the [components](../../guide/flows/concurrent_layers.md) of the XSI simulation are present
at once:

| component | here | produced by |
|---|---|---|
| Vitis kernel | `mm_fir` — csynth's Verilog for the [generated top-level function](codegen.md) | `mm_fir_build.py` |
| vendor IP | `axi_crossbar`, 1 SI, 4 or 2 MI | `generate_axi_xbar(xbar_config(topology), ...)` |
| hand-written RTL | `axi_slave_front`, `mm_regbank`, `mm_queue_in`, `mm_queue_out` | `waveflow/build/rtl/` |
| RTL top | `mm_fir_top`: the three above, wired | `render_top(top, topology)` |
| testbench (the harness) | an `AxiMmMaster`, and the host's `Writer` and `Reader` on the C++ endpoints | `render_tb(dll, x)`, `address_headers()` |

The RTL top is assembled by this example's `render_top` from framework pieces — it is not yet emitted
by `wrapper_gen` from the module graph, the way a design's memories are.

## Two topologies, one address map

```python
def xbar_config(topology: str) -> AxiXbarConfig:
    sysm = MmFirSystem(x=[0], plan=PLAN, one_front=topology == "one_front")
    return AxiXbarConfig.from_crossbar(sysm.xbar, XBAR_NAMES[topology])
```

The crossbar is not written here: `xbar_config` builds it from the pysim system's own crossbar, so its
slots are the ranges `assign_address_ranges` set — `MM_BASE` plus the FIR type's layout. The result is
the same IP the hand-written configs used to produce, down to its cache digest.

- **`per_view`** — each view is its own crossbar slave, with its own front (`render_view_slot`). Four
  4 KB windows at `0x0000`, `0x1000`, `0x2000`, `0x3000`.
- **`one_front`** — all four views behind **one** front and a generated decoder
  (`render_adaptor_slot`), in one 16 KB window at `0x0000`. The views land at the same addresses, so
  the host program is identical. The crossbar's second slot is a stub nothing addresses: a 1×1
  `axi_crossbar` is degenerate — `create_ip` silently generates an inconsistent two-slave IP for it
  whose simulation crashes — so `AxiXbarConfig` refuses one.

The views are the same `QueueView` / `RegBankView` declarations either way:

```python
NCFG = FirCfg.nwords_per_inst(DW)
NSTAT = FirStatus.nwords_per_inst(DW)
VIEWS = [RegBankView("regs", ncfg=NCFG, nstat=NSTAT, cfg_axis="k_cfg", status_axis="k_stat"),
         QueueView("qin", "in", axis="k_in", depth=QDEPTH),
         QueueView("qout", "out", axis="k_out", depth=QDEPTH),
         QueueView("qresp", "out", axis="k_resp", depth=RDEPTH)]
```

`NCFG` and `NSTAT` are read off the schemas, so the register bank's shadow and status sizes cannot
drift from the messages the kernel exchanges.

### The generated decoder

In the `one_front` top, this is all the decoding there is — a table from address bits to views:

```verilog
  axi_slave_front #(.DW(64), .AW(32), .IDW(1), .LAW(14)) u_fir_mm_front ( ... );
  wire [1:0] fir_mm_sel = fir_mm_req_addr[13:12];
  wire fir_mm_hole = 1'b0;                       // four views fill all four windows
  ...
  assign regs_req_valid = fir_mm_req_valid && (fir_mm_sel == 0);
  assign regs_req_addr  = fir_mm_req_addr[11:0];
  mm_regbank #(.DW(64), .LAW(12), .NCFG(5), .NSTAT(1)) u_regs ( ... );
  assign qin_req_valid = fir_mm_req_valid && (fir_mm_sel == 1);
  mm_queue_in #(.DW(64), .LAW(12), .DEPTH(64)) u_qin ( ... );
  ...
  assign qresp_req_valid = fir_mm_req_valid && (fir_mm_sel == 3);
  ...
  assign fir_mm_rsp_rdata = fir_mm_hole_rsp ? {64{1'b0}} :
                            ((fir_mm_rsel == 0) ? regs_rsp_rdata : (fir_mm_rsel == 1) ? qin_rsp_rdata : ...);
```

A request goes to the view its address selects; a read response comes from the view selected when the
read was accepted (`fir_mm_rsel`, latched — the front has one read outstanding). With four views every
window is used; with three, the decoder would answer the unused fourth with SLVERR so a stray read
cannot hang the bus.

### Joining the kernel

The kernel's five streams meet the views on `k_cfg`, `k_stat`, `k_in`, `k_out`, `k_resp`. The kernel
has no TLAST pins, so the TLASTs the leaves *drive* go nowhere, and the three they *read* are tied low —
the status bank completes a message on its last word, and a queue out ignores TLAST:

```verilog
  assign k_stat_TLAST = 1'b0;
  assign k_out_TLAST = 1'b0;
  assign k_resp_TLAST = 1'b0;
  mm_fir u_fir (
    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),
    .s_cfg_TDATA(k_cfg_TDATA), .s_cfg_TVALID(k_cfg_TVALID), .s_cfg_TREADY(k_cfg_TREADY),
    .s_in_TDATA(k_in_TDATA), .s_in_TVALID(k_in_TVALID), .s_in_TREADY(k_in_TREADY),
    .m_out_TDATA(k_out_TDATA), .m_out_TVALID(k_out_TVALID), .m_out_TREADY(k_out_TREADY),
    .m_resp_TDATA(k_resp_TDATA), .m_resp_TVALID(k_resp_TVALID), .m_resp_TREADY(k_resp_TREADY),
    .m_status_TDATA(k_stat_TDATA), .m_status_TVALID(k_stat_TVALID), .m_status_TREADY(k_stat_TREADY)
  );
```

## The host program

The host is the pysim [`FirHost`](python.md#the-host-program), written in C++ against the endpoints of
[`xsi_mm_host.h`](../../../waveflow/build/xsi/xsi_mm_host.h) — the C++ twins of the Python endpoints:

| view | Python endpoint | C++ endpoint |
|---|---|---|
| register bank, config | `StreamIFMaster` | `MmRegBankCfg` |
| register bank, status | `LatestValueIFSlave` | `MmStatusReader` |
| queue in | `StreamIFMaster` | `MmQueueWriter` |
| queue out (results, responses) | `MmStreamIFSlave` | `MmQueueReader` |

An XSI participant cannot block, so each C++ endpoint is a small state machine: the host calls
`start(...)`, then `step()` once per cycle, until `busy()` is false. The rules inside are the Python
ones, line for line, so the two hosts issue the same kinds of bus operations for the same reasons.

**The interrupts.** The top routes each queue view's `irq` output to a port (`irq_qin`, `irq_qout`,
`irq_qresp`), and the testbench samples each as an `IrqPin`. A queue endpoint given one
(`use_irq(pin)`) waits on it exactly as the Python endpoint waits on its `IrqIFSink`: set the view's
threshold (a bus write, only when it changes), wait for the pin, move the words. No count is ever
read:

```cpp
    IrqPin irq_qin(sim.dut(), "irq_qin"), irq_qout(sim.dut(), "irq_qout"), irq_qresp(sim.dut(), "irq_qresp");
    Reader rd(host, irq_qout, irq_qresp);
    Writer wr(host, irq_qin);
```

Two things are generated from Python, so the C++ restates neither:

- **The address map, in two halves.** `address_headers()` walks the pysim system's crossbar
  (`bus_address_headers`) and returns a **layout** header for the FIR type — offsets within the slave,
  from `MmFir.mm_views` — and a **bases** header for this system — where the FIR instance is placed:

  ```cpp
  namespace mm_fir_layout {
  static const uint64_t SPAN = 0x4000ull;
  static const wfbfm::MmViewLayout regs = {"regs", wfbfm::MmKind::RegBank, 0x0ull, 4096u, 8u, 0u, 5u, 1u, 0u};
  static const wfbfm::MmViewLayout qin = {"qin", wfbfm::MmKind::QueueIn, 0x1000ull, 4096u, 8u, 64u, 0u, 0u, 0u};
  ...
  }
  namespace mm_fir_bases {
  static const uint64_t FIR_BASE = 0x0ull, FIR_SPAN = 0x4000ull;
  }
  ```

  The testbench includes both (its `#include` lines come from the same walk) and builds every endpoint
  as `at(mm_fir_layout::qin, FIR)` — layout plus base. The offsets inside a window — COMMIT at `W/2`,
  status at `3W/4` — are in neither header: `MmView` computes them, as `ViewEntry` does in Python.
- **The schedule.** `host_schedule` — the list of configs and sample packets the pysim host sends — is
  rendered as a table: each config's words, and each packet's `FirCmdHdr` word, its samples as the
  serializer packs them (four int16 to a word), and the response the host expects back. The C++
  `Writer` walks it, and the `Reader` reads its packet sizes and expected responses from it.

The host itself is two `XsiSimObj`s sharing one `AxiMmMaster`, as the pysim host is two processes
sharing one `MMIFMaster`:

```cpp
class Writer : public XsiSimObj {
    ...
    void update() override {
        cfg_.step(); qin_.step();
        if (phase_ == SEND_CFG && !cfg_.busy()) next();
        else if (phase_ == SEND_HDR && !qin_.busy()) { qin_.start(SCHEDULE[i_].samples); phase_ = SEND_SAMP; }
        else if (phase_ == SEND_SAMP && !qin_.busy()) next();
        if (phase_ == IDLE && i_ < SCHEDULE.size()) {
            const Item& it = SCHEDULE[i_];
            if (it.kind == CFG) { cfg_.start(it.words); phase_ = SEND_CFG; }
            else                { qin_.start(it.words); phase_ = SEND_HDR; }
        }
    }
};
```

The `Writer` never waits for a config to be received: the header's `cfg_id` makes the kernel wait. The
`Reader` takes each packet's results, then its response from the response FIFO, and counts any
response whose `tx_id` or `cfg_id` is not what the schedule expects; after the last one it reads the
status once:

```cpp
        else if (phase_ == RESP && !qresp_.busy()) {
            const Item& it = SCHEDULE[i_];
            ++nresp;
            if (field(qresp_.words, 0, 32, 16) != it.tx || field(qresp_.words, 0, 48, 16) != it.want) ++mismatches;
            ++i_; phase_ = IDLE;
        }
```

The field positions and widths — where `tx_id` and `cfg_id` sit in the response word — are read off
`FirRespHdr`'s own serializer and field types by `field_pos`, not written into the C++.

### The bus master: one read and one write at once

AXI's read channels and write channels are independent, and AMD's crossbar routes a read and a write
in parallel. So a host whose writer and reader run concurrently — this one — can have a read and a
write in flight at the same time on a real bus. The testbench's bus master is told so where it is
built, in `render_tb`:

```cpp
AxiMmMaster host(sim.dut(), "s0_axi", 8, 0, /*overlap_rw=*/true);
```

The `true` comes from one constant at the top of
[`mm_fir_xsi.py`](../../../examples/mm_fir/mm_fir_xsi.py):

```python
OVERLAP_RW = True
```

With `overlap_rw`, `AxiMmMaster` keeps one read and one write outstanding, each channel in the order
it was queued. Without it (the default, and what every other gate uses) it serves one transaction at
a time — a model of a single-threaded driver, which waits for each access to finish before the next.
Order *between* a read and a write is then up to the host, as on a real bus: the endpoints never
issue a read that depends on a write until the write's response has come back.

The pysim side needs no setting: a pysim `MMIFMaster` already lets a read and a write run at once.

Set `OVERLAP_RW = False` to see the one-at-a-time master; the history table under Results shows what
it cost on the earlier protocol.

## Results

| topology | bit-exact vs `fir_golden` | status `nsamp / ncfg` | responses | cycles | bus operations | polls |
|---|---|---|---|---|---|---|
| `per_view` | yes | 200 / 2 | 13, 0 mismatches | **618** | 67 | 0 |
| `one_front` | yes | 200 / 2 | 13, 0 mismatches | **611** | 67 | 0 |

Both shapes produce the golden's 200 outputs bit for bit through the switch at sample 101, both take
both configs, and every one of the 13 responses echoes its packet's `tx_id` and intended config.

**No polls:** the gate parses every bus operation the testbench host issued and checks that none reads
a count (queue in's vacancy, a queue out's occupancy) and that the status is read exactly once.

**Against pysim.** pysim says 635 for both -- 2.8% and 3.9% over RTL -- once it charges the
straight-line body's measured per-packet costs (header, loop drain, status and response, restart),
found with handshake probes; see [Python simulation](pysim.md#how-close-is-pysims-timing). The cycle
gate also checks pysim stays within 5%.

**How the numbers got here**, on the same scenario:

| host program | `per_view` | `one_front` |
|---|---|---|
| this one: header + `cfg_id` + responses, packed samples, the host on interrupts; the body straight-line per packet | **618** | **611** |
| the same, the body a single-firing state machine (no drain between packets) | 520 | 529 |
| the same, the host polling the counts | 768 | 783 |
| the same, one sample per 64-bit word | 937 | 922 |
| `apply_at` configs, host polls status for "received", overlapping master | 567 | 721 |
| the same, one-transaction-at-a-time master (`OVERLAP_RW = False`) | 811 | 776 |
| single-process host, drained queue out before every push | 857 | 823 |

Waiting on interrupts instead of polling roughly halves the bus operations (67 against 124) and the
time: a poll that finds nothing still occupies the bus, and an interrupt costs nothing until it fires.
The header pattern itself costs a header write per packet and a response pop; what it buys is that the
host never waits on a status round trip, and every packet's config is checked.

## Before the run: is this the RTL I think it is?

The gate refuses to run against kernel RTL that was not built from the sources on disk: csynth writes a
content stamp beside the project, and `rtl_staleness(ROOT, "mm_fir")` compares it before anything is
simulated. A cycle count measured against someone else's build is worse than no count, because it
invites re-recording a number on the strength of an artifact nobody produced on purpose.

The crossbar, the leaves and the top are regenerated on every run, so they cannot be stale.
