# Plan — board packaging: an example becomes IP, a bitstream, and a PynQ host

**Status: SCOPE, not started.** 2026-09-20. This is the plan for **Flow 4** —
`docs/guide/flows/bitstream_ipi.md` has been a `nav_exclude` stub naming it since the flows
restructure, and `docs/future/vivado_backend.md` has pointed at a `plans/vivado_plan.md` that was
never written. This file is that plan.

**Who this is written for.** Someone other than its author, with a board and a Vitis/Vivado
install, working **one stage at a time**. Each stage below is one PR (many commits), lands green,
and is independently useful. A stage that cannot be finished should be *cut at a rung boundary*
(§3) rather than left half-wired.

**Target board:** RFSoC 4x2 (RealDigital), `xczu48dr-ffvg1517-2-e`. **Host:** PYNQ.

---

## 1. What this adds, and what "done" means

Today a Waveflow example ends at **RTL that has been proven** — csynth, cosim, and for several
examples an XSI gate asserting an exact cycle count. Nothing downstream of that exists. This plan
carries five examples the rest of the way:

> generated kernel → **Vitis IP** → **Vivado block design** → **bitstream** → **PynQ host code that
> drives it on the 4x2**

"Done" for a stage is not "it built". It is: **a clean clone on a different machine rebuilds the
bitstream from committed sources, and the committed host script drives the board to the same answer
the Python simulation gives.** That acceptance test is stated in `plans/rfsoc_4x2_bringup.md` and is
not optional — export scripts essentially always carry one absolute path or one missing board file,
and that only surfaces on a clean clone.

---

## 2. What exists today (cited from code, so this plan can be checked)

**The build DAG is the right host for all of this.** Every example already has a `*_build.py`
defining `BuildStep` subclasses with `consumes` / `produces` / `params`, assembled into a `BuildDag`
and driven by `run_dag_cli` (`--through STEP`, `--status`, `--force-step`). New rungs are new steps
on the existing DAG; nothing about the driver needs to change.

**The csynth TCL emitter is one line short of exporting IP.**
`waveflow/build/composite_gen.py::render_tcl` emits `open_project` / `set_top` / `add_files` /
`open_solution` / `set_part` / `create_clock` / `csynth_design`. It does **not** emit
`export_design`. `export_design` appears nowhere in `waveflow/`.

**The default synthesis part is not the board.** `DEFAULT_PART = "xc7z020clg484-1"`
(`composite_gen.py:2668`) — a Zynq-7020. `RFSOC4X2_CLK_HZ = 250e6` / `RFSOC4X2_PERIOD_NS = 4.0`
exist beside it, and `tcl_target(config)` will use a resolved platform's part/clock when there is
one, but the only platform in the tree is `waveflow/calib/platforms/zynq7020_bfm_100mhz`. **An IP
exported today would be built for the wrong device.** This is why S0 exists.

**`Platform` is a calibration platform, not a board.** `waveflow/calib/platform.py::Platform` is
`name` / `dir` / `part` / `clk_freq` / `res_types_stored` — a directory of measurements plus the
part and clock those measurements are valid for. It has no `board_part`, no board-files provenance,
no PS preset, no address map. Its `resolve()` is a **create-or-confirm gate** on part/clock, which
is exactly the hook S0 needs.

**The toolchain layer knows where Vivado is but never runs it.**
`waveflow/toolchain/toolchain.py` has `find_vivado_path()` and `run_vitis_hls()`. There is no
`run_vivado()`.

**Two loose pre-DAG scripts already do fragments of this work.**
`waveflow/scripts/collect_overlay.py` globs for a `.bit` under `impl_1` plus the matching `.hwh` and
copies them into `overlay/`. `waveflow/scripts/build_lab_autograder.py` packages a lab for
Gradescope. Both predate the DAG, neither is wired into it, and `collect_overlay.py` is superseded
by S0's `CollectOverlayStep`.

**The `bram_access` glue top is already generated.** `waveflow/build/wrapper_gen.py::render_wrapper`
emits a Verilog module instantiating the Vitis kernel *and* the hand-written memory
(`waveflow/build/rtl/bram_t2p.v`), joined — and that wrapper is already the elaborated top the XSI
gate runs. S4 is therefore **not** "write the glue"; it is "package a mixed IP".

**The regmap models the Vitis layout but nothing enforces that it does.**
`waveflow/hw/regmap.py::VitisRegMap` documents the verified layout (`0x00` packed control word,
`0x04` GIER, `0x08` IER, `0x0c` ISR, user fields from `0x10` on an 8-byte stride) and exposes
`offset_of` / `nwords_of` / `field_name_at_offset` / `total_size_bytes`. Its own docstring says:
*"Nothing yet enforces that this layout tracks Vitis; control.h is the authoritative artifact a
conformance test could check against."* These offsets were **wrong** once already (`ap_done` was
modelled at `0x04`, which is really GIER) and it went unnoticed for months because the offsets are a
closed loop inside the Python simulation — no generated C++ contains an offset, and there was no
firmware path. **This plan creates the firmware path**, so the conformance gate stops being optional
(§6.3).

**PYNQ can be driven off-board.** Verified 2026-08-22, pynq 3.1.2 on Windows 11 with no board and no
XRT: `Device.devices` returns `[]` with a warning rather than raising. `DeviceMeta` auto-registers
any subclass defining `_probe_`, and `capabilities = {"REGISTER_RW": True, "MEMORY_MAPPED": False}`
reduces the whole hardware surface to `read_registers` / `write_registers`. The in-tree template is
`pynq/pl_server/remote_device.py`. Install traps, in the order they bite: no win_amd64 wheel (source
build, ~10 min); `grpcio` has no cp314 wheel so use `pip install --no-deps pynq pynqmetadata
pynqutils`; needs `setuptools<=80`; install to a **short** path or `WinError 206`.

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

Two new pytest markers go in `pyproject.toml` beside the existing `vitis` / `xsi`:

```
"vivado: marks tests as requiring a Vivado installation (block design / bitstream; slow)",
"board: marks tests as requiring a physical RFSoC 4x2 (manual, opt-in, never in CI)",
```

`board` tests are **opt-in only** — they must not run under a bare `pytest`, and they must fail
loudly rather than skip silently when the board is expected (the `xsi` staleness lesson: a gate that
skips quietly is a gate that is not there; see `plans/xsi_staleness_and_silent_skips.md`).

---

## 4. Decisions already taken (do not relitigate without a reason)

**4.1 Artifacts: commit the `.hwh`, not the `.bit`.** PYNQ needs the `.hwh` and it is small; a 4x2
bitstream is tens of MB and five of them in git history is permanent. So:

- `examples/<name>/board/overlay/<name>.hwh` — **committed**.
- `examples/<name>/board/overlay/manifest.json` — **committed**: Vivado version, part, board-files
  version, the source commit, and the **sha256 of the `.bit`**.
- `<name>.bit` — **not committed**; published as a GitHub Release asset named
  `<name>-rfsoc4x2-<vivado-version>.bit`. `examples/*/board/overlay/*.bit` goes in `.gitignore`.

The manifest is what separates *"my rebuild broke"* from *"the board is misconfigured"*: rebuild,
hash, compare.

**4.2 The PynQ driver is generated from the `VitisRegMap`.** A hand-written script per example would
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

**No example. Framework only. Everything else depends on it.**

**Why first:** the default part is a Zynq-7020 (§2). Without a board object, five examples each
hard-code the 4x2 and the sixth diverges.

**5.1 New `waveflow/board/` subpackage.** A `Board` dataclass — a *build target*, distinct from
`Platform`, which is a *directory of measurements*:

```python
@dataclass(frozen=True)
class Board:
    name: str                  # "rfsoc4x2"
    part: str                  # "xczu48dr-ffvg1517-2-e"
    board_part: str | None     # the Vivado board_part string, if board files are installed
    board_files: str           # provenance: vendor + version (RealDigital, vX.Y)
    default_clk_hz: float      # 250e6 — RFSOC4X2_CLK_HZ
    ps_preset: str | None      # the PS configuration preset the BD templates apply
```

with `Board.rfsoc4x2()` as the one concrete instance. `RFSOC4X2_CLK_HZ` / `RFSOC4X2_PERIOD_NS` in
`composite_gen.py` become derived from it rather than stated twice.

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

**5.5 New `waveflow/build/board_steps.py`** — the steps every stage draws from:

| Step | Produces | Rung |
|---|---|---|
| `ExportIpStep` | the exported IP dir/zip + its pinned VLNV | 3 |
| `HostDriverGenStep` | `board/host/<top>_driver.py` from the `VitisRegMap` | 2 |
| `BdGenStep` | `board/bd/create_bd.tcl`, rendered from the stage's template | 1 |
| `BdValidateStep` | a `validate_bd_design` verdict, **no synthesis** | 4 |
| `BitstreamStep` | `.bit` + `.hwh` | 5 |
| `CollectOverlayStep` | `board/overlay/` + `manifest.json` (retires `collect_overlay.py`) | 5 |

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

The smallest possible closed loop, and the stage that carries most of the framework. Budget
accordingly: S1 is not 1/5 of the work.

**The design.** `SimpFun` is `HostActivated` with a single `VitisRegMapMMIFSlave` carrying
`x` / `a` / `b` in and `y` out. No streams, no memory, no DMA.

**The block design.** Zynq UltraScale+ PS → AXI SmartConnect → the exported IP's `s_axi_control`.
One clock, one reset, one address-map assignment. This is the smallest 4x2 BD that does anything.

**6.1 The host.** `board/host/simp_fun_driver.py`, generated: field offsets from `offset_of`, widths
from `nwords_of`, field packing from each `RegField`'s type, and `start()` / `done()` / `wait()`
against the **bits of the control word at `0x00`** (not registers of their own — the mistake the
model itself once made). On top of it, a hand-written `board/host/run_simp_fun.py` that writes
`x`/`a`/`b`, starts, polls, reads `y`, and compares against the same `DEFAULT_VECTOR` the DAG's
`BuildInputsStep` uses. **Same vector, three places** — pysim, cosim, board.

**6.2 `HostDriverGenStep` design note.** The driver must not import `pynq` at module scope; it takes
a backend object exposing `read_registers` / `write_registers` (§12). That is what lets rung 2 run
on any machine.

**6.3 The `control.h` conformance gate — the debt this plan pays.** A new `-m vitis` test parses
`<top>_proj/solution1/.autopilot/db/coregen/control.h` for its `ADDR_*` defines and compares them,
name by name, against `VitisRegMap.offset_of`. Any mismatch fails.

This is the test `VitisRegMap`'s own docstring asks for, and until now there was no reason it had to
exist: the offsets were a closed loop in the simulator, with the model on both ends. **The generated
driver breaks that loop** — it puts a Waveflow-computed offset on a real AXI bus — so from S1 onward
a wrong offset is a wrong read on hardware, not a harmless internal convention.

**Known trap, and it lands in S2 rather than here:** `VitisRegMap`'s docstring warns that its
multi-word stride rule is verified for 32-bit scalars only, and that **Vitis maps array arguments on
`s_axilite` as a BRAM-backed region, which the model does not reproduce.** `simp_fun` has only
scalars, so S1 is safe — `PolyAccel`'s `coeffs` array is not. Write the gate here; expect it to fire
in S2.

**Gates:** rungs 1–6. Rung 6 = `y == a*x + b` read back over PYNQ.

**Done when:** a notebook on the 4x2 computes `y` through the generated driver, and the conformance
test is green.

**Rough effort:** ~1 week (most of it framework, not this example).

---

## 7. Stage S2 — `examples/stream_inband`: streams via DMA

**The design.** `PolyAccel` is `HostActivated` with `s_in` (`StreamIFSlave`), `m_out`
(`StreamIFMaster`), and a regmap carrying `coeffs` plus status fields. The command rides **in band**
on `s_in`.

**The block design.** PS → SmartConnect → AXI DMA (MM2S + S2MM) → the IP's AXIS ports, plus the
AXI-Lite control path from S1. The DMA's own control slave joins the address map.

**7.1 The one thing most likely to hang the board: TLAST.** An AXI DMA **S2MM channel does not
terminate without TLAST**. This repo's standing law is that boundary TLAST comes from `ap_axis` —
the internal `framed_word` is internal only, and the flag that selects it must be a `ClassVar`. So
before any DMA work: confirm `PolyAccel`'s `m_out` lowers to an `ap_axis` boundary port that drives
TLAST at end of transfer. If it does not, that is the first commit of this stage, and it is a
codegen change, not a TCL change.

A hang here looks exactly like a host bug. Check TLAST in the VCD before suspecting the host.

**7.2 Expect the `coeffs` array to break the conformance gate.** See §6.3. Resolve it by making the
model match what `control.h` actually says — not by relaxing the gate.

**7.3 The host.** `pynq.allocate` for the input and output buffers, `dma.sendchannel` /
`dma.recvchannel`, with coefficients written through the generated driver first. The in-band command
header is built with the example's existing schema — **do not hand-pack it**; use the `DataSchema`
serializers (this is a recurring failure mode in this repo, and it hides at width 1).

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
the board as-is; `blk_mem_gen` is the alternative. Staying with the committed `.v` keeps the board
design and the XSI gate on the *same* RTL, which is worth more than using the vendor generator.

**Gates:** rungs 1–6, with rung 6 exercising the full address range.

**Rough effort:** ~1–1.5 weeks.

---

## 10. Stage S5 — `examples/rf_shot_tx`: the converter

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
(`examples/rf_shot_loopback`, the most recent commit on `main`) is the natural target. **Scope S5
deliberately:** either stop at rung 5 (a bitstream that builds and loads, with the DAC verified on a
scope) and open S6 for the loopback measurement, or plan for both halves up front. Do not discover
this at the end.

**Rough effort:** ~2 weeks, plus lab time.

---

## 11. Per-example repo layout

```
examples/<name>/
  board/
    bd/create_bd.tcl           # committed, de-absolutized, parameter header at the top
    constraints/<name>.xdc     # board pins (S5 only; earlier stages have none)
    scripts/build.tcl          # create project -> IP repo -> BD -> wrapper -> bitstream
    ip/                        # generated: the exported Vitis IP (gitignored)
    overlay/
      <name>.hwh               # COMMITTED
      manifest.json            # COMMITTED -- versions, part, board files, commit, .bit sha256
      <name>.bit               # gitignored; a Release asset
    host/
      <top>_driver.py          # GENERATED from the VitisRegMap
      run_<name>.py            # hand-written; runs on board or against pysim
      <name>.ipynb             # hand-written notebook (optional)
  README.md                    # + "On the board" section: how to build, how to run, what to expect
```

The `build.tcl` starts life as `write_project_tcl -no_ip_version -force` output, de-absolutized.
**Never commit the Vivado project tree** (`.xpr`, `.runs/`, `.cache/`, `.gen/`, `.hw/`, `.sim/`,
`.ip_user_files/`) — all regenerated, huge, path-dependent.

---

## 12. Off-board host execution

`waveflow/board/emu.py`: a `pynq.pl_server.device.Device` subclass with `_probe_`,
`_probe_priority_`, and `capabilities = {"REGISTER_RW": True, "MEMORY_MAPPED": False}`, whose
`read_registers` / `write_registers` route into a running pysim `Simulation` of the DUT. Copy the
method list from `pynq/pl_server/remote_device.py` rather than inventing one — it is structurally the
same thing over a different transport.

`REGISTER_RW` alone covers S1. S2/S3 additionally need `allocate()`, which is where this gets
harder — a simulated DMA buffer has to be reachable from the model's memory. **Scope honestly:**
S1's host runs off-board on day one; S2/S3 off-board support may lag by a stage, and that is
acceptable as long as it is stated rather than quietly dropped.

---

## 13. Environment manifest (the part everyone forgets)

Recorded once in `docs/` and repeated per-overlay in `manifest.json`:

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
- **`allocate()` in the emulated device** (§12) is the least-solved piece of this plan.
- **The `coeffs` array offsets** (§7.2) are a known unknown that the conformance gate will expose.
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
- `plans/resource_model.md` — the wrapper as the first scope an area number can be defined against;
  S4's IP is that scope made physical.
- `plans/xsi_staleness_and_silent_skips.md` — why a skipped gate is a missing gate (§3).
- `docs/guide/flows/bitstream_ipi.md` — the Flow 4 stub this plan fills in; un-hide it
  (`nav_exclude`) when S1 lands.
