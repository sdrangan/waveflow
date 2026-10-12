# Plan: the interconnect's timing and area in the platform model

> **Status (2026-10-11): stages 1-3 built on branch `xbar-profile`; stage 4 (resolve into pysim) and
> stage 5 (a resource model) not started.** Follows the mm_fir timing probe
> (`plans/mm_adaptor_host_endpoints.md`; commit "three pysim timing fixes").
> Revised 2026-10-11: the crossbar's **resources** are measured too (Vivado synthesis, place and route
> of the generated IP), the **connectivity mode** (SAMD crossbar vs SASD shared bus) is a config field,
> and the measurements are **records in the platform's module store**, not a new `interconnect.json`.

## Why

The pysim crossbar now has two measured numbers that make it match AMD's `axi_crossbar` at RTL:
`latency_init = 4` and `latency_travel = 2` (of those 4, the cycles a request spends reaching the
slave). Today each design sets them itself -- mm_fir has `XBAR_LATENCY, XBAR_TRAVEL = 4.0, 2.0` -- so
every design that uses a crossbar either copies the numbers or forgets them, and nothing re-measures
them when the crossbar's configuration changes.

They are a property of **an interconnect as built on a platform**: the crossbar IP, its configuration
(mode, register slices, connectivity), and the clock. They are not a property of the FPGA part as
such -- XSI simulates the configured IP the same way for any part -- but a platform's choices (a slower
part needing register slices, a different clock, SmartConnect instead of `axi_crossbar` in a board
design) change them.

The crossbar's **area** had the same problem, worse: nothing measured it at all. HLS cannot report it
(the crossbar is outside every kernel), and a crossbar's LUT/FF count is logic, not structure, so it
cannot be declared from geometry the way a memory's BRAM count can (`plans/rtl_module.md`, "Resource
accounting": structural blocks declare, logic blocks need a run). It needs a Vivado run of the
configured IP -- and unlike the cycle counts, **area and closure depend on the part's family and
speed grade**.

The master settings found by the same probe (`MMIFMaster.max_outstanding`, `issue_cycles`) are **not**
platform data: they describe the host driving a master, and stay with whatever models the host.

## The connectivity mode

`axi_crossbar` has two architectures, chosen by `CONFIG.CONNECTIVITY_MODE` (or the `STRATEGY` preset
that sets it):

| mode | `STRATEGY` preset | what it is | cost |
|---|---|---|---|
| **SAMD** -- shared address, multiple data | 2, "maximize performance" (also the default under 0, "custom") | a true crossbar: shared address arbiter, but independent data paths, so different masters' bursts to different slaves move at once; read and write channels independent; several outstanding transactions per slot | more area: a data path and mux per SI-MI pair, plus per-thread ID tracking |
| **SASD** -- shared address, shared data | 1, "minimize area" | a shared bus: one transaction at a time across the whole interconnect | least area; latency and contention behave like a bus |

Every crossbar generated before this plan is SAMD (`STRATEGY = 0`, `CONNECTIVITY_MODE = SAMD` in the
`.xci`). **Built:** `AxiXbarConfig.mode` (`"SAMD"` default / `"SASD"`), emitted as
`CONFIG.CONNECTIVITY_MODE`; it enters the cache digest only when not the default, so every existing
SAMD IP and XSI build keeps its key (pinned by `test_default_mode_keeps_existing_cache_keys`).

**Not built:** a pysim SASD model -- one crossbar-wide lock rather than one per slave, read and write
not overlapping (a `mode=` on `AXIMMCrossBarIF`).

## Where the measurements live -- decided

**In the platform's module record store** (`waveflow/calib/record_store.py`), not a new
`interconnect.json`. The store already has everything this needs:

- a content key per configuration -- `AxiXbarConfig.identity()` hashes the hardware fields (not the
  name, not the part), so two designs that build the same crossbar share one key and one synthesis;
- **fidelity tiers** on one key -- `vivado_synth` and `vivado_impl` resource records, `xsi` timing
  records -- with `ModuleStore.best` picking the strongest;
- provenance (part, period, tool version) checked on every read;
- the two-tier promote: sweeps write a work-tier platform (untracked), `waveflow_calib publish`
  promotes to the tracked library;
- `corpus_from_records(store, cls_name="AxiCrossbar")` -- the frame a resource model fits from.

A resource model for the crossbar (stage 5) therefore needs no new storage: it is a `ResourceModel`
whose corpus is `cls_name="AxiCrossbar"` and whose features are `hw_params()`.

## The design

1. **Measured by synthesis -- built.** `synth_axi_xbar(cfg, ip, out_dir, period_ns, impl=True)`
   synthesizes, places and routes the IP **out of context** (on a copy of the IP directory), reads the
   utilization after each (`waveflow/utils/vivado_report.py`: by row name, BRAM in 18K halves so it
   compares with `hls_estimate`), and prints the routed setup/hold slack, logic levels and critical
   path from TCL. Cached per configuration and period. `xbar_synth_records` turns it into a
   `vivado_synth` and a `vivado_impl` record; the routed one carries `wns_ns` / `met` in its payload.
   About 4 minutes per configuration.

   **OOC caveat:** the crossbar's ports have no partition pins, so routed slack covers its internal
   register-to-register paths; port-to-register paths are unconstrained. A system-level closure
   number needs the crossbar in its top.

2. **Latency measured at RTL -- built.** `render_xbar_bfm_top` (moved here from the Stage 0 witness
   test, which now uses it) plus `render_xbar_latency_tb`: host BFMs on every SI, memory BFMs on every
   MI, and a probe set (`xbar_latency_probes`) -- one/16-beat write/read alone; every master to its own
   slave at once (`par_*`); every master to one slave (`same_*`). Each read checks it gets back what
   its write stored. `xbar_timing_records` files one `xsi` timing record per probe,
   `{features: {op, beats, masters, slaves}, cycles}`.

3. **The census -- built.** `examples/xbar_profile`: a `run_dag_cli` build (ip -> synth -> latency ->
   records) and a `SweepRunner` sweep over 5 shapes (1x2, 2x1, 2x2, 3x3, 4x4) x 2 modes x 2 widths
   (32, 64), at 250 MHz, into the work-tier platform `examples/xbar_profile/calib/work/zu7ev_xbar_250mhz`.
   `xbar_profile_table` renders the tables from the store. Docs: `docs/examples/xbar_profile/`.

   **The part is a stand-in.** This machine's Vivado license cannot synthesize the RFSoC's `xczu48dr`
   (Enterprise device); it can the ZCU104's `xczu7ev-ffvc1156-2-e` -- same Zynq UltraScale+ fabric, same
   -2 grade. `create_ip` generates the crossbar's RTL from the family (`C_FAMILY = zynquplus`):
   measured, the synthesis wrapper, HDL library and simulation model are **byte-identical** for the
   two parts. So the latencies transfer exactly, and the area should to within placement noise; the
   closure margin is the one number to re-measure on the real part (`--part xczu48dr-ffvg1517-2-e`
   with a license that covers it). The records say which part they came from (provenance), and the
   platform is named for the part, so nothing passes for an RFSoC measurement.

4. **Resolved, not copied -- not started.** `AXIMMCrossBarIF` takes `latency_init` /
   `latency_travel` from the platform's `xsi` records for its configuration (`from_platform(...)`
   or a `platform=` argument), and mm_fir drops its constants for it. A design that sets the numbers
   explicitly still can; one that sets nothing keeps the old defaults. Mapping: `latency_init` from
   the `r1` probe; `latency_travel` needs a write-behind-read probe the set does not have yet.

5. **A crossbar resource model -- not started.** Fit LUT / FF on `n_si`, `n_mi`, `data_width`, per
   mode, from `corpus_from_records(..., cls_name="AxiCrossbar")`; a system's resource total adds the
   crossbar it instantiates (`render_system_top`'s designs: mm_fir, markov).

## Gates

- built: `tests/build/test_axi_xbar.py` (mode, digest compatibility, identity, probe sets, TB
  rendering, span parsing, utilization parsing, records) -- no toolchain;
- built: `test_axi_xbar_xsi.py -m xsi` unchanged numbers through the shared top renderer;
- built: the sweep files a `vivado_synth`, a `vivado_impl` and per-probe `xsi` records for every point,
  and a re-run files nothing new (identical records are not appended).  **Ran 2026-10-11: 20/20 points,
  176 records, ~75 min.**  Results in `docs/examples/xbar_profile/`: every point closes at 250 MHz;
  SASD 2.3-2.6x fewer LUTs at 4x4 but fully serial (`par_w16` 80 vs 30 cycles at 4x4); data width
  changes no latency; `r1` = 5 = pysim `latency_init` 4 + 1 word, as mm_fir found.  The sweep found a
  bug: SASD has no MI-side ID ports (fixed, `mi_signals`);
- to do: stage 4 -- mm_fir's pysim numbers (734 / 792) unchanged when its latencies come from the
  platform;
- to do: the RFSoC part itself, where licensed.

## Open

- Register slices (`R_REGISTER`, per-slot slices) and data FIFOs: crossbar options a slow part needs
  for timing; each is a config field and a separate measurement. **Not needed at 250 MHz for any
  measured shape** (worst routed slack +1.451 ns, 64-bit 4x4 SAMD) -- revisit above 4x4, or at a
  faster clock, or when a system's port-to-port paths (not timed OOC) fail.
- Publishing: the sweep's work-tier platform is `zu7ev_xbar_250mhz`; whether a stand-in measurement
  belongs in a tracked library at all, or only the real part's, is a decision for whoever publishes.
- SmartConnect, for board designs (`plans/board_packaging.md`): a second interconnect kind, measured
  the same way once a block design exists.
