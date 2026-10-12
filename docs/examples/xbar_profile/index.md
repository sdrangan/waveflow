---
title: What a crossbar costs
parent: Examples
nav_order: 9.552
example_dir: examples/xbar_profile
summary: "A measurement census rather than a design: AMD's axi_crossbar in every small shape (1x2 to 4x4), both connectivity modes (SAMD crossbar, SASD shared bus) and two data widths, each synthesized, placed and routed out of context at 250 MHz and run under XSI between host and memory BFMs. The area, the timing-closure margin and the latency of every configuration are filed into a platform's module record store -- the same records a resource model fits from -- and the tables on this page are rendered back out of that store."
---

# What a crossbar costs

[mm_fir](../mm_fir/) put AMD's `axi_crossbar` between a host and a kernel, and the
[crossbar guide](../../guide/interface/axi_mm/crossbar.md) measured what a transaction through it costs
in cycles. Two questions were left open: **how much fabric** a crossbar takes, and **how both answers
change** with its shape and its connectivity mode. This example measures them for every small
configuration and keeps the numbers where a model can use them.

The crossbar has two architectures (`AxiXbarConfig(mode=...)`):

| mode | what it is |
|---|---|
| **SAMD** -- shared address, multiple data | a true crossbar: one address arbiter, but a data path per master/slave pair, so different pairs transfer at once. The IP's default, and what every other example builds. |
| **SASD** -- shared address, shared data | a shared bus: one transaction at a time across the whole interconnect, for the least area. |

## What is measured

Every point of a grid -- shapes `1x2`, `2x1`, `2x2`, `3x3`, `4x4` (masters x slaves), both modes,
data widths 32 and 64 -- goes through four rungs
([`xbar_profile_build.py`](../../../examples/xbar_profile/xbar_profile_build.py)):

| rung | what it does | costs |
|---|---|---|
| `ip` | `create_ip` for the configuration (`generate_axi_xbar`) | ~20 s, cached |
| `synth` | out-of-context synthesis, place and route at 4 ns (`synth_axi_xbar`); utilization after each, routed setup/hold slack, logic levels, critical path | ~4 min, cached |
| `latency` | the IP under XSI, a host BFM on every master port and a memory BFM on every slave; the probe set below | ~30 s |
| `records` | files the measurements into the platform's module record store | -- |

There is no `1x1`: the IP refuses it, and a crossbar with one master and one slave routes nothing.
`1x2` (a host reaching two slaves) and `2x1` (two masters sharing a memory) are the small shapes
designs actually build.

The **latency probes** (`xbar_latency_probes`) each start every operation in the same cycle and
report the span from the first address phase to the last response, inclusive:

| probe | traffic | what it shows |
|---|---|---|
| `w1`, `r1` | master 0 to slave 0, one beat, alone | the per-transaction cost |
| `w16`, `r16` | the same with 16 beats | the per-beat cost |
| `par_w16`, `par_r16` | master *k* to slave *k*, for every *k*, at once | whether different pairs overlap -- the SAMD/SASD difference |
| `same_w16`, `same_r16` | every master to slave 0 at once | arbitration: bursts to one slave serialize in either mode |

Every read checks it gets back what its write stored, so a probe that moved the wrong data fails
rather than reporting a latency.

## The results

Measured 2026-10-11, Vivado 2025.1, on `xczu7ev-ffvc1156-2-e` (see [the part](#the-part-a-stand-in-for-the-rfsoc)), rendered by `xbar_profile_table` from the record store. No configuration uses block RAM or DSPs. WNS is the routed worst setup slack against the 4 ns clock -- positive means it closes.

### Area and timing closure at 250 MHz (routed, out of context)

| shape | width | SAMD LUT | SAMD FF | SAMD WNS (ns) | SASD LUT | SASD FF | SASD WNS (ns) |
|---|---|---|---|---|---|---|---|
| 1x2 | 32 | 237 | 379 | +2.178 | 197 | 166 | +2.526 |
| 2x1 | 32 | 336 | 313 | +1.862 | 245 | 171 | +2.402 |
| 2x2 | 32 | 472 | 417 | +1.908 | 285 | 173 | +2.006 |
| 3x3 | 32 | 798 | 574 | +1.637 | 409 | 179 | +2.148 |
| 4x4 | 32 | 1213 | 722 | +1.494 | 531 | 187 | +2.104 |
| 1x2 | 64 | 302 | 507 | +2.308 | 262 | 230 | +2.637 |
| 2x1 | 64 | 401 | 377 | +1.851 | 296 | 235 | +2.371 |
| 2x2 | 64 | 604 | 545 | +2.159 | 367 | 237 | +2.265 |
| 3x3 | 64 | 1049 | 766 | +1.531 | 492 | 243 | +1.887 |
| 4x4 | 64 | 1679 | 978 | +1.451 | 647 | 251 | +1.730 |

### Latency in cycles (XSI; first address phase to last response, inclusive)

| shape | width | mode | w1 | r1 | w16 | r16 | par_w16 | par_r16 | same_w16 | same_r16 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1x2 | 32, 64 | SAMD | 6 | 5 | 20 | 19 | -- | -- | -- | -- |
| 1x2 | 32, 64 | SASD | 5 | 5 | 20 | 20 | -- | -- | -- | -- |
| 2x1 | 32, 64 | SAMD | 6 | 5 | 21 | 20 | -- | -- | 39 | 37 |
| 2x1 | 32, 64 | SASD | 5 | 5 | 20 | 20 | -- | -- | 40 | 40 |
| 2x2 | 32, 64 | SAMD | 6 | 5 | 21 | 20 | 24 | 23 | 39 | 37 |
| 2x2 | 32, 64 | SASD | 5 | 5 | 20 | 20 | 40 | 40 | 40 | 40 |
| 3x3 | 32, 64 | SAMD | 6 | 5 | 21 | 20 | 27 | 26 | 57 | 54 |
| 3x3 | 32, 64 | SASD | 5 | 5 | 20 | 20 | 60 | 60 | 60 | 60 |
| 4x4 | 32, 64 | SAMD | 6 | 5 | 21 | 20 | 30 | 29 | 75 | 71 |
| 4x4 | 32, 64 | SASD | 5 | 5 | 20 | 20 | 80 | 80 | 80 | 80 |

## Reading them

**Every configuration closes at 250 MHz, with room to spare.** The tightest is the 64-bit 4x4 SAMD
crossbar at +1.451 ns -- 36% of the period unused -- so none of these shapes needs a register slice
for its own logic at the RFSoC's fabric clock. Slack falls as the crossbar widens (SAMD: about
+2.2 ns at 1x2 to +1.5 ns at 4x4), the way a wider arbiter and deeper data muxes would make it.

**SASD is the area answer, SAMD is the bandwidth answer.** At 4x4 the shared bus takes 2.3x (32-bit)
to 2.6x (64-bit) fewer LUTs and about 4x fewer flip-flops. Its flip-flop count barely moves with the
shape (166 to 187 at 32 bits): it has one data path, and only the arbiter grows. SAMD's LUTs grow
faster than linearly with the shape -- 472, 798, 1213 from 2x2 to 4x4 at 32 bits -- because it
has a data path for every master/slave pair. Widening the data from 32 to 64 bits costs SAMD 466 LUTs
at 4x4 and SASD 116. All of it is small next to the RFSoC's ~425k LUTs: the largest is 0.4%.

**Width never changes latency** -- every probe measured the same cycle count at 32 and 64 bits -- and
neither does the shape, for a transfer that is alone:

- **Alone**, a read costs 5 cycles plus one per extra beat (`r1` = 5, `r16` = 20) in either mode;
  a SAMD write costs one more for its response (`w1` = 6, `w16` = 21), a SASD write the same as its
  read. (A 1x2 SAMD crossbar is a cycle quicker on 16 beats.) That is the pysim crossbar's
  `latency_init = 4` plus one cycle per word, as the [crossbar guide](../../guide/interface/axi_mm/crossbar.md#matching-pysim-to-it)
  set it from mm_fir.
- **Different masters to different slaves** (`par_*`) is where the modes part. SAMD runs them side by
  side and pays 3 cycles per extra master -- the shared address arbiter grants one address at a time
  -- so four 16-beat writes finish in 30 cycles rather than 21. SASD serializes them completely: 20
  cycles per master, 80 for four.
- **Several masters to one slave** (`same_*`) serializes in both modes, as AXI4 requires. SAMD
  overlaps the next master's address phase with the current burst, so each extra master adds 18 cycles
  to a write (16 beats + 2) and 17 to a read; SASD adds the full 20.

**So for one master, or one slave, SASD is nearly free.** A `1x2` crossbar (a host reaching two
slaves) has no concurrency to lose -- its latencies match SAMD's to within a cycle -- and SASD saves
40 LUTs and 213 flip-flops at 32 bits. A `2x1` (two masters sharing a memory) serializes at the slave
anyway: SASD takes one cycle longer for the pair of writes and three for the pair of reads, and saves
91 LUTs and 142 flip-flops. SAMD earns its
area only when different pairs genuinely transfer at once.

## The part: a stand-in for the RFSoC

The board this is for is the RFSoC 4x2 (`xczu48dr-ffvg1517-2-e`). Synthesizing for an RFSoC needs a
Vivado license that covers the device, and the no-cost edition does not -- `synth_design` stops with
*"A valid license was not found for feature 'Synthesis' and/or device 'xczu48dr'"*, while `create_ip`
and XSI work. So the numbers above are measured on the closest part the no-cost edition covers, the
ZCU104's **`xczu7ev-ffvc1156-2-e`**:

- **same fabric** -- both are Zynq UltraScale+ (CLB, LUT6, the same routing architecture);
- **same speed grade**, -2;
- **the same circuit.** `create_ip` builds the crossbar's RTL from the *family*
  (`C_FAMILY = zynquplus`), and generating it for both parts gives byte-identical synthesis wrapper,
  HDL library and simulation model.

So the latencies are exactly the RFSoC's (it is the same RTL), the area should match to within
placement noise, and the closure margin is the one number to re-measure on the real part. With a
license that covers it:

```bash
python -m examples.xbar_profile.xbar_profile_build --part xczu48dr-ffvg1517-2-e --platform \
    --shape 4x4 --mode SAMD --dw 64
```

Records carry the part they were measured on (their provenance), and the work-tier platform is named
for it (`zu7ev_xbar_250mhz`), so nothing measured on the stand-in can pass for an RFSoC number.

**Out of context, only internal paths are timed.** The crossbar's ports have no partition pins, so
Vivado times its register-to-register paths and leaves the port-to-register ones unconstrained. A
slack here says the crossbar's own logic closes at 250 MHz; whether a system closes depends on what
sits on either side of it.

## Where the numbers live

Not in this page, and not in a results file. Each measurement is a **record** in the platform's
module store (`waveflow/calib/record_store.py`), filed under the crossbar's content key:

```python
from waveflow.calib.record_store import ModuleStore
from examples.xbar_profile.xbar_profile import xbar_config

cfg = xbar_config("4x4", "SAMD", 64)
ident = cfg.identity()                    # axi_crossbar-<digest of the hardware fields>
store = ModuleStore("examples/xbar_profile/calib/work/zu7ev_xbar_250mhz")
store.best(ident.key, "resource", identity=ident).payload    # vivado_impl: lut, ff, ..., wns_ns, met
store.read(ident.key, "timing", identity=ident)              # one xsi record per probe
```

- `identity()` hashes the hardware -- mode, shape, widths, address windows -- and not the name or the
  part, so two designs that build the same crossbar share a key and a synthesis.
- A key holds a `vivado_synth` and a `vivado_impl` resource record and one `xsi` timing record per
  probe, `{features: {op, beats, masters, slaves}, cycles}`. `ModuleStore.best` prefers the routed
  number.
- The tables above are rendered from the store by
  [`xbar_profile_table.py`](../../../examples/xbar_profile/xbar_profile_table.py), the way any
  later reader would see it.

That is what makes a **crossbar resource model** a small step rather than a project:
`corpus_from_records(store, cls_name="AxiCrossbar")` is already the frame one fits from, with
`n_si`, `n_mi`, `data_width` and `mode` as its columns. The sweep writes to an untracked work-tier
platform; `waveflow_calib publish` promotes a platform into the tracked library when someone decides
the numbers belong there (see [the platform workflow](../../guide/platform/workflow.md); the record store is [per-module records](../../guide/calib/modules.md)).

## Running it

```bash
python -m examples.xbar_profile.xbar_profile_sweep --dry-run   # IPs only: no synthesis license needed
python -m examples.xbar_profile.xbar_profile_sweep             # the census, ~1.5 h (cached per point)
python -m examples.xbar_profile.xbar_profile_sweep --shape 4x4 --resume
python -m examples.xbar_profile.xbar_profile_table             # the tables, from the store

python -m examples.xbar_profile.xbar_profile_build --shape 3x3 --mode SASD --dw 32   # one point
```

Needs Vivado (`create_ip`, synthesis, xsim) and a C++ compiler for the XSI testbench -- the mingw
`g++` bundled with Vivado on Windows. The plan this implements is
`plans/interconnect_platform_model.md`.
