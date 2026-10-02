---
title: RTL simulation
parent: A memory-mapped FIR
nav_order: 4
has_children: false
summary: "The whole system as RTL under XSI: AMD's axi_crossbar, the hand-written adaptor leaves, and the csynth'd mm_fir kernel in one generated Verilog top, driven by a C++ host program that runs the pysim host's protocol. Two adaptor shapes — one view per crossbar slot, and all three behind one front with a generated decoder — both bit-exact against the numpy golden through a mid-stream tap switch, at 857 and 823 cycles."
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
| vendor IP | `axi_crossbar`, 1 SI, 2 or 3 MI | `generate_axi_xbar(XBARS[topology], ...)` |
| hand-written RTL | `axi_slave_front`, `mm_regbank`, `mm_queue_in`, `mm_queue_out` | `waveflow/build/rtl/` |
| RTL top | `mm_fir_top`: the three above, wired | `render_top(top, topology)` |
| testbench (the harness) | an `AxiMmMaster` and a `HostProgram` state machine | `render_tb(dll, x)` |

The RTL top is assembled by this example's `render_top` from framework pieces — it is not yet emitted
by `wrapper_gen` from the module graph, the way a design's memories are.

## Two topologies, one address map

```python
XBARS = {
    "per_view": AxiXbarConfig(
        name="xbar_mm3_1x3", n_si=1,
        mi=[AxiXbarRange(REGS, 12), AxiXbarRange(QIN, 12), AxiXbarRange(QOUT, 12)],
        data_width=DW, addr_width=32, id_width=1),
    "one_front": AxiXbarConfig(
        name="xbar_mm1_1x2", n_si=1,
        mi=[AxiXbarRange(REGS, adaptor_law(3)), AxiXbarRange(0x0001_0000, 12)],
        data_width=DW, addr_width=32, id_width=1),
}
```

- **`per_view`** — each view is its own crossbar slave, with its own front (`render_view_slot`). Three
  4 KB windows at `0x0000`, `0x1000`, `0x2000`.
- **`one_front`** — all three views behind **one** front and a generated decoder
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
         QueueView("qout", "out", axis="k_out", depth=QDEPTH)]
```

`NCFG` and `NSTAT` are read off the schemas, so the register bank's shadow and status sizes cannot
drift from the messages the kernel exchanges.

### The generated decoder

In the `one_front` top, this is all the decoding there is — a table from address bits to views:

```verilog
  axi_slave_front #(.DW(64), .AW(32), .IDW(1), .LAW(14)) u_fir_mm_front ( ... );
  wire [1:0] fir_mm_sel = fir_mm_req_addr[13:12];
  wire fir_mm_hole = (fir_mm_sel >= 3);
  ...
  assign regs_req_valid = fir_mm_req_valid && (fir_mm_sel == 0);
  assign regs_req_addr  = fir_mm_req_addr[11:0];
  mm_regbank #(.DW(64), .LAW(12), .NCFG(5), .NSTAT(2)) u_regs ( ... );
  assign qin_req_valid = fir_mm_req_valid && (fir_mm_sel == 1);
  mm_queue_in #(.DW(64), .LAW(12), .DEPTH(64)) u_qin ( ... );
  ...
  assign fir_mm_rsp_rdata = fir_mm_hole_rsp ? {64{1'b0}} :
                            ((fir_mm_rsel == 0) ? regs_rsp_rdata : (fir_mm_rsel == 1) ? qin_rsp_rdata : ...);
```

A request goes to the view its address selects; a read response comes from the view selected when the
read was accepted (`fir_mm_rsel`, latched — the front has one read outstanding); the fourth window,
unused, answers SLVERR.

### Joining the kernel

The kernel's four streams meet the views on `k_cfg`, `k_stat`, `k_in`, `k_out`. The kernel has no TLAST
pins, so the TLASTs the leaves *drive* go nowhere, and the two they *read* are tied low — the status
bank completes a message on its second word, and queue out ignores TLAST:

```verilog
  assign k_stat_TLAST = 1'b0;
  assign k_out_TLAST = 1'b0;
  mm_fir u_fir (
    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),
    .s_cfg_TDATA(k_cfg_TDATA), .s_cfg_TVALID(k_cfg_TVALID), .s_cfg_TREADY(k_cfg_TREADY),
    .s_in_TDATA(k_in_TDATA), .s_in_TVALID(k_in_TVALID), .s_in_TREADY(k_in_TREADY),
    .m_out_TDATA(k_out_TDATA), .m_out_TVALID(k_out_TVALID), .m_out_TREADY(k_out_TREADY),
    .m_status_TDATA(k_stat_TDATA), .m_status_TVALID(k_stat_TVALID), .m_status_TREADY(k_stat_TREADY)
  );
```

## The host program

The pysim host is a Python generator that decides as it goes: poll until the config is received, wait
until there is room. At RTL the host is a C++ model, and those decisions become a **state machine over
bus operations**. `host_actions(x)` lays the protocol out as a flat list — the same decisions, in the
same order, as `FirHost`:

```python
def host_actions(x) -> list[tuple]:
    """The FirHost protocol as a flat action list (same decisions as the pysim host)."""
    acts: list[tuple] = []
    cfgs = sorted(PLAN, key=lambda c: c[0])
    nxt, n = 0, 0
    while n < len(x):
        while nxt < len(cfgs) and cfgs[nxt][0] <= n:
            words = [int(w) for w in make_cfg(cfgs[nxt][1], cfgs[nxt][0]).serialize(word_bw=DW)]
            acts += [("W", REGS, words), ("W", REGS + 0x800, [1]), ("POLL_NCFG", nxt + 1)]
            nxt += 1
        end = min(n + PKT, len(x), cfgs[nxt][0] if nxt < len(cfgs) else len(x))
        chunk = [int(v) & 0xFFFF for v in x[n:end]]
        acts += [("DRAIN", 0), ("WAIT_VAC", len(chunk)), ("W", QIN, [len(chunk)] + chunk)]
        n = end
    acts += [("DRAIN_ALL", len(x)), ("STATUS", 0)]
    return acts
```

For the gate's 200 samples that is 50 actions, beginning:

| action | address | |
|---|---|---|
| `W` | `0x0000` | the 5-word `FirCfg` into the shadow |
| `W` | `0x0800` | COMMIT |
| `POLL_NCFG` | `0x0C00` | read the status until `ncfg ≥ 1` |
| `DRAIN` | `0x2800`, `0x2000` | read the occupancy; pop that many results |
| `WAIT_VAC` | `0x1000` | read the vacancy until 16 slots are free |
| `W` | `0x1000` | the packet `[16 \| 16 samples]` |

`render_tb` turns the list into a C++ `HostProgram`: an `XsiSimObj` that, each cycle, checks whether the
`AxiMmMaster`'s current operation is done, reacts to the result (a `POLL_NCFG` that read `ncfg = 0`
issues the same read again, eight cycles later), and issues the next. The field positions it decodes
from the status — where `ncfg` sits in the two words — are read off `FirStatus`'s own serializer by
`field_pos`, not written into the C++.

## Results

| topology | bit-exact vs `fir_golden` | status `nsamp / ncfg / late` | cycles | bus operations | status polls |
|---|---|---|---|---|---|
| `per_view` | yes | 200 / 2 / 0 | **857** | 68 | 2 |
| `one_front` | yes | 200 / 2 / 0 | **823** | 68 | 2 |

Both shapes produce the golden's 200 outputs bit for bit through the switch at sample 101, and both
report every config received in time.

`one_front` is 34 cycles faster over the same 68 operations — half a cycle per operation. Where that
comes from (a 1×2 instead of a 1×3 crossbar, or one front instead of three) has **not** been isolated;
the cycle counts are exact and gated, the attribution is open.

pysim predicts 709 for both. The gap is mostly the C++ host's own pacing — it starts each operation
two cycles after the previous one ends, and the protocol issues four per packet — rather than the
adaptor, which tracks RTL to within 2 cycles per operation on its own. See
[Python simulation](pysim.md#how-close-is-pysims-timing).

## Before the run: is this the RTL I think it is?

The gate refuses to run against kernel RTL that was not built from the sources on disk: csynth writes a
content stamp beside the project, and `rtl_staleness(ROOT, "mm_fir")` compares it before anything is
simulated. A cycle count measured against someone else's build is worse than no count, because it
invites re-recording a number on the strength of an artifact nobody produced on purpose.

The crossbar, the leaves and the top are regenerated on every run, so they cannot be stale.
