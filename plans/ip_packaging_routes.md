# Plan: two routes from a system to a bitstream -- one global synthesis, or an IP per component

**Status:** discussion, 2026-10-12.  Nothing built.  Written after `plans/system_resources.md` (merged in
PR #250) measured markov's system top in Vivado, and before `plans/board_packaging.md` (scoped, not
started) reaches a multi-kernel system.  Its job is to put the two routes side by side, with what has
been measured and what has only been estimated, so the choice can be made on evidence.  The user's
stated preference is the per-IP route (B); the first stage below measures what it costs before anything
is committed to.

**Revised 2026-10-12:** route B's granularity is decided -- **one IP per device**, four for markov
(see [Granularity: one IP per device](#granularity-one-ip-per-device)) -- and the plan ends with the
markov docs comparing both routes on measured numbers.

**Revised 2026-10-11:** a [Docs](#docs) section places each new page with its stage, and lists five
existing build steps (and one index link) documented nowhere, to close independently.

**Paused after Stage 0 (2026-10-11).**  Stage 0 and Docs 0 are done.  Stages 1-4 wait on
`plans/system_model.md`.  A system today has no class stating its contract: `discover()` finds it by
searching for one crossbar and one `SwHost`, and the walk is crossbar-centric.  That plan gives the
system a `System` base, the cut as a per-build partition into DUT and TB, realization hooks for
interfaces, and a generic walk that builds `SystemTopSpec`.  Per-device synthesis and packaging then
build on that instead of on `discover()`.

## The question

A *system* -- several kernels behind memory-mapped adaptors, AMD's crossbar, an on-chip memory
(`examples/markov`, `examples/mm_fir`) -- is today one generated Verilog top (`system_top.py`).  XSI
simulates it, and `system_synth` synthesizes it in one Vivado run.  To reach the board, it must become
something a Vivado block design can hold.  There are two ways to do that, and they differ in what is an
IP and in how synthesis runs.

| | **Route A -- one IP per system** | **Route B -- one IP per component** |
|---|---|---|
| what is packaged | the generated system top, whole (kernels, leaves, crossbar inside) | each pysim object: each kernel, each adaptor, each routed channel, the memory; the crossbar is vendor IP already |
| what the block design holds | PS + SmartConnect + **one** system cell | PS + SmartConnect + the components, wired bus to bus; a composite is a hierarchical IP |
| who wires the components | `system_top.py` (Verilog, exists) | a block-design generator, from the same pysim graph (does not exist) |
| synthesis | one run over everything (Vivado's "global") | one out-of-context run per IP, then link (Vivado's default for a block design) |
| matches | `plans/board_packaging.md` §9.2 option (a), applied to a system | §9.2 option (b), generalized; no plan covers it yet |

**Packaging and synthesis mode are independent choices.**  A block design of packaged IPs can still be
synthesized globally (the block design's synthesis option), and a generated top can be synthesized per
instance (`ooc_link`, below).  The routes are named by their natural pairing, but each cost below is
labelled with which of the two choices causes it.

## What has been measured (markov, xc7z020, 10 ns, Vivado 2025.1)

From PR #250 (`plans/system_resources.md`, Results):

- **One global synthesis** of the system top: 116 s of `synth_design` (plus Vivado start-up); total
  **6,287 LUT / 7,187 FF / 4 BRAM18**.
- **Each leaf out of context**, `synth_design` only: `axi_slave_front` 34-37 s, `mm_bram_port` 34 s,
  `mm_queue_out` 36 s, `mm_queue_in` 48-49 s, `mm_credit_in` 54 s, `bram_t2p` (512 x 64, now 2 LUT +
  one block RAM) 58 s, `mm_sync_fifo` 60 s.  A small IP's synthesis time is **fixed overhead**, not
  design size: the 2-LUT memory costs as much as anything else.
- **The parts measured separately, summed** ("composed"): 11,938 LUT / 8,088 FF / 6 BRAM18 against the
  6,287 / 7,187 / 4 measured -- +90% LUT.  But the kernels in that sum are csynth *estimates*, not
  Vivado runs, so this is **not** the cost of route B; see the first open item.
- **Context effects that were measured:** the crossbar is 1,292 LUT out of context and 849 in markov's
  global synthesis; the memory needs two RAMB36 out of context (both ports live) and one in markov
  (port B tied idle).
- **`KEEP_HIERARCHY` on every instance** (a global synthesis that may not optimize across instances):
  7,829 LUT / 9,314 FF, **+25% LUT / +30% FF**.  This is an upper bound on what losing cross-boundary
  optimization can cost, not the cost of route B: per-IP synthesis plus link-time `opt_design` recovers
  part of it.

Not measured: export / packaging times, Vivado start-up per run, a real per-IP link, a block design.

## Route A -- one IP per system

**What it is.**  `ipx::package_project` over the system top's sources -- the top, the leaves it uses
(`SystemTopSpec.leaf_sources`), each kernel's csynth Verilog, the crossbar's `.xci` -- yielding one IP
whose boundary is the host's AXI4 port (`s0_axi`) and the interrupt lines.  The block design is
`board_packaging.md`'s S1-sized one: PS, SmartConnect, this cell.

**Advantages**

- **Exists, nearly.**  The generated top, its inventory and its synthesis are built and gated; what is
  missing is one packaging step and a block-design template (`board_packaging.md` §4.4: by hand, once).
- **Best area and timing.**  Vivado optimizes across every instance boundary: tied-off ports prune
  (markov's memory: one RAMB36, not two), unused outputs vanish, logic is shared.
- **Fastest cold build.**  One synthesis: ~2.5 min wall clock for markov.
- **What ships is what XSI simulated.**  One Verilog top serves both.
- **The resource number is already measured.**  `system_synth` is this route's synthesis.

**Costs**

- **The total is not the sum of the parts.**  Each instance's in-context number is a share of the
  design: Vivado moved a read mux between instances and dissolved two of them (priced since PR #250 by
  their cell names).  Prediction before synthesis therefore needs a model of the cross-boundary effects
  -- the open +90% gap.  *(Caused by global synthesis, not by packaging.)*
- **Every change re-synthesizes everything.**  A one-line edit to one kernel costs the whole system's
  synthesis.  Fine at markov's size, and it grows with the system.  *(Global synthesis.)*
- **No component on the board alone.**  The only packaged unit is the whole system; debugging one
  kernel on hardware means building a second design by hand.  *(Packaging granularity.)*
- **A system that grows past one designer's block design** has nowhere to go: the one cell is the
  whole design.

## Route B -- one IP per component

**What it is.**  Each pysim object becomes an IP: a kernel through `export_design -format ip_catalog`
(no `-flow`, `board_packaging.md` §5.6); hand-written leaves through `ipx::package_project`, grouped as
the inventory already groups them -- one IP per owner (an adaptor = its front + its views; a routed
channel = its writers + its FIFO; the memory = window + RAM); the crossbar as the vendor IP it already
is.  A composite (a kernel with its device) is a hierarchical IP of those.  A generator emits the block
design from the pysim graph, as `system_top.py` emits Verilog today.  For markov: about seven cells.  *(Revised: one IP per device instead -- four cells for markov; see
[Granularity: one IP per device](#granularity-one-ip-per-device).)*

**Advantages** (the user's three, checked)

- **The total is (close to) the sum of the parts.**  Each IP is synthesized alone, so its number is its
  own and reusable; the design total is the sum plus link-time cleanup (`opt_design` removing unloaded
  or constant logic) plus the interconnect cells the block design adds.  The composed estimate stops
  being an estimate.  *(Per-IP synthesis.)*
- **No top-level re-synthesis.**  An unchanged IP reuses its checkpoint (Vivado's IP cache, or this
  repo's out-of-context library, keyed by the same identity: sources + parameters).  A change
  re-synthesizes the IPs it touches.  Link, place and route still run over the whole design each time,
  and for a bitstream they are usually the larger share.  *(Per-IP synthesis.)*
- **Each component can go on the board alone**, in a small test block design -- if its ports are ones
  the board can drive (AXI-Lite from the PS, AXI-Stream through a DMA).  *(Packaging.)*
- **Design-space exploration pays per configuration, once.**  A sweep that touches one kernel
  synthesizes that kernel per point, not the system.

**Costs**

- **Lost cross-boundary optimization.**  Bounded above by the +25% LUT of `KEEP_HIERARCHY`; the real
  per-IP figure, with link-time `opt_design`, is unmeasured.  Estimate: a few hundred LUT on markov
  (~5%), plus whatever tie-offs fail to prune.  *(Per-IP synthesis.)*
- **Tie-offs must become parameters.**  Out of context, markov's memory cannot know port B is idle, so it
  takes two RAMB36.  The fix is to say so in the IP's parameters (`bram_t2p` with a `PORT_B_USED`
  generic), so each IP specializes inside its own synthesis -- which also puts the tie-off in its
  identity, the open question `system_resources.md` left.  Recovers pruning; does not recover
  cross-boundary sharing or LUT packing.
- **Every link must be a declared interface.**  Inside a Verilog top any wire works; between IPs each
  link needs a bus definition.  Streams map to AXI-Stream; the credit-stream pairs, BRAM ports,
  interrupt lines and constant inputs (a bus writer's `target`) need interface definitions or IP
  parameters.  Per interface *type*, once -- but real work.
- **A block-design generator.**  `board_packaging.md` §4.4 defers one deliberately ("build the BD once
  by hand") for its five single-kernel designs; a seven-cell system is past what is reasonable by hand
  and past what a template survives.  The generator walks the same graph `system_top_spec` walks.
- **Two realizations of one graph.**  XSI runs the generated Verilog top; the board runs the block
  design.  Both must come from one spec (as the inventory and the renderer do today), or they drift.
- **Cold-build time.**  Packaging itself is cheap (estimated: under a minute for a kernel's
  `export_design`; seconds per `ipx::package_project` within one Vivado session, ~20-40 s per session
  start).  The cost is **one out-of-context synthesis per IP**, each ~1 min of mostly fixed overhead:

  | markov, estimated from the leaf timings above | cold build | after a one-block change |
  |---|---|---|
  | global (route A) | ~2.5 min | ~2.5 min |
  | per-IP, sequential, ~15 runs (every leaf its own IP) | ~15-20 min | ~1-1.5 min |
  | per-IP, ~7 runs (one IP per pysim object) | ~8-10 min | ~1-1.5 min |
  | per-IP, 4 parallel jobs | ~3-5 min | ~1-1.5 min |
  | **per device, 4 IPs** (chosen; crossbar ~4 min once per configuration) | ~7-8 min sequential, ~4-5 parallel | ~1-1.5 min |

  Granularity is the main lever, because the overhead is per run.  *(Per-IP synthesis.)*  All rows are
  estimates; Stage 1 measures the chosen one.
- **Caches to keep out of git and invalidate correctly.**  Checkpoints and the IP cache are large,
  path-dependent and Vivado-version-specific (`board_packaging.md` §11 already bars `.runs/`, `.cache/`).
  Invalidation is solved by keying on identity, as the out-of-context library does.

## Granularity: one IP per device

**Decided (2026-10-12):** route B packages one IP per **device**, not one per pysim object.  A device
is what `build_mm_device` already assembles around a kernel, plus the bus masters that kernel needs:

- **a device IP** = the kernel (its csynth Verilog) + its memory-mapped views + its AXI front + **the
  bus writers that act for that kernel**;
- **the memory** = its window + the RAM, one IP;
- **the crossbar** = AMD's IP, configured, as it already is.

**Where a bus writer goes.**  A routed credit link (`MmCreditStreamIF`) has two writers, and each goes
in the device of the kernel it acts for -- not with the crossbar:

- the **forward writer** carries the sender's data into the receiver's queue: it is the *sender's* bus
  master, so it joins the sender's device, beside the sender's `CreditIn` view;
- the **credit writer** returns credit into the sender's `CreditIn`: it is the *receiver's* bus master,
  so it joins the receiver's device.

Bundling the writers with the crossbar was considered and rejected.  The crossbar is the most expensive
IP to synthesize (~4 min out of context) and the writers change whenever a link's parameters change
(width, credit window, depth) -- exactly the knobs a design-space search sweeps, so every link change
would pay the crossbar's synthesis.  And in a block design the interconnect may be Vivado's own
(SmartConnect), which cannot absorb custom logic.

**markov at this granularity: four IPs.**

| IP | contents | boundary |
|---|---|---|
| generator device | `MarkovGen` + `qcmd` (queue in) + `u_crd` (credit in) + the front + the forward writer | AXI-MM slave, AXI-MM master, interrupts |
| chain device | `MarkovChain` (its memory writer included) + `qu` (queue in) + `qresp` (queue out) + the front + the credit writer | AXI-MM slave, AXI-MM master(s), interrupts |
| memory | the window + `bram_t2p` | AXI-MM slave |
| crossbar | `axi_crossbar` | AXI-MM |

**Why this grouping.**

- **Every link between IPs is standard AXI-MM or an interrupt line.**  The kernel-to-view streams, the
  credit-stream pairs and the FIFOs -- the links this plan's Route B costs listed as needing custom
  interface definitions -- all end up *inside* a device.  For the memory-mapped wiring, no custom bus
  definitions are needed at all.
- **Tie-offs inside a device prune inside its own synthesis** (an unused view, an idle port).  The
  memory is still its own IP, so its port-B tie-off still needs Stage 2's parameter.
- **It is the unit everything else wants.**  The host sees a device through its AXI port, so a device's
  unit harness (verification and timing) drives it the same way; it is what goes on the board alone;
  and it is the natural unit for a sub-agent to own (`plans/multi_agent_flows.md`).  A sending
  kernel's whole side of a link -- its stream, the forward writer, its credit counter -- is in one IP
  and is tested end to end there.
- **Four fixed overheads instead of about seven**, and the crossbar's is paid once per configuration.

**What it gives up.**  Reuse below the device: two identical adaptors in two devices synthesize twice.
And a per-instance split inside a device (kernel vs adaptor) is no longer a separate synthesis -- it is
recovered within the device's run by the cell-name census `system_resources.md` already uses.

**Identity.**  A device IP's identity is the digest of everything in it: the kernel's csynth Verilog,
each leaf's source and parameters, each writer's parameters.  A kernel's csynth stays cached under the
kernel's own identity, so a change to a view's depth re-runs the device's out-of-context synthesis but
not the kernel's csynth.

**Open: the direct wiring.**  markov also runs with the two kernels joined directly (`link="direct"`): a
`CreditStreamIF` between them, no bus.  At device granularity that link crosses two IPs and needs a
credit-stream interface definition -- or the two kernels form one IP when joined directly.  Decide when
a direct-wired system is packaged; the memory-mapped wiring does not need it.

## Choosing per purpose, not once

Because packaging and synthesis mode are independent, a design packaged as route B can be synthesized
either way:

| purpose | packaging | synthesis |
|---|---|---|
| final bitstream | per component (B) | global -- best area and timing, fastest cold build |
| design-space exploration, incremental work | per component (B) | per IP -- each configuration paid once |
| debugging one component on the board | that component's IP | its own test block design |
| first light on the board, soonest | one per system (A) | global |

So the routes need not be exclusive: A is the shortest path to a bitstream for a system that exists
today; B is the structure that makes area predictable and components testable, and it can still be
synthesized globally for the shipped bitstream.

## Stages

Each stage is evidence for the next; stop at any rung if the numbers say so.

**Stage 0 -- devices from the inventory.**  A grouping derived, not hand-listed: from
`SystemTopSpec.instances()` and each instance's `owner`, map every RTL instance to its device, the
memory or the crossbar, by the rules in [Granularity](#granularity-one-ip-per-device) (a bus writer to
the kernel it acts for).  Then render each device as a **sub-top** of the system top, so route A's
global synthesis, XSI and route B's per-device synthesis all read the same device modules -- one
realization of the graph, not two.  Gates: markov's fifteen instances fall into the four groups, each
exactly once (and mm_fir's, both topologies); the system top re-rendered with device sub-tops runs
markov's XSI gate unchanged (1870 cycles, traces identical) and its global synthesis total moves by no
more than noise.

> **Done (2026-10-11, branch `ip-packaging-routes`).**  `system_top_spec` places every kernel, net and
> slot (`_group`); `SystemTopSpec.subs` lists the sub-tops, and `RtlInstance.sub` / `.path` place each
> instance.  `render_system_top` emits one module per sub-top (`<top>_<name>`, instance
> `u_<name>_sub`) and then the top.  A sub-top's ports are named for the nets they carry, so instance
> and net names are unchanged.  Probes on device nets are computed in the device; probes on SI channels
> are computed in the top.  Gates:
> - markov: 15 instances fall into `u_gen_sub` (6), `u_chain_sub` (5), `u_mem_sub` (3) and the top
>   (`u_xbar`), each exactly once; mm_fir (both adaptor shapes) is one device;
> - no stream net crosses a sub-top in either system (`tests/build/test_system_top.py`);
> - XSI: all 13 markov / mm_fir gates pass on the hierarchical tops (1870, 618, 611; traces identical);
> - global synthesis (xc7z020, 10 ns): **6,281 LUT / 7,187 FF / 4 BRAM18** against the flat top's
>   6,287 / 7,187 / 4 (−0.1% LUT).  `system_synth` now reads `-hierarchical_depth 2` and joins rows by
>   path.  `synth_design` took 173 s against 116 s, but the machine was loaded (the fast test suite ran
>   alongside), so that is not a measurement.

**Stage 1 -- measure the per-device cost, no packaging.**  An `ooc_link` mode for `system_synth`: each
device sub-top, the memory and the crossbar synthesized out of context (the leaf library already does
the leaves; add the kernels' csynth Verilog), the checkpoints linked into the system top,
`opt_design`, report.  Each out-of-context result files under its identity, so a repeat is a cache hit.
On markov, record, from the runs' own timing events (`waveflow.events`):

- area: the `ooc_link` total, the sum of the four parts, the link term, and the gap to global;
- wall clock: cold, sequential and parallel; after a change to one device (a view depth); after a change
  to one kernel's source (its csynth plus its device);
- per-IP synthesis times, and each kernel's out-of-context Vivado number -- which splits the open +90%
  into csynth's estimate error and context.

Gate: the `ooc_link` total equals the sum of its parts plus a link term that is measured, not assumed;
unchanged devices show no synthesis span in a one-device rebuild.

> **Built and measured (2026-10-12, branch `ip-packaging-s1`, on `system-model-s0`).**
> `waveflow/build/system_ooc.py::synth_ooc_link` runs three kinds of job:
> 1. **Each sub-top**, rendered alone (`system_model.md` S5a), out of context with its netlist kept
>    (`synth_rtl_ooc(checkpoint=True)`, cached by content).
> 2. **The top**: the crossbar and the glue, against `(* black_box *)` stubs.  It is cached by its
>    black-box text and the crossbar's configuration.
> 3. **The link**: `add_files` + `SCOPED_TO_CELLS` + `link_design`, then `opt_design`.
>    `read_checkpoint -cell` into the design `synth_design` left open is refused ("no structural
>    source files"), so the link is its own Vivado project.
>
> `SystemSynthStep` has a param `synth_route` (`global` | `per_device`), and markov's CLI has
> `--route`.  The per-device route writes the `OocLink` and files no store records yet.
>
> **Area** (markov, xc7z020, 10 ns, Vivado 2025.1):
>
> | | LUT | FF | BRAM18 |
> |---|---:|---:|---:|
> | gen device, OOC | 2,299 | 2,560 | 2 |
> | chain device, OOC | 3,028 | 3,934 | 0 |
> | memory, OOC | 103 | 39 | 2 |
> | top: crossbar + glue, OOC | 1,292 | 808 | 0 |
> | sum of parts | 6,722 | 7,341 | 4 |
> | link term (`link_design` + `opt_design`) | −472 | −242 | 0 |
> | **linked** | **6,250** | **7,099** | **4** |
> | global synthesis (route A) | 6,281 | 7,187 | 4 |
> | global + `opt_design` (like for like) | 6,228 | 7,091 | 4 |
>
> - **Losing cross-boundary optimization costs +22 LUT (+0.35%) and +8 FF (+0.1%)** against global +
>   `opt_design`.  The plan's estimate was "a few hundred LUT (~5%)", with `KEEP_HIERARCHY`'s +25% as
>   the upper bound.
> - **The memory's tie-off prunes inside its own sub-top.**  It is one RAMB36 out of context, because
>   port B's idle wires are in the memory's module.  So Stage 2's tie-off parameter is not needed at
>   device granularity, for the memory at least.  (The leaf-level OOC run needed two RAMB36 only
>   because the leaf alone had both ports live.)
> - **The link term is mostly the crossbar's.**  It is 1,292 LUT out of context and 849 in context
>   (PR #250), a 443 LUT difference, close to the −472 LUT link term.  The parts' own numbers barely
>   move when linked.
>
> **Wall clock** (sequential, one machine):
>
> | | seconds |
> |---|---:|
> | global synthesis + `opt_design` | 144 |
> | per device, cold | about 330: gen 110, chain 81, memory 50, top ~70, link ~30 |
> | per device, nothing changed | **31**: the link only, no part or top re-synthesized |
> | per device, one device changed (gen's `qcmd` queue 16 → 32 deep) | **103**: gen re-synthesized (53 s), chain, memory and top cached |
>
> Gates: the linked total is the parts plus a measured link term; a one-device change re-runs that
> device only (`part_cached` and `top_cached` in the result).  Not yet done from this stage's list:
> - parallel part runs (each part is independent, so it is a scheduler question);
> - the kernels' own OOC numbers, which split the old +90% composed estimate into csynth error and
>   context.  The old figure compared csynth *estimates* with Vivado; the per-device parts here are
>   all Vivado and land within 8% before linking;
> - records in the store for the per-device route.

**Stage 2 -- tie-offs as parameters.**  Add parameters for the tie-offs the inventory can see
(`bram_t2p` port B, unused crossbar slots), carried in each leaf's identity.  Gate: markov's memory is
one RAMB36 out of context; the composed BRAM equals the measured.

> **Built (2026-10-13, branch `ip-packaging-s4`) -- as an identity, not a Verilog parameter.**
> Stage 1 had already shown the device-granularity half: markov's memory sub-top is one RAMB36 out of
> context, its port B tied inside its own module.  What remained was the leaf library, where
> `bram_t2p` alone has both ports live and the composed estimate said 6 BRAM18 against 4 measured.
> Rather than a `PORT_B_USED` generic in `bram_t2p.v` -- a source edit that would move the identity
> and the RTL of ten examples whose port B is live -- the **instance** states its tie-offs:
> - `RtlInstance.ties` (`((port, value), ...)`: an input tied to a value, an output left open), set
>   by `SystemTopSpec.instances()` from what the top renders (`BramView.IDLE_TIES` for a memory no
>   kernel holds);
> - `identify_rtl(..., ties)` keys on them **only when there are any**, so every untied leaf keys
>   exactly as before -- no record orphaned, no XSI top changed;
> - `synth_rtl_ooc(..., ties)` synthesizes the leaf inside a generated `<module>_tied` wrapper
>   (`rtl_ooc.tied_wrapper`, port bounds evaluated from the leaf's own header) that ties the ports as
>   the system does; `measure_instances` passes `RtlInstance.ties`.
>
> Measured (xc7z020, 10 ns): `bram_t2p` 512 x 64 tied **1 LUT / 2 BRAM18** (one RAMB36), untied 2 LUT /
> 4 BRAM18, 35 s each.  markov's composed estimate: **11,937 LUT / 8,088 FF / 4 BRAM18** against
> 6,281 / 7,187 / 4 measured -- **BRAM exact** (was 6 / 4).  The LUT gap (+90%) is the kernels'
> `hls_estimate`, untouched here.  Gates: `tests/build/test_leaf_ties.py` (no Vivado: only
> `u_mem_mem` is tied, untied keys unchanged, the wrapper) and `tests/examples/test_markov_tieoffs.py`
> (`-m xsi`: one RAMB36 out of context; composed BRAM == measured).  Unused crossbar slots, the
> plan's other tie-off, need nothing: a stub slot's front is not instantiated, and the crossbar's
> configuration already carries the slot count.

**Stage 3 -- package.**  `ipx::package_project` per device sub-top (its kernel's csynth Verilog and its
leaves' sources), the memory as one IP, the crossbar's `.xci` as is.  At device granularity the
memory-mapped wiring needs no custom interface definitions (every inter-IP link is AXI-MM or an
interrupt); the direct wiring's credit stream is the open case.  Gate: rung 3 of `board_packaging.md`
(VLNV and ports) for each of markov's four IPs.

> **Built (2026-10-12).**  `waveflow/build/system_ip.py` (`package_system`) and the opt-in DAG step
> `ip_package` (`IpPackageStep`, on every system's DAG via `add_system_steps`):
> - each device sub-top and the memory packaged by `ipx::package_project` from exactly what it
>   compiles (its rendered module, leaves, kernels' csynth Verilog; imported, so self-contained),
>   all in **one** Vivado session -- markov's three in **11 s** of Vivado, 19 s for the step;
> - interfaces: the AXI4 groups inferred from their names (`mi<k>_axi` slave, `si<k>_axi` master),
>   clock and reset inferred, interrupt lines declared from `SystemTopSpec.irqs`; the clock then
>   associated with every AXI interface and the reset (`ASSOCIATED_BUSIF` / `ASSOCIATED_RESET`),
>   what Stage 4's validation needs;
> - the crossbar recorded by its `.xci` (`xilinx.com:ip:axi_crossbar:2.1`), generated for the part;
> - each IP cached by the content of what it reads plus the recipe's revision: a re-run starts no
>   Vivado; the manifest is read back from each `component.xml`.
>
> Gate (`tests/examples/test_markov_ip.py`, `-m xsi`, `WANT_XSI_GATES` 177 -> 183): markov's four
> VLNVs; exactly the expected interfaces and modes; no port outside an interface; the clock
> association; the cache; and **each IP's files byte-identical to what the XSI gates compiled**, its
> own module the text the gated system top holds.  As predicted, every inter-IP link is AXI4 or an
> interrupt: no custom interface definition was needed.  Docs: `docs/guide/build/ipi.md` (Stage 3's
> half; Stage 4 adds the block design).  Not done: `export_design` per kernel (the plan's route B
> text) -- the kernel's csynth Verilog goes into its device's IP directly, so a kernel is never an IP
> on its own; the direct-wired markov (a credit stream between devices) is still the open case.

**Stage 4 -- the block-design generator.**  From the system spec, `create_bd_cell` /
`connect_bd_intf_net` for each IP and link; `validate_bd_design` (rung 4) as the gate.  The XSI top and
the block design rendered from one spec.

> **Built (2026-10-13, branch `ip-packaging-s4`).**  `waveflow/build/system_bd.py`
> (`build_block_design`) and the opt-in DAG step `block_design` (`BlockDesignStep`, after
> `ip_package`):
> - **links found by name**, by the rule the system top already follows (a sub-top's port is named
>   for its net): `si<k>_axi` meets crossbar `S0k_AXI`, `mi<k>_axi` meets `M0k_AXI`, two IPs with
>   one name meet each other (the direct-link case, untested: no system has one yet), any other name
>   becomes a design port (`bd_links`, testable without Vivado);
> - the crossbar cell configured from `AxiXbarConfig.config_props()` -- now the one source for both
>   `create_ip` and the block design;
> - **the address map assigned explicitly**: every master space (the host port and each bus writer)
>   maps each slave at its crossbar window.  `assign_bd_address` alone gave markov's three slaves
>   1 GB windows at 0, 1 G and 2 G -- a block design that validates and decodes nothing like the
>   verified crossbar;
> - validated, then **read back**: interface nets, nets, segments, crossbar config, wrapper ports,
>   critical warnings (`block_design.json`); `write_bd_tcl` exports `<top>_bd.tcl`; cached by inputs.
>
> markov: **13 s** of Vivado; 0 critical warnings.  Gate `tests/examples/test_markov_bd.py`
> (`-m xsi`, `WANT_XSI_GATES` 183 -> 189): validates; the wrapper's 41 ports equal the system top's
> (names case-folded, directions, widths); the interface nets are the system top's seven links and
> the clock, reset and interrupts are wired as there; all four masters see all three slaves at the
> crossbar windows; the crossbar cell's config after validation equals the XSI crossbar's; the
> cache.  Also fixed here: Stage 3's cache test called `package_system` without the crossbar and
> overwrote the DAG's manifest with `"xbar": null`; it now re-runs the step.  Not done: simulating
> the block design's wrapper under the XSI testbench (the IPs are byte-identical to what XSI ran and
> the wiring is read back, so the remaining risk is small; a lower-case port shim would be needed).

**Stage 5 -- choose the default.**  With stages 1-4 measured, decide which synthesis mode a system's
bitstream uses by default, and record why.

> **Decided (2026-10-13): global synthesis is the default for a bitstream; per device is the mode
> for incremental work.**  The evidence is Stage 1's and Stage 6's runs: per device costs 0.35% LUT
> against global + `opt_design` on markov, so area does not decide; time does, and the two modes win
> on different rebuilds.  A bitstream build is usually cold (a new part, a new board, a release),
> where global is one run against five, each with Vivado's fixed overhead.  A design-space loop
> changes one device per point, where per device re-synthesizes that device only.  So:
> - `system_synth`'s `synth_route` stays `global` by default; `per_device` is opt-in;
> - the block design is generated with `synth_checkpoint_mode None` (global), not Vivado's per-IP
>   default -- `build_block_design(synth=...)`, the DAG param `bd_synth`, read back and gated
>   (`test_a_bitstream_build_will_synthesize_it_globally`);
> - packaging stays per device in both cases (Stage 3): it is what makes a device testable on its
>   own, and costs nothing when synthesized globally.
> Docs: `docs/guide/flows/bitstream_ipi.md`, un-hidden and rewritten as "Toward the board".

**Stage 6 -- the markov docs: both routes, measured.**  `docs/examples/markov/synth.md` already shows
route A (csynth of the four tops, the crossbar and the top, the system's resources).  Add the
comparison, on markov's own numbers from Stages 1-2:

- the four-IP grouping, as a table and a figure, and why each bus writer sits where it does;
- resources: global (A) against per-device (B) -- the sum of parts, the link term, the gap, per
  resource -- and what the gap is (cross-boundary optimization and tie-offs, named);
- synthesis time: cold, and after a one-device change, for both routes;
- what each route is for (the [purpose table](#choosing-per-purpose-not-once)).

Conditions, so the page stays true:

- **reproducible:** both routes run from `markov_build.py` (a `--route global|per_device` option on the
  synthesis steps), and every number on the page comes from a run's timing events and synthesis
  records, never typed in;
- **the part named:** the numbers are for the stand-in part (xc7z020), not the board's; say so;
- **one page, not a new chapter:** a section of `synth.md` (or one sibling page), linked from
  markov's build page, so markov's reading order is unchanged.

Gate: the page's numbers regenerate from the build (a docs figure step, as markov's figures do), and
the docs snippet harness passes.

> **Built (2026-10-13, branch `ip-packaging-s4`).**  `examples/markov/markov_routes.py`:
> `MarkovRoutesStep` (`markov_routes`, opt-in) runs both routes -- `synth_system(..., opt=True)`
> (new: `opt_design` after the synthesis, `SystemSynth.opt_total`, the like-for-like number the
> plan's Stage 1 measured by hand) and `synth_ooc_link` -- each cached by content, and writes
> `results/routes.json` (counters from the reports, seconds from Vivado's own run timers) and
> `results/routes.svg`; `SyncRoutesFigureStep` (`sync_routes_figure`) promotes both to
> `docs/examples/markov/images/`.  The page is `docs/examples/markov/synth.md#routes`: the four-part
> grouping and where each bus writer sits, the area table, the time table, the figure, the part
> named (xc7z020 stand-in).
>
> Reproduced exactly: global + `opt_design` **6,228 / 7,091 / 4**, per device linked **6,250 / 7,099 /
> 4** (+22 LUT, +0.35%), parts and link term as Stage 1.  Seconds (run timers, start-up excluded):
> global 132; per device cold 226 (parts 54 + 60 + 33, top 55, link 24); one device changed 56-84;
> unchanged 24 (the link).  The link's seconds move run to run (it re-runs every call), so the page
> quotes the **committed record**, and two gates hold it: `tests/docs/test_markov_routes_page.py`
> (no toolchain: every cell of the page's tables, and the flows page's, is the record's) and
> `tests/examples/test_markov_routes.py` (`-m xsi`: a fresh run's area equals the record's; parts
> plus link term equal the linked total; gap under 1%).  `kb` finds the page for "IP packaging",
> "block design" and "out of context".

## Docs

The guide splits this route the way it already splits the XSI one: the concept under `flows/`, the
mechanics under `build/`, the numbers under `resource/`, markov's measurements under its example.  Each
page lands with the stage that builds what it describes -- never ahead of it.

**Docs 0 -- close the existing gaps (independent; can land before Stage 0).**  An audit of the 48
`*Step` classes in `waveflow/` (2026-10-11) found 31 documented by class name and the ten in
`system_dag.py` documented by DAG step name in `build/xsi_system.md` (right: users reach them through
`add_system_steps`).  Five are documented nowhere, and the build index misses one section:

- **`SvSimStep`** (`waveflow/build/svsim_steps.py`) -- the SystemVerilog-testbench rung (xvlog / xelab
  / xsim on a hand-written SV testbench, no HLS), named nowhere in `docs/`.  No example uses it (only
  `tests/build/test_svsim_steps.py`), so a section of `build/xsi.md` (it shares the terms defined
  there), not a page or an RTL-rung row.
- **`GenWrapperStep`** (`waveflow/build/rtl_steps.py`) -- the other half of `GenRtlStep`, the wrapper
  joining the kernel to its hand-written RTL; used by `bram_access` and five RF examples.  A section of
  `comp_codegen/rtl_module.md`, beside `GenRtlStep`.  *(The audit first credited these six examples
  to `SvSimStep`, from a combined search.)*
- **`MemLockStep`, `RfSampBufStep`, `RfTxStreamStep`** (`waveflow/build/streamutils.py`) -- header
  codegen steps; the RF guide documents the modules, not the steps.  Rows in `build/codegen.md`'s step
  list beside `MemMgrStep`, linked from the RF pages that use them.
- **The resource steps are unreachable from the build index.**  `SystemSynthStep`, `LeafResourcesStep`,
  `KernelResourcesStep`, `ResourceReportStep` are documented in `resource/systems.md`, but
  `build/index.md`'s topic list never links it.  Add the link (Stage 1's `ooc_link` needs it too).

Gate: every public `*Step` class is named in `docs/` -- a test that greps the class list against the
docs (system-DAG inner steps exempt by their DAG names), so the next new step cannot go undocumented.

**Docs for the new route, by stage:**

| stage | page | what |
|---|---|---|
| 0 | `build/xsi_system.md`, its `system_rtl` section | the device sub-tops: how instances group into devices (a bus writer to the kernel it acts for), and that XSI, global synthesis and per-device synthesis read the same modules |
| 1 | `resource/systems.md`, a new section beside "The system synthesis" | `system_synth`'s `ooc_link` mode: per-device out-of-context runs, the link, the link term, the cache by identity; linked from `build/index.md` |
| 2 | `resource/systems.md`, same section | tie-offs as leaf parameters, and that they enter the leaf's identity |
| 3-4 | **new `build/ipi.md`** ("IP packaging and block design"), a peer of `xsi_system.md` | the packaging steps (`export_design` per kernel, `ipx::package_project` per device and memory, the crossbar's `.xci`), the block-design generator, `validate_bd_design` and the VLNV / port gates, the steps an `add_*_steps` helper puts on the DAG; a fourth row in `build/index.md`'s RTL-rung table (system -> bitstream), marked a realization, not a verification rung |
| 4-5 | `flows/bitstream_ipi.md` -- **un-hidden and rewritten** (now a `nav_exclude` stub that presumes route B) | the concept: the two routes; packaging and synthesis mode as independent choices; the device as the unit; the [purpose table](#choosing-per-purpose-not-once).  Framed as what follows either simulation flow, not a third flow; `flows/index.md`'s "A third path" paragraph points to it.  Links down to `build/ipi.md` and `resource/systems.md` for mechanics and numbers |
| 6 | `docs/examples/markov/synth.md` | markov's measured comparison (Stage 6 above) |

Gate per page: the docs snippet harness passes, and `waveflow kb` finds the page for "IP packaging",
"block design" and "out of context".

## Open

- **The composed-vs-measured gap** (+90% LUT on markov): Stage 1 separates its two causes.
- **Granularity** -- decided: one IP per device
  ([above](#granularity-one-ip-per-device)).  Still open within it: the direct wiring's credit stream
  between devices.
- **The part.**  Everything above is on xc7z020 (what the kernels are csynth'd for); the board is the
  RFSoC 4x2 (`xczu48dr`), which this machine's Vivado cannot synthesize.  `board_packaging.md` S0's
  `Board` should supply the part to both plans; measurements use a stand-in where the license requires
  one (`reference-vivado-license-no-rfsoc-synth`).
- **Post-route numbers.**  `board_packaging.md`'s bitstream rung produces a routed design; running
  `system_synth`'s per-instance attribution on it would file post-route records at no extra synthesis.

## Relationship to other plans

- `plans/system_resources.md` -- the inventory, the out-of-context library, `system_synth`: everything
  Stage 1 builds on, and the measurements quoted here.
- `plans/board_packaging.md` -- the board flow for single kernels.  Its §9.2 is this plan's question in
  miniature (option (a) = route A, option (b) = route B); its §4.4 deferral of a block-design generator
  is what Stage 4 revisits for systems.  Its §9.4 claimed `bram_t2p.v` infers block RAM; that was
  false until PR #250.
- `plans/rfsoc_4x2_bringup.md` -- the board and the clean-clone acceptance test any bitstream must pass.
- `plans/incremental_xsi.md` -- the device sub-tops of Stage 0 must keep XSI's content stamps valid; a
  device's unit harness runs on an `XsiSnapshot` of its sub-top.
- `plans/multi_agent_flows.md` -- the device as the unit a sub-agent owns: its harness, its IP, its
  calibration.
