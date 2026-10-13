---
title: Systems
parent: Resource Analysis Tools
nav_order: 4
has_children: false
audience: python
api: [estimate_system, SystemEstimate, synth_system, SystemSynth, synth_ooc_link, OocLink, KernelResourcesStep, SystemSynthStep, LeafResourcesStep, ResourceReportStep, measure_instances]
summary: "A system's resources: every RTL instance of its top -- the Vitis kernels, the hand-written leaves its interfaces bring, the vendor IP -- priced from its own measurement and grouped by the pysim object it realizes, plus the system integration term, with the measured total of a whole-top synthesis beside the prediction. The composite-kernel decomposition, one level up."
---
# Systems

A [composite kernel](./composite.md) decomposes as *Σ modules + integration*.  A **system** -- several
kernels reached over a bus, each behind a [memory-mapped adaptor](../interface/axi_mm/slave.md), with
AMD's crossbar and an on-chip memory ([markov](../../examples/markov/), [mm_fir](../../examples/mm_fir/))
-- decomposes the same way one level up, and the parts are of three kinds:

| kind | in markov's top | measured by |
|---|---|---|
| Vitis kernel | `u_gen`, `u_chain`, and the two bus writers `u_u_fwd_wr`, `u_u_crd_wr` | its csynth report (`hls_estimate`) |
| hand-written leaf (`waveflow/build/rtl/`) | three `axi_slave_front`s, the queue / credit leaves, `mm_bram_port` + `bram_t2p`, an `mm_sync_fifo` | Vivado, out of context or in the system |
| vendor IP | `u_xbar` (`axi_crossbar`) | Vivado, the same two ways |

**Most of the RTL is not a module.**  The leaves come from *interfaces and adaptors* -- a device's
views, a deep link's FIFO, the crossbar -- and even the two bus writers, which are HLS tops, are brought
by an interface (`MmCreditStreamIF`).  A module walk (`compose`) cannot see them, so a system is priced
from its **instance inventory** instead.

## The instance inventory

`SystemTopSpec.instances()` lists every instance the generated top holds, from the same spec objects
the Verilog is rendered from -- so the list and the top cannot disagree:

```python
from examples.markov.markov_build import build_dag
spec = next(s for s in build_dag().steps() if s.name == "system_rtl").spec
for i in spec.instances():
    print(f"{i.inst:16} {i.module:24} {i.kind:6} {getattr(i.owner, 'name', '')}")
```

```text
u_u_fwd_ff       mm_sync_fifo             leaf   u
u_xbar           xbar_markov_4x3          ip     xbar
u_gen_mm_front   axi_slave_front          leaf   gen_mm
u_gen_qcmd       mm_queue_in              leaf   gen_qcmd
...
u_gen            markov_gen               kernel gen
u_u_fwd_wr       mm_queue_writer_64_128   kernel u
```

Each instance carries its parameters, its sources, an **identity** -- a kernel's structure key, a
leaf's module + parameters + a digest of every source it compiles from, the crossbar's configuration
-- and its **owner**, the pysim object it realizes: the kernel, the view or adaptor, the memory, or the
interface (the routed channel `u` owns its two writers and the FIFO feeding one).  Editing
`mm_queue_in.v` moves that leaf's identity, so its old numbers become unreachable rather than silently
wrong.  The XSI file list is the inventory's too: a system compiles only the leaves it uses.

## Where each number comes from

A system build ([`add_system_steps`](../build/xsi_system.md)) adds four resource steps:

```text
csynth ──> kernel_resources ──────────────┐
system_rtl ──> leaf_resources ────────────┴──> resource_report
system_rtl ──> system_synth   (opt-in; resource_report reads its records)
```

| step | does | cost |
|---|---|---|
| `kernel_resources` | each kernel's csynth report attributed ([as for a composite](./composite.md)), filed as `hls_estimate` records with their owner | seconds |
| `leaf_resources` | each leaf / IP configuration looked up by identity; with `measure_leaves`, a missing one synthesized out of context once and filed | lookup; ~1 min per new configuration |
| `system_synth` | the whole top synthesized in Vivado, every instance attributed from `report_utilization -hierarchical` | minutes; cached by the system's identity |
| `resource_report` | the estimate, row by row, with the measured total beside it | milliseconds |

Records go to the build's [platform](../calib/) (`--platform`), at the part and clock the kernels were
csynth'd for.  A build **writes** its work-tier platform and a lookup **reads** that first and then the
published library of the same name, so the next estimate sees what the build just filed.

## The system synthesis {#the-system-synthesis}

`python markov_build.py --through system_synth --platform <name>` synthesizes markov's top out of context
(xc7z020, 10 ns; 116 s of Vivado 2025.1) and attributes every inventory instance:

| instance | LUT | FF | BRAM18 |
|---|---:|---:|---:|
| `u_chain` (`markov_chain`) | 2032 | 3115 | 0 |
| `u_gen` (`markov_gen`) | 1193 | 1298 | 0 |
| `u_xbar` (`axi_crossbar`, 4 × 3) | 849 | 680 | 0 |
| `u_u_fwd_wr` (queue writer) | 811 | 1020 | 2 |
| `u_u_crd_wr` (credit writer) | 508 | 622 | 0 |
| `u_chain_qu` (`mm_queue_in`, 128 deep) | 322 | 64 | 0 |
| `u_mem_mem` (`bram_t2p`, 512 × 64) | 64 | 0 | 2 |
| eight more leaves | 544 | 386 | 0 |
| **integration** (the top's own cells) | −36 | 2 | 0 |
| **total** | **6287** | **7187** | **4** |

Instances plus integration equal the total exactly -- integration is *defined* as the remainder, and
negative is legal.

{: .note }
> These numbers were measured on the flat top. The top now holds each
> [device and the memory as a sub-top](../build/xsi_system.md#devices), and the step reads the report
> one level deeper (`-hierarchical_depth 2`), joining each row to the inventory by its path
> (`u_gen_sub/u_u_fwd_wr`). The same synthesis of the hierarchical top (2026-10-11) gives
> **6,281 LUT / 7,187 FF / 4 BRAM18** with integration +2 LUT. Vivado's default
> `-flatten_hierarchy rebuilt` dissolves the sub-tops, so the global result is the flat one to
> within 0.1%.

**A dissolved instance is still its own.**  Under Vivado's default `-flatten_hierarchy rebuilt`,
`u_mem_mem` and `u_mem` get no row of their own: synthesis dissolved their hierarchy into the top.
Their cells keep their names, so the step prices each such instance by the primitives under its name
prefix (`u_mem_mem/*`: one RAMB36, 64 LUTs) rather than letting its cost read as glue.  Counting
primitives and the report's site-level LUT count differ slightly (here by 36 LUTs), and the difference
lands, visibly, in the integration term.  An instance that can be priced neither way is an error, not a
zero.  `keep_hierarchy=True` would give every instance its row, at a price: +25% LUT and +30% FF on
markov -- numbers for a design nobody would ship, so it is off by default.

**The memory used to be flip-flops.**  The first synthesis of markov found `bram_t2p` inferring no
block RAM: 8769 LUT and 32832 FF of a 18593 / 40130 total, plus 3625 LUT of read mux in its front.  Both
ports were written in one `always` block, which Vivado's true-dual-port template does not match; with
one block per port it is one RAMB36 (two out of context, where both ports are live).  No XSI gate
moved.  This is what the step is for: the system's dominant cost was a coding-style accident that no
per-module estimate could have shown.

**csynth overestimates LUT about 2×.**  The `hls_estimate` of each kernel against its in-context
synthesis:

| kernel | LUT (hls / vivado) | FF (hls / vivado) |
|---|---|---|
| `markov_gen` | 2585 / 1193 | 1905 / 1298 |
| `markov_chain` | 4423 / 2032 | 3030 / 3115 |
| `mm_queue_writer_64_128` | 1531 / 811 | 943 / 1020 |
| `mm_credit_writer_64` | 1097 / 508 | 874 / 622 |

So *how* a number was obtained (an exact lookup) and *what* it measures (an HLS estimate) are separate
columns of the report.

## Per device: synthesized apart and linked {#per-device}

The system synthesis above is **global**: one run that optimizes across every boundary. A block
design of packaged IPs is synthesized another way by default: each IP on its own, then linked. The
param `synth_route="per_device"` on `system_synth` does that (`markov_build.py --through system_synth
--route per_device`, [`system_ooc.synth_ooc_link`](../../../waveflow/build/system_ooc.py)):

1. each [device and the memory](../build/xsi_system.md#devices), rendered as a module of its own,
   synthesized out of context with its netlist kept;
2. the top (the crossbar and the glue) synthesized against a black box per device;
3. the netlists linked (`link_design`, each part scoped to its cell), then `opt_design`.

Each part and the top are cached by the content of everything they read. A change to one device
re-synthesizes that device and re-links, and nothing else.

On markov (xc7z020, 10 ns):

| | LUT | FF | BRAM18 |
|---|---:|---:|---:|
| gen device, alone | 2,299 | 2,560 | 2 |
| chain device, alone | 3,028 | 3,934 | 0 |
| memory, alone | 103 | 39 | 2 |
| top (crossbar and glue), alone | 1,292 | 808 | 0 |
| link term | −472 | −242 | 0 |
| **linked** | **6,250** | **7,099** | **4** |
| global synthesis + `opt_design` | 6,228 | 7,091 | 4 |

The parts plus the link term equal the linked total exactly. The link term is measured, not assumed,
and it is mostly the crossbar's: 1,292 LUT out of context, against 849 in context. Against the global
synthesis with the same `opt_design`, synthesizing apart costs 22 LUT (0.35%) and 8 FF. The memory
needs one RAMB36 even alone, because its idle port B is tied off inside its own module.

The time is what changes:

| rebuild | wall clock |
|---|---:|
| global synthesis + `opt_design` | 144 s |
| per device, cold | about 330 s |
| per device, one device changed | 103 s |
| per device, nothing changed | 31 s (the link only) |

So route A, global synthesis, is the cheapest cold build. The per-device route makes each part's
number its own and an incremental change cheap. [plans/ip_packaging_routes.md](../../../plans/ip_packaging_routes.md)
weighs the two.

## The estimate

`estimate_system(sysm, platform)` prices every instance from the store -- no toolchain, no build -- and
groups them by owner, a multi-view adaptor's views rolled into the adaptor:

| instance | priced by, best first |
|---|---|
| kernel | an in-context record from a synthesis of *this* system (matched by the system's identity, not the top's name); else its class's own model (`get_rm`); else its modules' `hls_estimate` records plus its top's integration term |
| leaf / IP | an in-context record; else the configuration's out-of-context record; else its owner's model hook |
| system integration | the system identity's integration record |

A part with no data is a row at `UNCALIBRATED` with zero counters **and** what measuring it would cost
(`weakest()`), never a silent zero -- but the `total` line still sums it, so read the total with the
`weakest:` verdict beside it.  Before any Vivado run, markov's estimate is its kernels:

```text
markov_top   platform scratch_7020
u (MmCreditStreamIF)            2,628    1,817        2        0   UNCALIBRATED   hls_estimate
xbar (AXIMMCrossBarIF)              0        0        0        0   UNCALIBRATED   --
gen_mm (MemSlaveAdaptor)            0        0        0        0   UNCALIBRATED   --
chain_mm (MemSlaveAdaptor)          0        0        0        0   UNCALIBRATED   --
mem (MemoryMod)                     0        0        0        0   UNCALIBRATED   --
gen (MarkovGen)                 2,585    1,905        0        0   EXACT          hls_estimate
chain (MarkovChain)             4,423    3,030        0        0   EXACT          hls_estimate
system integration                  0        0        0        0   UNCALIBRATED   --
total                           9,636    6,752        2        0   weakest: UNCALIBRATED (u (MmCreditStreamIF))
```

`use_in_context=False` ignores the system's own synthesis records: the **composed prediction**, each
part from its own measurement, which the measured total is then a check on -- the area counterpart of
`compare`.

## Prediction against measurement

With markov's kernels attributed, its eleven leaf / IP configurations measured out of context
(`--through leaf_resources` with `measure_leaves`; a minute or so per configuration the first time,
nothing on a re-run) and the top synthesized, the default estimate reads every instance from the
system's own synthesis and reproduces the measured total:

```text
markov_top   platform scratch_7020
                                  LUT       FF     BRAM      DSP   how            measured by
u (MmCreditStreamIF)            1,355    1,658        2        0   EXACT          vivado_synth (in markov_top)
xbar (AXIMMCrossBarIF)            849      680        0        0   EXACT          vivado_synth (in markov_top)
gen_mm (MemSlaveAdaptor)          230      217        0        0   EXACT          vivado_synth (in markov_top)
chain_mm (MemSlaveAdaptor)        489      178        0        0   EXACT          vivado_synth (in markov_top)
mem (MemoryMod)                   132       39        2        0   EXACT          vivado_synth (in markov_top)
gen (MarkovGen)                 1,193    1,298        0        0   EXACT          vivado_synth (in markov_top)
chain (MarkovChain)             2,031    3,115        0        0   EXACT          vivado_synth (in markov_top)
system integration                  2        2        0        0   EXACT          vivado_synth (in markov_top)
total                           6,281    7,187        4        0   weakest: EXACT (u (MmCreditStreamIF))
measured total                  6,281    7,187        4        0   vivado_synth, 2026-10-13   gap +0.0% LUT, +0.0% FF, +0.0% BRAM
```

The **composed** prediction (`--composed`: each part from its own measurement, none from this
synthesis) is the one a design-space exploration has before it synthesizes anything:

```text
u (MmCreditStreamIF)            2,689    1,833        2        0   EXACT          vivado_synth (ooc) + hls_estimate
xbar (AXIMMCrossBarIF)          1,292      808        0        0   EXACT          vivado_synth (ooc)
gen_mm (MemSlaveAdaptor)          262      219        0        0   EXACT          vivado_synth (ooc)
chain_mm (MemSlaveAdaptor)        538      188        0        0   EXACT          vivado_synth (ooc)
mem (MemoryMod)                   148      105        2        0   EXACT          vivado_synth (ooc)
gen (MarkovGen)                 2,585    1,905        0        0   EXACT          hls_estimate
chain (MarkovChain)             4,423    3,030        0        0   EXACT          hls_estimate
system integration                  0        0        0        0   UNCALIBRATED   --
total                          11,937    8,088        4        0   weakest: UNCALIBRATED (system integration)
measured total                  6,281    7,187        4        0   vivado_synth, 2026-10-13   gap +90.0% LUT, +12.5% FF, +0.0% BRAM
```

Every row is `EXACT` -- an exact lookup -- and the total is still nearly 2× the truth, which is the
report's point about keeping *how* apart from *what*:

- **HLS estimates.**  The four kernels' `hls_estimate` rows are ~2× their synthesized LUT (above):
  most of the 5,656-LUT gap.
- **The crossbar out of context** is 1,292 LUT against 849 in the design: 443 more.

The BRAM is exact, because a **tie-off is part of a leaf's identity**.  `bram_t2p` alone is a
two-port memory, two RAMB36.  In markov its kernel-side port B is tied idle, and the one live port
fits one RAMB36.  The system states that tie-off (`RtlInstance.ties`: an input tied to a value, an
output left open), and `identify_rtl` keys on it, so the tied memory is another configuration with
a record of its own. `leaf_resources` measures it inside a generated wrapper that ties the ports as
the system does (`rtl_ooc.tied_wrapper`). An untied leaf keys as it always did, so no record is
orphaned. (Before, the composed BRAM was 6 against the measured 4:
[plans/ip_packaging_routes.md](../../../plans/ip_packaging_routes.md) Stage 2.)

(Before the memory was recoded the composed gap was +412% LUT, nearly all of it a tied-off port on a
flip-flop memory: 83,194 LUT out of context against 8,769 in markov.)

From the command line (`--json` for an agent):

```bash
python -m waveflow.build.system_estimate examples.markov.markov_build:system \
    --platform <name> --top markov_top [--composed] [--json]
```

## See also

- [Composite kernels](./composite.md) -- the same three-term decomposition for one kernel.
- [XSI system simulation](../build/xsi_system.md) -- the system DAG these steps belong to.
- `plans/system_resources.md` -- the plan, its stages and its open questions.
