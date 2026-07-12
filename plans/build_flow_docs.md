# Plan: Build-flow docs — extend `docs/guide/build/` (one flow; single-kernel + composite/XSI)

> Companion to [`concurrency_docs.md`](./concurrency_docs.md). Records the decision that the
> synth/sim/XSI story is **not** a new "Verification" section — it is the **build flow**, and it extends
> the existing `docs/guide/build/` section. Write after the interleaver is stable (it is, PR #106); the
> XSI/BFM pages can be written now since the harness is built + verified.

## Decision: ONE build flow, extend `build/` (no parallel section)

`docs/guide/build/` already IS the build flow — [`build/index.md`](../docs/guide/build/index.md)'s poly
DAG runs schema → codegen → pysim → csim → csynth → cosim → timing, and
[`build/vitis.md`](../docs/guide/build/vitis.md) is "what step performs csim/csynth/cosim." So:

- **Do not** create a `verification/` section (the title was wrong anyway — the content is the
  *process* of synthesizing/simulating, not functional/timing *checking*).
- The flow is **identical through the whole front half** (schema headers → streamutils → codegen →
  pysim golden) and **forks at exactly one rung — the RTL-exercising rung — by execution model:**

  | top kind | RTL rung |
  |---|---|
  | single kernel (`ap_ctrl_hs` / regmap-launched) | **Vitis cosim** (`build/vitis.md`) |
  | composite (`ap_ctrl_none` free-running task network) | **XSI/BFM** (`ap_ctrl_none`+`m_axi` can't cosim) |

  It is one flow whose last rung has two realizations — NOT two flows. The **verification ladder**
  (pysim → csim → csynth → cosim | XSI) is the fidelity axis of that one flow. Ladder table already
  drafted in [`plans/component.md`](./component.md) ("The verification ladder").

- **Composite top *codegen* does NOT live in `build/`** — it is generation, so it stays in the
  concurrency section (`concurrency/hls/codegen.md`: `MemStreamStep`, `KernelTask`,
  `composite_top_spec`), the multi-kernel analog of `comp_codegen/structure.md`. `build/` *drives* what
  codegen produces; `build/xsi.md` cross-links `concurrency/hls/codegen.md`.

## New pages under `build/`

```
docs/guide/build/
  index.md    + "one flow, ladder, forks at the RTL rung by execution model" framing + ladder table
  tcl.md      NEW — authoring run.tcl (the single-kernel gap: vitis.md treats it as a black box)
  xsi.md      NEW — the XSI/BFM rung: from-zero primer + the flow (for the composite)
  bfm.md      NEW — how to WRITE the BFM testbench (the one hand-authored artifact)
```

### `tcl.md` — authoring `run.tcl`
The gap in the current docs: `vitis.md` treats `run.tcl` as given. Show its contents:
`open_project` / `add_files` (kernel + tb) / `set_top` / `create_clock` / `csim_design` /
`csynth_design` / `cosim_design`, and the `COSIM` env branch that `CSimStep`/`CSynthStep` toggle.
Shared by both single-kernel and composite (drives every Vitis rung).

### `xsi.md` — the XSI/BFM rung, FROM ZERO  *(hard requirement: define every term for a reader who has never heard them)*
Motivation first: after csynth you have **RTL (Verilog)** and want to *run* it vs the pysim golden; for
`ap_ctrl_none`+`m_axi` Vitis cosim refuses → you drive the RTL yourself. Then define, plainly:

- **RTL / Verilog** — synthesized cycle-by-cycle hardware description (`…/solution1/syn/verilog/*.v`).
- **xsim** — Vivado's RTL simulator (computes every wire each clock tick).
- **xvlog / xelab** — compile the Verilog into a runnable sim; `xelab -dll` → a loadable `.dll`.
- **XSI (Xilinx Simulator Interface)** — a **C++ API** to that `.dll`: set a pin, tick the clock, read a
  pin — i.e. drive the RTL sim from C++ instead of a Verilog testbench (`xsi_loader`:
  `put_value`/`run`/`get_value`).
- **BFM (Bus Functional Model)** — C++ that plays the *other end* of the kernel's AXI-MM + AXI-Stream
  buses cycle-by-cycle (pretends to be memory + the in/out streams). = the testbench at wire level.
- **`.f` file** — a plain text manifest: one Verilog path per line, fed to `xvlog -f`. It lists the
  csynth-generated `.v` files (see `rtl_interleaver_canon.f`).

The flow in one line: **csynth → Verilog → `xvlog` → `xelab -dll` (→ `.dll`) → BFM (C++) loads it via
XSI and plays memory+streams against the pins, checking vs the golden.** `run.bat` chains the four
commands + sets the Windows PATH (note: `MSYS_NO_PATHCONV`, invoke as `.\run.bat`; the
clock-LOW handshake-sampling gotcha — memories `reference-systemc-xsim-windows-xsi`,
`project-xsi-aximm-bfm-harness`).

**Supplied vs. generated table (answers "how do I write the `.f` file?"):**

| artifact | who makes it | authoring |
|---|---|---|
| `…/syn/verilog/*.v` | Vitis csynth | fully generated — never touched |
| `rtl_*.f` | you today / a build step tomorrow | *just the list of those `.v`* (≈ `ls syn/verilog/*.v`) — NOT real authoring; a future `XsiStep` globs + writes it |
| `xsi_loader.*`, `xsi_shared_lib.h`, `run.bat` | boilerplate | copied per project; only Vivado-version paths edited |
| **`*_bfm_tb.cpp`** | **you — the real work** | the one hand-authored file → its own page (`bfm.md`) |

**Automation honesty:** rungs 1–2 (csim/csynth/cosim) are BuildDag steps; the XSI rung is currently a
standalone `run.bat`, **not** a BuildStep. Future = an `XsiStep` (globs the `.f`, invokes
xvlog/xelab/g++, runs the BFM). Do not imply parity.

### `bfm.md` — writing the BFM testbench (the hand-authored artifact)
The AXI-MM + AXI-Stream cycle-driving pattern from `interleaver_canon_bfm_tb.cpp`: modeling memory
behind `gmem0`/`gmem1` (flat array, base 0), feeding the command / capturing output over AXIS, the
handshake rules, sampling in the **clock-LOW** phase, recording per-job completion cycles → steady-state
period. Frame the BFM as the cycle-level analog of the single-kernel sequential C++ testbench.
Forward-pointer: the BFM is generatable from the component `boundary` port list (future) — the biggest
automation target on this rung.

## Sequencing / open

1. `xsi.md` + `bfm.md` are writable **now** (harness built + XSI-verified, PR #106).
2. `tcl.md` writable now (single-kernel flow long stable).
3. `build/index.md` ladder framing — after the above so it links to real pages.
4. Cross-link `concurrency/hls/codegen.md` ↔ `build/xsi.md` (codegen produces / build drives).
- [ ] Decide `xsi.md` + `bfm.md` (two pages) vs one long page — leaning two, per user ("even if it
      requires another page").
- [ ] Whether the ladder gets a dedicated `build/ladder.md` or lives in `index.md` (leaning index).

> Note: untracked plan; touches no tracked files.
