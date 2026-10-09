---
title: RTL simulation
parent: A memory-mapped FIR
nav_order: 4
has_children: false
summary: "The results of the whole system as RTL under XSI (built and run by the three pages before: Build flow, Synthesis, XSI testbench): AMD's axi_crossbar, the hand-written adaptor leaves, and the csynth'd mm_fir kernel in one generated Verilog top, driven by the pysim host program written against C++ endpoints, with an address map generated from the pysim system. Two adaptor shapes — one view per crossbar slot, and all four behind one front with a generated decoder — both bit-exact against the numpy golden through a mid-stream tap switch, every response checked, no polling (the host sleeps on the queue views' interrupts), at 618 and 611 cycles."
---

# RTL simulation

The whole system at RTL: AMD's crossbar, the framework's view leaves and the csynth'd `mm_fir` kernel
under one generated Verilog top, driven by `FirHost`'s C++ twin and checked against pysim, in both
adaptor topologies. How it is built and run is three pages: [Build flow](build.md) (the DAG, which
steps are the example's), [Synthesis](synth.md) (the kernel, the crossbar, the top and the two
topologies) and [XSI testbench](xsi.md) (the host program, the harness, the conformance gate).

```bash
python -m examples.mm_fir.mm_fir_build             # both topologies, end to end
pytest tests/examples/test_mm_fir_xsi.py -m xsi     # the gates: bit-exact, cycles, traces
```

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
