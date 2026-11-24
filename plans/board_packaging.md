# Plan — board packaging: an example becomes IP, a bitstream, and a PynQ host

**Status: upstream `main` at `9a09fb1e` is integrated through `44bcb1b1`.
Fresh S1/S2 software/model checks passed. Broader software regression passed 3177
tests, with 49 skipped and one independently reproduced upstream failure.
The user-run S1 CMD build passed C/RTL simulation, BD validation, implementation
and overlay identity review. All three physical-board cases passed with
`[0, 11, 17]`; the returned acceptance JSON and payload/ABI hashes were verified.
Current-source S1 CMD build and physical-board acceptance are complete.
The fresh S2 CMD build passed C/RTL, BD, implementation and overlay review.
All three returned S2 board records passed read-only review: 100 samples,
five completed DMA transfers and zero maximum absolute error per case;
manifest, bitstream, ABI and per-case vector hashes match.
Current-source S1/S2 CMD builds and physical-board acceptance are complete.
Earlier notebook outputs and overlays are preserved as historical evidence.
Vitis/Vivado, software tests and board runs remain manual.
Updated 2026-11-24.** This plan tracks the template-driven
RFSoC4x2 board packaging extension. The implemented APIs, source map and manual
commands are in [Board packaging development](../docs/guide/flows/board_packaging.md).

**Who this is written for.** Someone other than its author, with a board and a Vitis/Vivado
install, working **one stage at a time**. Each stage below is one PR (many commits), lands green,
and is independently useful. A stage that cannot be finished should be *cut at a rung boundary*
(§3) rather than left half-wired.

**Target board:** RFSoC 4x2 (RealDigital), `xczu48dr-ffvg1517-2-e`. **Host:** PYNQ.

---

## 0. Progress and next actions

### Current source and S1/S2 revalidation — 2026-11-24

Fetched upstream again and confirmed `9a09fb1e` (PR #344) is still its current
`main`. Local `main` matches it; `board_packaging` includes it through merge
`44bcb1b1`. The existing board runtime integration remains compatible. Regression
coverage checks the shared Vivado environment handling and complete PolyAccel
schema-header generation under upstream's missing-dependency warning.

The new software regression completed with **3177 passed, 49 skipped and one
known upstream failure** in `test_there_are_workspaces_to_check` (two committed
XSI workspaces, while the assertion requires at least three). All four updated
notebook checks passed in an isolated checkout with hardware switches disabled;
the live notebooks' outputs, kernels and manual switches were preserved. The
JUnit report is `/tmp/waveflow-latest-s1-s2-tests.xml`. Hardware-marked tests were
excluded, and the upstream workspace-count assertion was left unchanged.

Fresh software runs on this source passed:

- S1: the canonical register cases returned `[0, 11, 17]`. Result:
  `examples/regmap/board_build/revalidation/software-uckhu9_0/host_sim.json`.
- S2: upstream model/pysim checks passed at 32 and 64 bits. The board host model's
  cubic, identity and constant cases each passed with 100 samples and maximum
  absolute error `0.0`. Result:
  `examples/stream_inband/board_build/revalidation/software-9g5twj5j/host_sim.json`.

These are software results with `board_execution_verified=False`. The updated
build notebooks display the source commit and retain their earlier outputs.

The user subsequently completed the S1 CMD build in 406.8 seconds. Read-only
review confirmed C simulation and RTL co-simulation each returned 11; BD
validation passed at `0xA1000000` with a 64 KiB aperture. The collected overlay
has source `44bcb1b1`, a configured PS PL0 frequency of 99.999985 MHz, WNS
4.728 ns, hold slack 0.022 ns and no severe DRC. Payload hashes, embedded driver
ABI, HWH IP identity and pinned board-resource hashes agree.

The new S1 CMD overlay is
`examples/regmap/board_build/overlays/overlay-mb3xu3ac/`. Its manifest SHA256 is
`5446bfb1e1e81ef905db33fbad36a2b68fabb358227bbeacf0142d853e5c5870`.
An unpacked handoff with the matching overlay, portable host and an unexecuted
English `test_s1.ipynb` is prepared at
`examples/regmap/board_build/test_s1_cmd_mb3xu3ac/`. Its board-test switch is
disabled by default. No test, tool invocation or upload was performed during
this review or handoff preparation.

The returned acceptance JSON confirms a successful RFSoC4x2 run from this
handoff: `passed=True`, outputs `[0, 11, 17]`, and
`board_execution_verified=True`. The recorded board
path is
`/home/xilinx/jupyter_notebooks/Ruibin/waveflow_package/test_s1_cmd_mb3xu3ac/board-acceptance-s1-20261124T145433482404Z.json`.
The original JSON is now returned under
`examples/regmap/board_build/test_s1_cmd_mb3xu3ac/` and was reviewed read-only.
All three input/expected/output tuples and completion statuses match: start is
cleared, done/idle/ready are set, auto-restart and interrupt are cleared.
Its overlay manifest, bitstream and ABI hashes match `overlay-mb3xu3ac`; both
the source and staged overlay payloads match the manifest, and the generated
driver's embedded ABI hash agrees. Runtime is Python 3.10.4 / PYNQ 3.1.1;
completion was recorded at `2026-11-24T14:54:33.481776+00:00`.
This completes current-source S1 CMD build, physical-board acceptance and
acceptance-record review. The review did not execute the host or any tests.

The user also completed the S2 CMD build from `44bcb1b1`. Read-only inspection
confirmed eight C-simulation scenarios and the timing RTL co-simulation passed,
with verified input frames of 6, 100 and 6 words and output frames of 1 and
100 words. BD validation and implementation passed. The configured PS PL0
frequency is 99.999985 MHz with a 100 MHz HLS target; WNS is 4.840 ns, hold slack
is 0.011 ns and severe DRC count is zero. Poly and DMA AXI-Lite apertures are
`0xA1000000` and `0xA1010000`. The HWH describes 32-bit simple DMA without SG
or DRE, with MM2S/S2MM reaching the low 2 GiB PS DDR window.

The current S2 delivery is
`examples/stream_inband/board_build/overlays/overlay-56c683vl/`. Its manifest
SHA256 is `859de225e4b0a39e180b7e39180bf98cf85e88a8bda510938fb20c4cf46ba15c`.
Payload hashes, embedded driver ABI, HWH IP/address identities and board-resource
hashes agree. The existing collected-driver model receipt matches this ABI and
records all three 100-sample cases passing with zero maximum absolute error.

The unpacked board handoff is
`examples/stream_inband/board_build/test_s2_cmd_56c683vl/`: four overlay files,
the portable host and DMA adapter, the current CMD build's three vector files,
an English `test_poly.ipynb` and upload instructions. Setup pins manifest, host
and vector hashes. The manually enabled test checks all three cases and the
five completed DMA frame labels, directions and byte counts per case; a separate
plot cell saves measured/reference curves and residuals. The notebook is
unexecuted with `RUN_BOARD_TEST=False`. Preparation did not run any model,
test, notebook, AMD tool or upload.

The returned original JSON records confirm `passed=True` and
`board_execution_verified=True` for all three S2 cases. The board
acceptance directory is
`/home/xilinx/jupyter_notebooks/Ruibin/waveflow_package/test_s2_cmd_56c683vl/board-results/20261124T152508374772Z-cf8b57eb`.
Its three `board-acceptance-s2-<case>.json` records and `poly-results.png` are
returned under the corresponding local `test_s2_cmd_56c683vl/board-results/`
directory. Read-only review found:

| Case | Samples | Maximum absolute error | Completed DMA transfers |
| --- | --- | --- | --- |
| Cubic | 100 | 0.0 | 5 |
| Identity | 100 | 0.0 | 5 |
| Constant | 100 | 0.0 | 5 |

The saved samples agree with the matching vector references, including their
reported error values and response words. Manifest, bitstream, ABI and vector
hashes all match the current CMD overlay and staged files; source/staged overlay
payloads and the driver's embedded ABI hash also agree. Completion statuses
show start cleared, done/idle/ready set, auto-restart and interrupt cleared;
`halted`, `error` and final `tx_id` are zero.

Every case records the expected completed frame sequence: DATA header / MM2S /
24 bytes, response header / S2MM / 4 bytes, sample input / MM2S / 400 bytes,
sample result / S2MM / 400 bytes, and END header / MM2S / 24 bytes. All recorded
buffer addresses are aligned and inside the template's low-2-GiB DDR aperture.
Runtime is Python 3.10.4, NumPy 1.21.5 and PYNQ 3.1.1. The returned six-panel
plot shows all measured curves overlapping their references and zero residuals.

This completes current-source S2 CMD build, physical-board acceptance and
acceptance-record review. Together with S1 above, the requested S1/S2
revalidation after the upstream sync is complete for these CMD build paths.
No tests or host programs were rerun to inspect the returned artifacts.
Subsequent S3/S4/S5 implementation and their acceptance remain future work.

The assistant has not launched Vitis/Vivado builds, board tests or pushes during
this revalidation; the user performs hardware operations manually. The XSI
workspace-count failure also exists on the November 16
baseline; it is independent of the board extension and remains unchanged.

### Earlier migration and validation history

The minimal implementation was migrated onto upstream `main` at
`4bb72aa9d2f90d6851aa9c4ee85ccdb588ff13c9`. That was the common base used for the
migration audit. On 2026-11-16, the fork's remote `main` and local `main` were
synchronized to upstream `54cb46e1f37c4d376167792b27734f8c90291f91` (PR #311).
The local `board_packaging` branch includes it through merge commit `9e627b9b`.
This merge is not pushed; only the explicitly requested fork `main` sync was
published. The three migration commits
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
| S0: target and clocks | Board/configuration checks plus four real Vivado/Vitis clock tests passed on 2026-11-17; retained clock contexts match current source | Applicable XSI gates and broader packaging acceptance remain separate |
| S0: packaging pipeline | Offline conformance/failure tests and real S1/S2 notebook export, ABI, BD and implementation outputs checked | Dedicated tool-gate suite and remaining physical acceptance coverage |
| S1: SimpFun | Current notebook build `run-g1ifa6_n` passed HLS C/RTL, export/ABI, BD, implementation and collected-driver checks | Accept the new `overlay-4u2wyf7o` on board and review returned JSON/hashes; CMD deferred |
| S2: PolyAccel | Pre-update notebook build checked; returned notebook shows cubic, identity and constant board cases passed with zero maximum error | Rebuild current branch and accept the new overlay on board; review the three returned JSON records/hashes; CMD deferred |
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
The independently generated CMD overlay has not yet been accepted. The user
explicitly deferred CMD testing on 2026-11-15 and chose to proceed with the S2
notebook. Deferred CMD checks do not block that next notebook validation.

The S2 notebook build at source commit `36d54af3` produced
`run-vmhut1lv/overlays/overlay-ffdh9xj3`. Its receipts and payload hashes were
checked locally. Eight C-simulation scenarios passed; RTL co-simulation covered
the `timing` scenario, including frame data and TLAST. The verified input frames
have 6, 100 and 6 words; output frames have 1 and 100 words. All three collected
driver/model cases passed with 100 samples, five host transfers and zero maximum
absolute error. Clock readback is 99.999985 MHz, WNS is 4.840 ns, hold slack is
0.011 ns and severe DRC count is zero.

A local, unpacked board test directory is prepared at
`examples/stream_inband/board_build/test_poly_notebook_vmhut1lv/`. It contains the
four-file overlay, current portable host/DMA adapter, this run's three vector
files and an English `test_poly.ipynb`. The board notebook checks the uploaded
files before its manually enabled test cell. The original handoff defaults to
`RUN_BOARD_TEST = False`; the returned copy preserves the user's enabled test
cell and its execution outputs. Generated test material stays outside tracked
source. No automatic push is made.

The returned `test_poly.ipynb` now contains successful setup, physical-board test
and plotting outputs, with no saved cell errors. Cubic, identity and constant
each report 100 samples, `passed=True` and maximum absolute error `0.0`.
The test cell asserts five completed transfers per case and prints
`Board execution verified: True`. Its saved six-panel figure shows all three
measured curves overlapping their references and zero residuals. The printed
board acceptance directory is
`test_poly_notebook_vmhut1lv/board-results/20261116T040509343248Z-af0c54ca`.
The setup output's ABI hash matches the local overlay. Only the executed
notebook has been returned; the three original acceptance JSON files are not
present locally, so their recorded manifest, bitstream, ABI and vector hashes
have not yet been independently checked against the workstation artifacts.

### Upstream synchronization — 2026-11-16

The sync imports 184 upstream commits. Four text conflicts were resolved while
keeping both the upstream behavior and the optional board extension:

- `.gitignore`: retain upstream channel-manifest exceptions and secret ignores,
  together with the board build ignore and pinned-resource exceptions.
- `pyproject.toml`: retain upstream optional extras and `legacy/*.h` package data,
  together with the three board package-data entries and board test markers.
- `waveflow/toolchain/toolchain.py`: retain upstream calibrated-version reporting,
  Vitis header discovery and `_tool_env`, together with the board Vivado runner.
  The runner now uses `_tool_env` so Windows `MSYSTEM` cleanup also applies to
  board validation and implementation launches.
- `tests/toolchain/test_toolchain.py`: retain both suites and add two Vivado
  environment regressions, covering inherited and explicitly extended environments.

The core board/application/configuration/toolchain suite passed **399 tests**;
the notebook, S1/S2 model/codegen, build, schema/stream, documentation and MCP
integration checks passed **131 tests**. In total, **24 tool-dependent cases were
deselected**. Notebook defaults were tested in an isolated merged-tree checkout;
the original notebook outputs, kernels and manual switches were preserved.
At the time of this sync, every changed path relative to upstream remained within
the original 85-path board extension. The canonical SimpFun and PolyAccel sources, arithmetic body,
scenarios and upstream `poly_build.py` match upstream exactly. No Vitis/Vivado
build or board execution was launched during this sync. The earlier overlays
and board results remain evidence for their recorded pre-sync source commits.

### Clock-profile cleanup — 2026-11-17

Initially removed the unused RF stream default profile and its declared BD recipe mapping.
At that point, the board defaults contained `ps_control` (100 MHz), `pl_reference` (122.88 MHz),
and `adc_reference` / `dac_reference` (both 491.52 MHz reference clocks).
Custom PS PL0 and PL-reference/MMCM requests remain available through
`ClockRequest`. Frequency-conformance tests now use an explicitly synthetic
`test_hls` profile instead of adding an RF operating point to the board defaults.
The core software checks passed **399 tests**, with **23 tool-dependent cases
deselected**. S1/S2 continue to use their existing 100 MHz configuration; RFDC
sampling rates and stream clocks remain part of the future RF design.

### Reference clocks and application requests — 2026-11-17

The intermediate profile API above is replaced by a reference-only board model.
`Board.reference_clocks` records PS (33.33333 MHz from the pinned preset), PL
(122.88 MHz), ADC and DAC (both 491.52 MHz) references as `ReferenceClock` values,
with no application output or HLS target. Board-owned generation routes bind
PS PL0 to the PS reference and MMCM to the PL reference.

Applications provide `ClockRequest(source, requested_hz, hls_target_hz,
tolerance_ppm)`. `BuildConfig(clock_request=...)` performs tool-free validation
for software checks. Before HLS, `synthesize_for_board()` invokes Vivado to check
the route's input reference, generated frequency and allowed error. Packaging
requires the resulting `ClockResolution`; it rejects unresolved, mismatched or
stale requests. ADC/DAC internal PLL generation remains outside these two routes.
S1/S2 keep a 100 MHz application default and expose `--clock-hz`, `--hls-clock-hz`
and `--clock-tolerance-ppm`. Their manual notebooks expose the same settings and
retain historical execution outputs. Core checks passed **406 tests** and the
isolated notebook/model/codegen/documentation integration checks passed **131**,
with **24 tool-dependent cases deselected** in total. Manual tool/board validation
remains pending.

### Verified clock hardware inventory — 2026-11-17

Cross-checked RealDigital's V3I1 schematic (sheets 15-18), A6 manual (Figure 5
and Appendix A), and the pinned PS preset. The declarations now live in
`waveflow/board/rfsoc4x2.py`: 18 nominal reference/synchronization signals and
11 external devices or internal PS PLLs. `ReferenceClock` includes physical
source/output and consumers; `Board.clock_devices` includes input references and
fixed, factory-ROM, PS-preset or runtime-SPI configuration requirements.

The inventory covers the Si5395's 48 MHz crystal and PS/PL/PL-DDR/peripheral
outputs, LMK's 10 MHz reference and 160 MHz VCXO, its PL/SYSREF/LMX outputs,
both LMX2594s and the five internal PS PLLs. PS DDR remains a DPLL consumer;
the preset's derived 1200 MHz setting is not an external reference. Programmable
frequencies are documented defaults, not a claim about current board state.
The LMK CLKIN0/1 discrepancy is documented, following schematic sheet 16.

The two implemented generation recipes remain PS PL0 and PL-reference/MMCM.
Inventory registration does not implement LMK/LMX programming, PL DDR or RFDC
integration. Resolution fingerprints include the clock inventory hash. Core
software validation passed **410 tests**, with **23 tool-dependent cases
deselected**. Another **23 notebook/documentation checks** passed using temporary
notebook copies with hardware switches off. Vendor board files and all user notebook outputs were preserved;
no AMD tools, physical-board tests or pushes were run for this update.

### S0 real clock validation passed — 2026-11-17

The user manually ran `tests/board/test_vivado_clock_resolution.py` with the
configured Vivado/Vitis 2025.1 executables. Inspection of the JUnit report
confirmed **4 passed, 0 failed, 0 skipped**, in 139.749 seconds. All five retained
clock manifests match the current board, reference, inventory and recipe context.

| Check | Verified result |
| --- | --- |
| PS PL0 request and round trip | 100 MHz request produces 99.999985 MHz, 0.15 ppm error; round-trip error is zero; IOPLL, divisors 15 and 1 |
| PL-reference MMCM | 122.88 MHz input produces exactly 245.76 MHz; multiplier 9.75, input divider 1, output divider 4.875; separate HLS target is 250 MHz |
| Rejected PS request | 123.456 MHz request produces 115.384598 MHz and is rejected outside tolerance; diagnostic manifest retained |
| Clock target passed to HLS | Vitis synthesizes the probe for `xczu48dr-ffvg1517-2-e` with a 10 ns target period |

The local report is `/tmp/waveflow-s0-clock-results.xml`; temporary tool traces
are under `/tmp/pytest-of-nyu/pytest-41/`. This accepts the updated clock
configuration and HLS-target path. It does not establish full S0 packaging/XSI
acceptance, external-chip programming, implementation timing or a physical-board
clock measurement. No additional tool or board run was started during review.

### Current S1 notebook build verified — 2026-11-17

The user manually rebuilt `examples/regmap/board/build_simp_fun.ipynb` in
`examples/regmap/board_build/notebook/run-g1ifa6_n`. Saved code cells executed
in order (1-6), with no error outputs. Independent read-only inspection checked
the synthesis snapshot/RTL/report hashes, C/RTL simulation evidence, BD inputs
and receipts, implementation reports, and collected overlay/driver identities.
The clock fingerprint matches the current board/reference/inventory/recipe.

- Clock: PS PL0 through IOPLL, requested 100 MHz and calculated 99.999985 MHz
  (0.15 ppm); separate HLS target is 100 MHz / 10 ns.
- C simulation and RTL co-simulation each returned 11 for the default vector.
- BD validation passed; SimpFun AXI-Lite aperture is `0xA1000000`, size 64 KiB.
- Routed setup slack is 4.728 ns and hold slack is 0.022 ns. Timing and
  unconstrained checks passed; severe DRC count is zero.
- HWH addresses and XSA/BIT consistency passed. The collected driver's three
  model cases independently returned `[0, 11, 17]`.

The new delivery directory is
`examples/regmap/board_build/notebook/run-g1ifa6_n/overlays/overlay-4u2wyf7o/`.
Its delivery files are `simp_fun.bit`, `simp_fun.hwh`, `driver.py` and
`manifest.json`; source provenance is merge commit `9e627b9b` plus the recorded
input/clock hashes. `board_execution_verified` remains false. No additional
AMD tool invocation or physical-board test was performed during this review;
the user's notebook outputs were preserved.

The user chose to rebuild the **S2 notebook first**, before starting new board
acceptance. The next actions remain **manual**:

1. Restart the S2 build notebook kernel and run its cells in order, with a fresh
   build directory and PS PL0/HLS requests at 100 MHz. Review its new clock,
   HLS/export/ABI, BD, implementation and collected-driver records. Preserve the
   historical notebook outputs until the user replaces them through execution.
2. After the S2 build review, perform board acceptance for the fresh S1 and S2
   overlays. S1 `overlay-4u2wyf7o` must return `[0, 11, 17]`; S2 must pass cubic,
   identity and constant cases. Return the JSON records, match manifest/bitstream/
   ABI/vector identities, and check output errors and completed transfers.
3. Preserve the previously returned S2 notebook and recover the older S1/S2 raw
   acceptance records when available. Those records remain evidence for their
   original builds. Keep generated overlays, reports and JSON outside tracked source.
4. Track notebook acceptance and the explicitly deferred CMD validation
   separately. The notebook CMD sections remain available for a later independent
   build/test; do not run them during this notebook-only validation round.
5. Proceed to later stages after reviewing the current acceptance evidence;
   retain generic multi-IP/system packaging and RF work as follow-up scope.

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
implemented; offline gates and the fresh notebook tool build passed. The user
reported a passing notebook-overlay board run with outputs `0`, `11`, `17`.
Review of the returned notebook acceptance JSON and independent CMD build/board
acceptance remain pending. CMD validation is deferred by the user's request and
does not block proceeding to S2 notebook validation.

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
Offline gates and the fresh notebook HLS/export/ABI/BD/implementation checks
passed. The returned RFSoC4x2 notebook shows cubic, identity and constant passing
with 100 samples each, five asserted transfers per case and zero maximum error.
Its saved plots agree with those results. Independent review of the three
acceptance JSON files remains pending; CMD validation is explicitly deferred.

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
- **Acceptance receipt review remains pending.** Fresh S1/S2 notebook board runs
  show success, but the original JSON records still need hash review. Independent
  CMD and clean-clone rebuild acceptance remain outstanding.
- **DMA framing, allocation and cache maintenance** are exercised by the returned
  S2 notebook's three board cases. The model adapter alone does not establish
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
