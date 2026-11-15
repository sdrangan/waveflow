# Plan — board packaging: an example becomes IP, a bitstream, and a PynQ host

**Status: S0 core and S1/S2 application paths implemented; S1 notebook board run
reported passed, receipt review and CMD/S2 acceptance pending. Updated 2026-11-15.** This plan tracks the template-driven
RFSoC4x2 board packaging extension. The implemented APIs, source map and manual
commands are in [Board packaging development](../docs/guide/flows/board_packaging.md).

**Who this is written for.** Someone other than its author, with a board and a Vitis/Vivado
install, working **one stage at a time**. Each stage below is one PR (many commits), lands green,
and is independently useful. A stage that cannot be finished should be *cut at a rung boundary*
(§3) rather than left half-wired.

**Target board:** RFSoC 4x2 (RealDigital), `xczu48dr-ffvg1517-2-e`. **Host:** PYNQ.

---

## 0. Progress and next actions

The minimal implementation was migrated onto upstream `main` at
`4bb72aa9d2f90d6851aa9c4ee85ccdb588ff13c9`. Both upstream and fork `main` still
resolve to that commit when checked on 2026-11-15. The three migration commits
(`1e0376ce`, `8ba53fd9`, `c3773dde`) are published on the fork's
[`board_packaging` branch](https://github.com/Ruibin2000/waveflow/tree/board_packaging).
The progress and developer documentation are follow-up changes on that branch.
The runtime output is now reduced to BIT, HWH, `driver.py` and one schema-2
`manifest.json`. XSA, reports and rebuild inputs remain in the build workspace;
intermediate validation gates and receipts are retained. The portable hosts
also accept existing schema-1 overlays.
The source audit against that base found 2,837 upstream files identical in
content and Git mode, 13 existing files changed for board integration or this
documentation, 72 added board-related files, and no deleted upstream files.

| Work | Implemented and checked | Remaining acceptance |
| --- | --- | --- |
| S0: target and clocks | `waveflow.board`, vendored RealDigital resources, optional board-aware `BuildConfig`, explicit clock resolution and target mismatch checks | Fresh tool/clock probes and applicable XSI gates |
| S0: packaging pipeline | Export receipts, header/XML control ABI, generated host driver, Tcl contracts, BD validation, implementation and overlay collection; offline conformance/failure tests | Fresh Vitis export and Vivado validation/implementation |
| S1: SimpFun | Current upstream model/codegen reused; fresh notebook tool build checked and RFSoC4x2 run reported passed | Return notebook acceptance JSON and review hashes; independently build and test the CMD path |
| S2: PolyAccel | Current upstream in-band coefficient protocol, PS DDR/simple DMA template, portable host and manual build notebook | Rebuild overlay and rerun all three cases on RFSoC4x2 |
| S3: direct AXI master to DDR | Future stage | Design, implementation and all acceptance gates |
| S4: mixed HLS/RTL IP | Future stage | Packaging route, implementation and all acceptance gates |
| S5: RF converter path | Future stage | RFDC topology/clocks, implementation and instrument/loopback acceptance |

After output simplification, the core/application/configuration suite passed
**390 offline tests**, with **23 tool-dependent cases deselected**. A further
**29 notebook, documentation and MCP corpus checks passed**, including
developer-guide snippet execution. Notebook defaults were checked in an isolated
checkout so the user's saved S1 executions and manual switches remained intact.
These exercise configuration, export/ABI/driver contracts, template generation,
portable host protocols and manual notebooks.
They do not establish Vitis, Vivado, XSI or physical-board acceptance. Broader
build/toolchain checks also encountered the upstream workspace-census test
expecting three XSI workspaces while the base contains two; it was reproduced
on the unmodified base and remains outside this extension.

S1 and both S2 build paths previously passed user-run board tests **before the
migration**. Those runs are historical context only. Their reports, overlays and
acceptance records were not migrated, and their success does not accept the new
branch. The old untracked board build directories have now been removed. The
separate migration clone, note and backups are retained outside the repository;
future verification starts from the committed sources and fresh outputs.

Since that cleanup, the S1 notebook was rebuilt manually at source commit
`5a788856`, producing `run-vzfkcj1w/overlays/overlay-mp59jnfe`. Its C/RTL checks,
export/driver ABI, BD and implementation receipts were checked locally. The
clock readback is 99.999985 MHz, WNS is 4.728 ns, hold slack is 0.022 ns and
severe DRC count is zero. The compact overlay has four delivery files.

On 2026-11-15, the user reported a successful RFSoC4x2 run of this notebook
overlay: `passed=True`, outputs `[0, 11, 17]`, and
`board_execution_verified=True`. The board saved
`test_s1_notebook_vzfkcj1w/board-acceptance-20261115T164059914431Z.json`.
That JSON has not yet been returned to the workstation, so matching its recorded
manifest, bitstream and ABI hashes against the local overlay remains pending.
The independently generated CMD overlay has not yet been accepted.

The next actions are sequential and remain **manual**:

1. Return the S1 notebook acceptance JSON and compare its manifest, bitstream
   and ABI hashes with `run-vzfkcj1w/overlays/overlay-mp59jnfe`.
2. Independently build the S1 CMD overlay using the environment/CMD instructions
   at the end of [the S1 notebook](../examples/regmap/board/build_simp_fun.ipynb).
   Check HLS/export/ABI, BD validation, target/clock, timing and DRC gates.
3. Upload that CMD overlay and the current portable host to RFSoC4x2. Run all
   three S1 cases and return its separate acceptance JSON.
4. Build and test S2 via [its notebook](../examples/stream_inband/board/build_poly.ipynb)
   and independently via CMD. Check `bd_validate` before implementation, then
   run cubic/identity/constant cases on RFSoC4x2 with each new overlay.
5. Match all new acceptance records to their overlay hashes. Keep generated
   overlays, reports and acceptance JSON outside tracked source control.
6. Mark S1/S2 accepted after reviewing the required records. Then start S3; keep
   generic multi-IP/system packaging and RF work as separate follow-up scope.

---

## 1. What this adds, and what "done" means

This extension carries application examples beyond their model, code-generation
and RTL checks into a PS-connected board design:

> generated kernel → **Vitis IP** → **Vivado block design** → **bitstream** → **PynQ host code that
> drives it on the 4x2**

"Done" for a stage is not "it built". It is: **a clean clone on a different machine rebuilds the
bitstream from committed sources, and the committed host script drives the board to the same answer
the Python simulation gives.** That acceptance test is stated in `plans/rfsoc_4x2_bringup.md` and is
not optional — export scripts essentially always carry one absolute path or one missing board file,
and that only surfaces on a clean clone.

---

## 2. Current implementation and integration boundaries

**The existing DAG and CLI host the extension.** Optional steps in
`waveflow/build/board_steps.py` use the upstream `BuildDag`, artifact handling and
`run_dag_cli`. The canonical SimpFun and PolyAccel models, handwritten arithmetic
and upstream build entry points are unchanged by the migration.

**Board and calibration Platform have separate jobs.** `waveflow/board/spec.py`
defines the physical target, versioned vendor resources and named clock profiles.
Board-aware `BuildConfig` checks target consistency before resolving the optional
measurement platform. Builds without a board retain upstream target selection.

**Export is an explicit, verified step.** `waveflow/build/ip_export.py` creates a
fresh board-targeted HLS solution and binds its synthesis inputs, RTL, reports
and exported IP to receipts. It refuses mismatched targets and changed inputs.
The generic csynth Tcl emitter does not export every upstream build implicitly.

**The hardware ABI comes from the export.** `waveflow/build/hls_control.py`
cross-checks the actual header and IP-XACT against register intent. The generated
driver uses that verified ABI; model-only layouts are marked as unverified.
Array and pointer fixtures exercise conformance without changing canonical models.

**Vivado and the application template have explicit contracts.** `run_vivado`
provides version-checked batch execution. `waveflow/build/vivado_board.py` renders
the project and template, checks connections/properties/addresses/clocks, invokes
`validate_bd_design`, and checks implementation timing/DRC before collection.
The legacy `waveflow/scripts/collect_overlay.py` remains unchanged.

**Two board application paths are supplied.** S1 uses PS AXI-Lite. S2 uses PS DDR
through simple DMA and one 100 MHz clock; coefficients travel in-band. Each has
an application-owned Tcl template, portable model/PYNQ host and manual notebook.
The generic upstream `SystemTopSpec` device/system packaging path remains a
separate flow; this extension does not infer arbitrary PS topologies from it.

---

## 3. The rung ladder — the gates, per example

The reason to name these is **loop time**. Rungs 1–3 run in seconds to minutes; rung 5 is tens of
minutes; rung 6 needs the lab. A student who only has rungs 5–6 debugs a block design at 40 minutes
a try.

| # | Rung | Needs | Marker | Typical time |
|---|---|---|---|---|
| 1 | Generated TCL matches its committed golden | nothing | *(none)* | seconds |
| 2 | Generated host driver matches the regmap | nothing | *(none)* | seconds |
| 3 | `export_design` produces an IP with the expected VLNV and ports | Vitis | `vitis` | minutes |
| 4 | **`validate_bd_design` — block design builds and wires, no synthesis** | Vivado | `vivado` | ~1 min |
| 5 | Bitstream + `.hwh` written | Vivado | `vivado` | 20–40 min |
| 6 | Board run matches the pysim answer | the 4x2 | `board` | manual |

**Rung 4 is the one that earns its keep and the one people skip.** It catches every wiring, VLNV,
address-map and clock-domain error in about a minute, without synthesis. Do not let a stage reach
rung 5 before rung 4 is a test.

The extension registers two pytest markers in `pyproject.toml` beside `vitis` / `xsi`:

```
"vivado: marks tests as requiring a Vivado installation (block design / bitstream; slow)",
"board: marks tests as requiring a physical RFSoC 4x2 (manual, opt-in, never in CI)",
```

`board` tests are **opt-in only** — they must not run under a bare `pytest`, and they must fail
loudly rather than skip silently when the board is expected (the `xsi` staleness lesson: a gate that
skips quietly is a gate that is not there; see [PR #177](https://github.com/sdrangan/waveflow/pull/177)).

---

## 4. Decisions already taken (do not relitigate without a reason)

**4.1 Artifacts: commit sources and contracts; retain generated overlays per run.**
For the minimal migration, BIT/HWH/XSA, generated drivers, manifests, tool reports
and acceptance records stay in ignored build directories. No historical overlay
or experiment result is committed. A collected overlay binds its files, exported
ABI, target and tool receipts by hashes. Release publication is future work and
has not been performed.

**4.2 The PYNQ driver is generated from the verified exported ABI.** `VitisRegMap`
expresses design intent; the actual header/XML must agree before driver generation.
A hand-written script per example would
be faster to the first demo and would drift five ways, with offsets hand-copied. Generating the
driver from the same regmap object the kernel is built from is what makes this a *feature* rather
than five one-offs — and it forces the conformance gate in §6.3, which is a debt this repo already
owes. The notebook on top stays hand-written.

**4.3 Every example's host code runs off-board.** Each example ships **one** host script that runs
against either a real PYNQ `Overlay` or a pysim-backed backend (§12). The student is not blocked on
lab access, the host layer is CI-checkable on Windows, and the sim/hardware difference becomes
*measurable* — which is the argument `plans/rf_lab_platform.md` already makes for `RfLab(sim=True)`,
applied one layer down.

**4.4 Do NOT start by writing a block-design generator.** A general "boundary ports → BD" generator
is a research project and the wrong first move. For these five designs the BD is small. The
prescribed route, from `plans/rfsoc_4x2_bringup.md`, is:

> build the BD **once by hand** in the GUI → `write_bd_tcl -force -include_layout` → de-absolutize
> the IP-repo path → commit that TCL as a **template** with a small parameter header.

`BdGenStep` then *renders* the template (IP repo path, VLNV, clock, address map) rather than
inventing it. Generalizing five committed templates into a generator is future work (§14) and must
not be attempted before all five exist — there is nothing to generalize from until then.

---

## 5. Stage S0 — the board becomes a first-class object

**Progress:** core implemented and covered by offline tests. Fresh Vitis/Vivado
and applicable XSI acceptance remain pending for the migrated branch.

**No example. Framework only. Everything else depends on it.**

**Why first:** the board target and clock must be explicit and consistent with
the calibration platform and exported IP.

**5.1 Implemented `waveflow/board/` subpackage.** A `Board` dataclass — a *build target*, distinct from
`Platform`, which is a *directory of measurements*:

```python
from waveflow.board import Board

board = Board.rfsoc4x2()
assert board.part == "xczu48dr-ffvg1517-2-e"
assert board.board_files_version == "1.0"
assert board.clock("ps_control").physical_hz == 100e6
assert board.clock("ps_control").hls_target_hz == 100e6
```

`Board.rfsoc4x2()` supplies the bundled target, vendor resources, tool release and
clock profiles. Existing non-board synthesis defaults are preserved. Clock
resolution is explicit and does not run during `BuildConfig` construction.

**5.2 The board is upstream of the calibration platform.** `Platform.resolve()` already
create-or-confirms `part` / `clk_freq`; a build that selects a `Board` feeds those in, so a
calibration platform measured on a different part is *refused* rather than silently reused. A
resource or timing fit is only valid for the device it was measured on, and this is where that is
enforced.

**5.3 Refuse to package IP built for another part.** `ExportIpStep` (§5.5) fails when the solution's
part is not the board's. This is the single guard that makes S0 load-bearing instead of decorative.

**5.4 `toolchain.run_vivado(tcl, work_dir, *, capture_output, env)`** — mirroring `run_vitis_hls`,
built on the existing `find_vivado_path()`. Batch mode, non-interactive, journal/log written into the
step's work dir, a `WAVEFLOW_ERROR:`-style sentinel convention so a step can tell a real failure from
a noisy-but-successful run.

**5.5 Implemented `waveflow/build/board_steps.py`** — the steps every stage draws from:

| Step | Produces | Rung |
|---|---|---|
| `ExportIpStep` | the exported IP dir/zip + its pinned VLNV | 3 |
| `HlsControlSpecStep` | header/XML ABI checked against register intent | 2/3 |
| `HostDriverGenStep` | standalone `driver.py` and ABI/driver manifests | 2 |
| `BdGenStep` | `create_bd.tcl`, rendered into the build directory | 1 |
| `BdValidateStep` | a `validate_bd_design` verdict, **no synthesis** | 4 |
| `BitstreamStep` | `.bit` + `.hwh` | 5 |
| `CollectOverlayStep` | isolated overlay directory + `manifest.json` | 5 |

**5.6 `export_design` keeps its own solution.** Emit `export_design -format ip_catalog` with a
pinned `-ipname` / `-version`, and **no `-flow`** — `-flow syn|impl` runs Vivado underneath and turns
a minutes-long step into an hours-long one. Two standing traps here:

- `csynth_design` writes `solution1/syn/verilog/`; `impl/` is *left over from a prior
  `export_design`* and goes stale. Anything reading generated RTL must read `syn/verilog/`. Once
  export is in the DAG, `impl/` will exist on more machines than before, so this trap gets easier to
  fall into, not harder.
- The XSI gates read RTL from `syn/verilog/` and assert exact cycle counts, and the staleness guard
  hashes sources. Adding an export step must not perturb them: after S0, run `pytest -m xsi -rs`,
  read the **skip count**, and confirm `WANT_XSI_GATES` is unchanged.

**Done when:** a `Board` exists, `run_vivado` runs a trivial TCL, `pytest -m xsi -rs` is unchanged,
and `ExportIpStep` refuses a part mismatch in a unit test.

**Rough effort:** 2–3 days.

---

## 6. Stage S1 — `examples/regmap`: AXI-Lite only

**Progress:** source-to-overlay DAG, template, portable host and manual notebook
implemented; offline gates and the fresh notebook tool build passed. The user
reported a passing notebook-overlay board run with outputs `0`, `11`, `17`.
Review of the returned notebook acceptance JSON and independent CMD build/board
acceptance remain pending.

The smallest possible closed loop, and the stage that carries most of the framework. Budget
accordingly: S1 is not 1/5 of the work.

**The design.** `SimpFun` is `HostActivated` with a single `VitisRegMapMMIFSlave` carrying
`x` / `a` / `b` in and `y` out. No streams, no memory, no DMA.

**The block design.** Zynq UltraScale+ PS → AXI SmartConnect → the exported IP's `s_axi_control`.
One clock, one reset, one address-map assignment. This is the smallest 4x2 BD that does anything.

**6.1 The host.** `driver.py`, generated into the overlay from the verified ABI:
field offsets, widths, packing and `start()` / `done()` / `wait()`
against the **bits of the control word at `0x00`** (not registers of their own — the mistake the
model itself once made). On top of it, a hand-written `board/host/run_simp_fun.py` that writes
`x`/`a`/`b`, starts, polls, reads `y`, and compares against the canonical vectors
from `simp_fun.py`. The board entry reuses upstream source/code-generation steps.

**6.2 `HostDriverGenStep` design note.** The driver must not import `pynq` at module scope; it takes
a backend object exposing `read_registers` / `write_registers` (§12). That is what lets rung 2 run
on any machine.

**6.3 The implemented conformance gate.** `HlsControlSpecStep` parses the actual
exported control header and IP-XACT and checks register intent, offsets, widths,
access and side effects before publishing a driver. Tool-free fixtures cover
failure cases; opt-in Vitis tests exercise fresh exported files.

This is the test `VitisRegMap`'s own docstring asks for, and until now there was no reason it had to
exist: the offsets were a closed loop in the simulator, with the model on both ends. **The generated
driver breaks that loop** — it puts a Waveflow-computed offset on a real AXI bus — so from S1 onward
a wrong offset is a wrong read on hardware, not a harmless internal convention.

**Array conformance remains a core concern:** `VitisRegMap`'s docstring warns that its
multi-word stride rule is verified for 32-bit scalars only, and that **Vitis maps array arguments on
`s_axilite` as a BRAM-backed region, which the model does not reproduce.** `simp_fun` has only
scalars. Current upstream `PolyAccel` sends coefficients in-band, so S2 no longer
has an AXI-Lite coefficient array. Core array fixtures retain the conformance gate
for future IPs that do expose such arrays.

**Gates:** rungs 1–6. Rung 6 = the canonical SimpFun result read back over PYNQ,
including its zero-clamping case.

**Done when:** a notebook on the 4x2 computes `y` through the generated driver, and the conformance
test is green.

**Rough effort:** ~1 week (most of it framework, not this example).

---

## 7. Stage S2 — `examples/stream_inband`: streams via DMA

**Progress:** current upstream protocol adapted without changing `poly.py` or its
handwritten body; DMA/DDR template, portable host and manual notebook implemented.
Offline gates passed; fresh tool builds and board acceptance pending.

**The design.** `PolyAccel` is `HostActivated` with `s_in` (`StreamIFSlave`), `m_out`
(`StreamIFMaster`), and a regmap carrying `halted`, `error` and `tx_id` status.
Commands and four polynomial coefficients ride **in band** on `s_in`.

**The block design.** PS → SmartConnect → AXI DMA (MM2S + S2MM) → the IP's AXIS ports, plus the
AXI-Lite control path from S1. The DMA's own control slave joins the address map.
Separate control and memory SmartConnects route DMA MM2S/S2MM to PS HP0 DDR
in the lower 2 GiB aperture. All blocks use the same 100 MHz PS PL clock. The
template uses 32-bit streams and simple DMA with SG and DRE disabled.

**7.1 The one thing most likely to hang the board: TLAST.** An AXI DMA **S2MM channel does not
terminate without TLAST**. This repo's standing law is that boundary TLAST comes from `ap_axis` —
the internal `framed_word` is internal only, and the flag that selects it must be a `ClassVar`. So
before any DMA work: confirm `PolyAccel`'s `m_out` lowers to an `ap_axis` boundary port that drives
TLAST at end of transfer. If it does not, that is the first commit of this stage, and it is a
codegen change, not a TCL change.

A hang here looks exactly like a host bug. Check TLAST in the VCD before suspecting the host.

**7.2 Match the current exported status ABI and in-band schema.** Coefficients
are not registers. DATA and END headers each serialize to six 32-bit words.
The exported status enum follows the kernel's C++ representation; it must not be
confused with compact stream serialization. Old register-coefficient vectors are
rejected before programming the board.

**7.3 The host.** `pynq.allocate` supplies input/output buffers, with explicit
cache maintenance and DMA timeouts. The portable DMA adapter arms S2MM before
output-producing MM2S transfers. Vectors use the upstream schema/scenarios and
contain DATA, samples and END; the host checks response ID, samples and final
status. Cubic, identity and constant cases exercise the same five-frame protocol.

**Gates:** rungs 1–6. Rung 6 = the board's output stream equals the pysim output for the committed
vector.

**Rough effort:** ~1 week.

> **Ordering note.** S2 and S3 may be swapped. `shared_mem` has the *simpler host* (one
> `pynq.allocate`, a base address at `0x10`, `ap_start` — no DMA IP, no TLAST). `stream_inband` has
> the *more reusable* result, because S5 needs the same DMA path to load its shot buffer. Doing DMA
> second, as written here, front-loads the risk; doing it third gets an easier win on the board
> sooner. Either is defensible — pick one and record which in the PR.

---

## 8. Stage S3 — `examples/shared_mem`: a master to DDR

**Progress:** not implemented in this extension. Start after fresh S1/S2 acceptance.
S2 already accesses PS DDR through DMA; S3 adds the accelerator's own AXI master.

**The design.** `HistAccel` is `HostActivated` with `s_in`, `m_out`, and `m_mem` (`MMIFMaster`),
and a **deliberately empty** `VitisRegMap({})` whose only job is `ap_start`/`ap_done`.

**8.1 The address register is the whole point.** `m_axi ... offset=slave` forces a control slave into
existence carrying `m_mem`'s base at `0x10`, and the kernel therefore needs **two masters**: the CPU
to write the base address, and something to pulse `ap_start`. `HostActivated` fills the reserved
`0x00` slot in the slave that already existed. On hardware this becomes: `pynq.allocate` a buffer,
write `buf.device_address` to `0x10`, pulse `ap_start`.

**8.2 Check for a second control slave.** Adding `s_axilite port=return bundle=control` absorbs
`ap_start` but does **not** fill the reserved `0x00` — Vitis then emits a *second*, auto-named
`s_axi_control_r` slave holding `m_mem` at `0x10`. One block, two AXI-Lite address spaces, and a BD
that looks wired but reads the wrong place. `hwgen.py` emits `s_axilite port=<m_axi port>
bundle=control` per m_axi port to force the single canonical slave; **verify in the exported IP's
port list that there is exactly one control interface** before building the BD.

**8.3 Cache coherency is a real answer, not a footnote.** The PS caches. Either allocate from a
coherent port, or invalidate/flush around the kernel run. A stale cache line looks like a wrong
result, not like a cache problem — say which choice the example makes, in the example's README.

**Gates:** rungs 1–6. Rung 6 = the histogram computed on the board equals the pysim histogram.

**Rough effort:** 3–4 days (it inherits S1's control path and adds one concept).

---

## 9. Stage S4 — `examples/bram_access`: a mixed IP

**Progress:** future board application stage; no new board acceptance claimed.

**Less new work than it looks, in a different place than expected.**

**9.1 The glue already exists and is already proven.** `wrapper_gen.py::render_wrapper` emits the
Verilog top instantiating the Vitis kernel and `bram_t2p.v`, joined — and that wrapper is the
elaborated top the XSI gate runs today. S4 does not write glue.

**9.2 What is new: packaging a *mixed* IP.** Two viable routes; pick one and say why in the PR:

- **(a) Package the wrapper as a user IP** — `ipx::package_project` over a directory holding the
  exported HLS IP plus the wrapper `.v`, yielding one BD cell with only AXI-Stream on its boundary
  (which is exactly the shape the wrapper was designed to present).
- **(b) Add both as sources** — the HLS IP in the IP repo, the wrapper `.v` as an RTL source, and
  instantiate the wrapper as an RTL module in the BD.

(a) is cleaner in the BD and matches how the wrapper is already conceived — *the design scope, the
elaborated top*. (b) is faster to first light. **(a) is recommended**, because it is the route that
generalizes to every later mixed design.

**9.3 The live risk is the byte-address shift, and it fails silently.** A `mode=bram` `Addr_A` is a
**byte** address — the generated RTL literally contains `Addr_A_local = Addr_A_orig << 32'd3` for a
64-bit array — while `T2pBram` indexes by word. `_bram_addr_shift` undoes this, and the same pass
widens `WEN` to the real byte count. Joined straight through, a memory of `N` words is reachable at
`N/(W/8)` locations and **everything above that aliases onto a live word, silently**. It round-trips
perfectly until the memory wraps: the old 16-bit witness filled 256 of 1024 words and was green
either way; a 64-bit design got the second half back twice.

`tests/examples/test_bram_access_xsi.py::test_the_wrapper_undoes_the_shift_vitis_actually_emits`
checks this against the *actual* generated RTL. **Rung 6 for this stage must write and read past the
aliasing point** — a board test that only touches the low quarter of the memory proves nothing.

**9.4 Which memory on hardware?** The hand-written `bram_t2p.v` infers a block RAM, so it can go to
the board as-is; `blk_mem_gen` is the alternative.  (True only since PR #250: until then both ports
were written in one `always` block and Vivado built the memory from flip-flops -- see
`plans/system_resources.md`.)  For multi-kernel systems, see `plans/ip_packaging_routes.md`. Staying with the committed `.v` keeps the board
design and the XSI gate on the *same* RTL, which is worth more than using the vendor generator.

**Gates:** rungs 1–6, with rung 6 exercising the full address range.

**Rough effort:** ~1–1.5 weeks.

---

## 10. Stage S5 — `examples/rf_shot_tx`: the converter

**Progress:** future board application stage; no RFDC overlay or measurement claimed.

**Hardest, last, and the only stage with board-specific physical constraints.**

**The design.** `RfShotTx` is a `FreeRunMod` composite (`waveflow/hw/rf_shot_tx.py`) — a loader and
a player over a shot buffer, `samp_per_word=4`, absolute indexing. Its samples go to the RFDC DAC.
The `Rfdc` model (`waveflow/hw/rfdc.py`) carries `n_rx` / `n_tx` and a `word` type; the board preset
`Rfsoc4x2SampWord.specialize(samp_per_word=4)` already exists.

**10.1 In simulation the converter is a model; on the board it is hardened IP.** That substitution
*is* the sim/synth duality this project is built on, and S5 is where it becomes literal. The RFDC is
instantiated via `create_bd_cell` plus a large pile of `set_property CONFIG.*` — tile/converter map,
sample rate, PLL/clocking, mixer, interpolation. Capture it from a working GUI design with
`write_bd_tcl` (§4.4). Do not hand-write it.

**10.2 Clocking is the fiddly part, and it is board-specific.** `fpga_refclk_in`, `dac0_clk`,
`sysref_in`, `vout00` — pins, XDC, and the RFDC's own PLL settings. A gen3 4x2 and a gen1 ZCU111
differ here; nothing about this section transfers between boards, which is precisely why the board
files' **provenance and version** are in the manifest (§4.1) rather than assumed.

**10.3 It depends on S2.** The shot buffer is loaded over a stream, so the DMA path from S2 is a
prerequisite, not a parallel concern.

**10.4 Rung 6 needs an instrument.** Unlike S1–S4, "matches pysim" is not checkable from the host
alone — the output leaves the chip. The honest gate is a **loopback**: DAC out to ADC in, captured
and compared. That means S5 in practice pulls in the RX half, and the loopback example
(`examples/rf_shot_loopback`) is the natural target. **Scope S5
deliberately:** either stop at rung 5 (a bitstream that builds and loads, with the DAC verified on a
scope) and open S6 for the loopback measurement, or plan for both halves up front. Do not discover
this at the end.

**Rough effort:** ~2 weeks, plus lab time.

---

## 11. Per-example repo layout

The implemented S1/S2 layout keeps source inputs separate from generated runs:

```
examples/<name>/
  board/
    bd/template.tcl            # committed cells and connections
    design.py                  # committed address/topology/clock contracts
    build_<name>.ipynb         # manual notebook; name follows the application
    host/
      run_<name>.py            # portable model/PYNQ entry
      sim_backend.py           # model adapter
  <name>_board.py               # application DAG using the shared CLI
  board_build/                 # generated; ignored; absent in a fresh clone
    notebook/run-*/            # notebook outputs
    overlays/overlay-*/         # collected overlay and manifests
```

S2 also has a portable DMA adapter and frame checks. The generated overlay
contains only BIT/HWH, `driver.py` and `manifest.json`. The driver embeds its ABI;
XSA, ABI/driver sidecars, reports and rebuild inputs remain in build directories.
Future S5 pin constraints belong with its application sources when implemented.
**Never commit the Vivado project tree** (`.xpr`, `.runs/`, `.cache/`, `.gen/`, `.hw/`, `.sim/`,
`.ip_user_files/`) — all regenerated, huge, path-dependent.

---

## 12. Off-board host execution

The implemented generated register driver depends only on a backend exposing
`read_registers` / `write_registers`; it imports neither PYNQ nor Waveflow.
Application `sim_backend.py` adapters connect that interface to the canonical
models. S2's model adapter exercises the framed transaction protocol without
claiming to emulate physical DMA, allocation or cache behavior.

Before export, `host_sim` checks the model protocol. After export,
`export_host_sim` runs the generated hardware-ABI driver against the model.
The board host uses PYNQ MMIO, allocation and the actual DMA. Physical acceptance
remains separate. A generic `pynq.Device` emulation layer was not added by this
minimal migration.

---

## 13. Environment manifest (the part everyone forgets)

Documented in the developer guide and recorded with generated overlay metadata:

- **Vitis HLS / Vivado version** — 2025.1 is what the author's machine has; a different version
  changes generated RTL, interface names, and the VLNV strings the BD TCL pins.
- **Part** — `xczu48dr-ffvg1517-2-e`.
- **RFSoC 4x2 board files** — vendor (RealDigital) and version. A BD TCL names a `board_part` that
  **does not exist on a clean machine**; vendoring the board files into `boards/` is the safe move.
- **PYNQ image version** on the SD card.

---

## 14. Risks and open questions

- **Vivado version drift breaks committed BD TCL.** It hard-codes IP VLNV version strings; a
  different Vivado prompts an upgrade. Mitigation: pin the version in the manifest and treat a
  version bump as a real change with its own PR.
- **Rung 5 is slow.** 20–40 min per bitstream, five examples. Bitstream builds should never be part
  of a default test run; `-m vivado` is opt-in, and CI (if any) runs rungs 1–2 only.
- **Fresh acceptance is still pending.** Offline tests and historical board runs
  cannot establish compatibility of newly generated RTL, exports or overlays.
- **DMA framing, allocation and cache maintenance** still need physical validation;
  the model adapter does not simulate the PS memory/cache system.
- **ABI changes must be checked against the actual export.** This includes arrays
  in future IPs and enum/pointer representation; S2 coefficients are now in-band.
- **S5's rung 6 needs either an instrument or the RX half** (§10.4).
- **Open: where does a shared host API live?** This plan deliberately generates a *per-example*
  driver and stops there. `plans/rf_lab_platform.md` proposes a backend-neutral `RfLab` façade for
  the teaching labs. Those should converge — but only after five generated drivers exist to
  generalize from, for the same reason §4.4 defers the BD generator.
- **Open: does a BD generator ever get written?** Revisit after S5, with five committed templates in
  hand.

---

## 15. Relationship to other plans

- `plans/rfsoc_4x2_bringup.md` — the board, the archival contract (§4.4, §11, §13), and the
  clean-clone acceptance test. **Read it before S0.** This plan is its packaging half.
- `plans/rf_lab_platform.md` — what the RF hardware is *for*; owns the host-API question (§14).
- `plans/rtl_module.md` — `add_rtl_mod` and the wrapper that S4 packages.
- the resource_model plan ([commit b715f1d](https://github.com/sdrangan/waveflow/commit/b715f1d)) — the wrapper as the first scope an area number can be defined against;
  S4's IP is that scope made physical.
- [PR #177](https://github.com/sdrangan/waveflow/pull/177) — why a skipped gate is a missing gate (§3).
- [Board packaging development](../docs/guide/flows/board_packaging.md) — current
  APIs, extension procedure, source map, validation contracts and manual commands.
- `docs/guide/flows/bitstream_ipi.md` — the broader device/system packaging flow;
  it links to the application-template extension without replacing that flow.
