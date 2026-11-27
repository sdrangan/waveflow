# Plan — board packaging: an example becomes IP, a bitstream, and a PYNQ host

**Status:** S0–S2 are implemented. The current cleanup passed the core and
integration offline regressions, four real Vivado/Vitis clock checks and fresh
S1/S2 overlay builds.
Both new overlays passed physical-board runs and returned-record integrity review.
An independent local clone plus the current source patch also passed 433 offline
tests and both overlay builds in a separate virtual environment. Acceptance covers
the current Linux workstation and RFSoC4x2; a committed-revision rebuild on another
machine and actual Windows AMD-tool acceptance remain unrecorded.
S3–S5 are planned application extensions.

This plan records scope, acceptance and the next stages. The API reference,
source map and commands are in
[Board packaging development](../docs/guide/flows/board_packaging.md).
The compatibility baseline is upstream `main` at `9a09fb1e`.
Target: RealDigital RFSoC4x2, `xczu48dr-ffvg1517-2-e`, Vitis/Vivado 2025.1.
Tests, notebooks, AMD-tool builds and board runs are invoked manually.

## 0. Progress and next actions

### Accepted baseline

| Stage | Verified evidence | Scope |
| --- | --- | --- |
| S0 clocks | Four real Vivado/Vitis clock checks passed in 138.41 s, zero skipped | After the catalog metadata repair; PS roundtrip, MMCM, rejected-request diagnostics and resolved HLS target |
| S1 | C/RTL, BD, implementation and returned board JSON passed; outputs `[0, 11, 17]` | AXI-Lite control |
| S2 direct-source example | 37 application tests passed in 2.43 s; complete overlay build and three returned board cases passed | PS DDR, simple DMA, 32-bit AXIS |
| PR cleanup, core offline regression | 362 passed, 22 deselected in 4.86 s; manually run with Python 3.12.3 / pytest 9.1.1 | `tests/board`, application packaging and seven XSim-launcher cases; external-tool/board markers excluded |
| PR cleanup, integration regression | 71 passed, 1 deselected in 0.68 s; JUnit has zero failures, errors or skips | DMA (6), notebooks (4), BuildConfig (13), Tcl target (13) and toolchain (35) |
| Fresh source checkout | 433 offline tests passed with zero skips; S1/S2 complete builds passed in 405.6/550.5 s | Independent local Git clone plus current uncommitted sources, separate venv, no historical build products copied |
| Broader software regression | 3177 passed, 49 skipped, one independently reproduced upstream workspace-count failure | Before the remaining PR cleanup |

The latest accepted S1 overlay is `overlay-tmdvivaz`; its manifest SHA256 is
`694974081d1490f9079c55fb45a7009f4716caf3fdb3b722d1a50d3115ba2bf9`.
The latest accepted S2 overlay is `overlay-6bgv3k3i`; its manifest SHA256 is
`ca033a5acebdd9ad0ead3b3cc6421432759369fbc1127bb0d7eee6e92ad10496`.
Both used PS PL0 configured at 99.999985 MHz and a 100 MHz HLS target.
S1 setup/hold slack was 4.728/0.022 ns; S2 was 4.840/0.011 ns; severe DRC was zero.

S2 reads the repository's C++ body and testbench directly into each fresh synthesis
snapshot. Its 545.3 s CMD build passed eight C scenarios, timing RTL co-simulation,
export/ABI checks, BD validation and implementation. Cubic, identity and constant
board cases each returned 100 correct samples, zero maximum error and five DMA
transfers. Returned manifest, bitstream, ABI and vector hashes matched; response
IDs, statuses, DDR apertures and plots were checked. The user also ran all 37
application regressions, including stale-copy and direct-source checks.

These records accept their original inputs. Generated receipts, overlays, board
JSON and plots remain in ignored build directories. Detailed migration and run
history is retained locally rather than included in this PR plan. A rebuild from
a clean clone on another machine has not yet been recorded.

### Current builds accepted on RFSoC4x2

| Stage | New overlay | Full CMD build | Setup / hold slack | Severe DRC |
| --- | --- | --- | --- | --- |
| S1 | `overlay-tmdvivaz` | Passed in 403.5 s | 4.728 / 0.022 ns | 0 |
| S2 | `overlay-6bgv3k3i` | Passed in 545.3 s | 4.840 / 0.011 ns | 0 |

Both contain the repaired catalog metadata and resolve PS PL0 to 99.999985 MHz
with a 100 MHz HLS target. Receipt links, input snapshots, implementation files,
driver ABI and collected payload hashes were reviewed. S1 C/RTL returned 11;
S2 passed eight C scenarios and timing RTL, including the revised XSim launcher
on Linux, with input frames 6/100/6 and output frames 1/100 words.

Unpacked handoffs `test_s1_cmd_tmdvivaz` and `test_s2_cmd_6bgv3k3i` remain under
their ignored application build roots. Their prepared English notebooks pin file
identities and default to `RUN_BOARD_TEST=False`; no tests were run while preparing
them. Collected overlays keep `board_execution_verified=False`; physical acceptance
is recorded separately in returned board JSON. Previous records remain available.

Returned S1 JSON passed integrity review with outputs `[0, 11, 17]` and record
`board-acceptance-s1-20261124T182905929552Z.json`. S2 cubic, identity and constant
each passed with 100 samples, zero maximum error and five transfers; the result
directory is `20261124T182955661248Z-a5e77e69`. All four records match the current
manifest, bitstream and ABI hashes; S2 also matches the source vector hashes and
reference samples. Completion/status fields, response IDs, transfer lengths and
aligned DDR addresses within the declared HP0 apertures passed review. The returned
plot agrees with the records. Board runtime was Python 3.10.4 / PYNQ 3.1.1, with
NumPy 1.21.5 for S2. Executed notebooks were not returned; local copies remain the
unexecuted templates. JSON records establish this round's board acceptance.

The separate fresh-checkout run preserved all 3674 pinned source files and used
its own editable installation. Receipt and file-hash review passed for S1
`overlay-5mrif03w` and S2 `overlay-ak8j0_60`; targets, register fields, addresses,
timing and DRC match the board-accepted designs. The staged Tcl does not reference
the original checkout. These new overlays have build acceptance only; physical
acceptance above remains attached to its original overlay hashes.

### PR cleanup and validation

- S2 XSim replay uses the discovered platform executable and upstream command and
  environment helpers, retains its timeout, and rejects stale VCD output. Tcl
  paths are normalized before validation. Tool-free Linux/Windows launcher cases
  have been added; a real Windows AMD-tool build is not claimed.
- The vendor `xitem.json` contributors are separate valid JSON objects. Its pinned
  hash and catalog conformance test are updated. Device/pin/preset XML and license
  files retain their original contents.
- Unused imports are removed; pytest fixture re-exports remain explicit. This plan
  keeps one progress summary and the stage roadmap.

The metadata repair changed the board-resource fingerprint, so fresh target,
clock and build receipts were produced for the current inputs. Previous records
continue to describe their original inputs. The user ran the core offline regression:
all 362 selected tests passed, including catalog conformance, pinned resource
hashes and the seven mocked launcher cases. The 22 deselected tests were excluded
by the external-tool/board marker expression. The user then ran all four real
clock checks successfully in 138.41 s; the JUnit record reports zero failures,
errors or skips. Fresh S1/S2 builds, physical-board runs and returned-record review
passed as recorded above. Mocked Windows commands do not establish an actual
Windows build.
The separate integration selection then passed all 71 selected cases in 0.68 s,
with one external-tool case deselected. Together these two non-overlapping
selections cover 433 passing offline tests after the cleanup.

### Next actions

1. Core and integration offline regressions are complete: 433 passed across two
   selections, with 23 external-tool/board cases deselected.
2. Real clock checks, fresh S1/S2 builds, board runs and returned JSON review are
   complete. Keep generated overlays, receipts, board records and plots ignored.
3. Record Windows tool acceptance separately from mocked command coverage, and
   record a committed-revision rebuild on another machine before claiming
   portable end-to-end acceptance. Same-workstation fresh-checkout validation
   is complete: a separate environment passed 433 offline tests and both builds
   without copied historical build products.
4. Submit the S0–S2 extension with its remaining validation limits stated. Implement
   S3 afterward as a separate application stage.

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

**Keep the existing Waveflow core integration small.** Changes to pre-existing
`waveflow/` files are limited to optional board/clock validation in `BuildConfig`,
the board-aware `tcl_target` branch, the shared `run_vivado` runner and its public
export. New board-packaging functionality lives in dedicated modules. Test
sources, conformance fixtures, example sources, Tcl templates, portable hosts and
manual notebooks are included in the submission. Generated application builds,
overlays, measurements and local review reports remain ignored.

**Board and calibration Platform have separate jobs.** `waveflow/board/spec.py`
defines the physical target, versioned vendor resources, reference clocks and
supported clock-generation routes.
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

**Progress:** core implemented; all four real clock checks passed again after the
catalog repair (138.41 s, zero skipped), as did fresh S1/S2 overlay builds and
returned board-record reviews. Dedicated S0 tool gates
and applicable XSI acceptance remain separate checks.

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
assert board.reference_clock("ps_reference").frequency_hz == 33_333_330
assert board.reference_clock("pl_reference").frequency_hz == 122.88e6
```

`Board.rfsoc4x2()` supplies the bundled target, vendor resources, tool release and
reference clocks and supported generation routes. Application targets are
`ClockRequest` values, not board defaults. Existing non-board synthesis defaults
are preserved. `BuildConfig` construction is tool-free; board synthesis resolves
the request and checks feasibility before invoking HLS.

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
implemented. The baseline CMD build and returned physical-board records passed
with outputs `[0, 11, 17]`. The repaired catalog's fresh CMD build also passed
(403.5 s). `overlay-tmdvivaz` passed on the board with the same outputs, and its
returned JSON passed integrity review (§0).

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

**Progress:** the direct-source baseline passed all 37 application regressions,
a complete CMD build and three returned physical-board cases. The current XSim
launcher and catalog changes passed core offline checks and a fresh Linux CMD
build (545.3 s). All three `overlay-6bgv3k3i` board cases and their returned-record
integrity reviews passed (§0).

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

S2 establishes the DMA command/response transport that S3 can reuse. S3 adds a
separate bulk-data path through the accelerator's own AXI memory master.

---

## 8. Stage S3 — `examples/shared_mem`: a master to DDR

**Progress:** not implemented in this extension. Start after fresh S1/S2 acceptance.
S2 already accesses PS DDR through DMA; S3 adds the accelerator's own AXI master.

**The design.** `HistAccel` is `HostActivated` with `s_in`, `m_out`, and `m_mem` (`MMIFMaster`),
and a **deliberately empty** `VitisRegMap({})` whose only job is `ap_start`/`ap_done`.

**8.1 Use the exported pointer ABI.** `m_axi ... offset=slave` adds a base-pointer
register to the control slave. Its actual generated name, offset and width come
from the verified header/XML and explicit `pointer_bindings`; the host uses the
generated driver's `set_pointer()` method. Allocate a contiguous DDR arena and
keep its physical base distinct from the command's relative byte offsets.

**8.2 Check for a second control slave.** Adding `s_axilite port=return bundle=control` absorbs
`ap_start` but does **not** fill the reserved `0x00` — Vitis then emits a *second*, auto-named
`s_axi_control_r` slave holding `m_mem` at `0x10`. One block, two AXI-Lite address spaces, and a BD
that looks wired but reads the wrong place. `hwgen.py` emits `s_axilite port=<m_axi port>
bundle=control` per m_axi port to force the single canonical slave; **verify in the exported IP's
port list that there is exactly one control interface** before building the BD.

**8.3 Cache coherency is a real answer, not a footnote.** The PS caches. Either allocate from a
coherent port, or invalidate/flush around the kernel run. A stale cache line looks like a wrong
result, not like a cache problem — say which choice the example makes, in the example's README.

**8.4 Commands still need transport.** Existing `HistAccel` consumes a `HistCmd`
on `s_in` and returns `HistResp` on `m_out`. Programming the base and `ap_start`
alone leaves it waiting for a command. Reuse S2's simple DMA for descriptors and
responses; route the accelerator's own master through the memory SmartConnect
to PS HP0 for samples, edges and counts. Start with the existing single PS PL0
application domain at 100 MHz and explicit noncoherent cache maintenance.

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
- **Acceptance is input-specific.** The current XSim/catalog cleanup passed
  software, clock, overlay-build and physical-board checks with returned-record
  integrity review, plus same-workstation fresh-checkout builds. Windows AMD-tool
  acceptance and a committed-revision rebuild on another machine remain outstanding.
- **DMA framing, allocation and cache maintenance** are exercised by the three
  returned S2 board cases. The model adapter alone does not establish
  physical PS memory/cache behavior.
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
