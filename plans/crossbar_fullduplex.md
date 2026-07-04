# Plan — make the AXI interconnect full-duplex ("B"): the interconnect owns bus contention + timing + duplex

## Context

The matrix-LT FIR build established that **real AXI is full-duplex by construction** (independent AR/R
and AW/W channels), so a read and a write on one `m_axi` bundle never contend — half-duplex is a
*slave* property (single-port BRAM, or a DDR model capturing R/W bandwidth sharing), not the default.

Today the model gets this **right at the master, wrong at the interconnect**, and splits the timing
across two mechanisms. "B" consolidates all three bus properties — **contention, timing, duplex** —
onto the **interconnect/slave**, where they physically belong (configured once per platform, shared
across masters). The accelerator then supplies only the *access pattern* (`num_trans`, `nwords`) at
the slice call.

This supersedes `fir-cleanup.md` #3 ("move bus_rd/bus_wr to the master") — that was a throwaway
double-move; this is the real home.

## Current state (post A1–A3) — verified against `waveflow/hw/memif.py`

- **`MMIFMaster`** (`~284`) owns `read_channel` + `write_channel` (capacity-1 SimPy resources, `~313`)
  and `bus_timing: BusTiming | None` (`~317`). `BusTiming` (`~246`) maps `{num_trans, nwords}` → a
  per-direction occupancy span via calibrated `CalibModel`s; `None` ⇒ fall back to the plain
  `word_bw`-derived span.
- **`Region` slice calls** acquire the **master's** channels and read the **master's** `bus_timing`:
  `read_slice`/`write_slice` (`~650`/`~662`) and the pipelined variants
  `read_slice_pipelined`/`write_slice_pipelined` (`~676`/`~716`).
- **`MMIFSlave`** (the slave endpoint, `__post_init__` `~235`) has a single
  `self.bus = simpy.Resource(capacity=1)` (`~238`) — **shared by reads AND writes**, i.e. every slave
  is modeled half-duplex.
- **`AXIMMCrossBarIF`** (`~890`) funnels its word-mover through `slave_ep.bus.request()` on the write
  path (`~1057`, `~1070`) and the read path (`~1097`, `~1124`). **`DirectMMIF`** (`~1141`) likewise
  (`~1222` write, `~1239` read).

So there are two timing mechanisms (master channel+`bus_timing` *and* the crossbar's `slave_ep.bus`),
and contention is modeled at the master instead of the contended physical resource (the memory port).

### Blast radius (verified)
- **Only `examples/rowwise_fir/fir.py` sets `bus_timing`** (`fir.py:340`,
  `self.m_mem.bus_timing = self.timing.bus_timing(self.clk.freq)`) and uses the calibrated pipelined
  slices with `num_trans`. It is the sole consumer of the per-direction span model.
- **VMAC** (`examples/vmac/vmac.py`, `vmac_host.py`) uses plain `read_slice`/`write_slice` with **no
  `bus_timing`** → it rides the `word_bw` fallback. **This must stay byte-identical.**
- Core change is `memif.py`; `interface.py` has only incidental references.

## Design decision: channels + timing live on the **slave endpoint**

The contended physical resource is the **memory port**, so the per-direction channels and the
occupancy model live on **`MMIFSlave`**, not the master. This is the only model where **multi-master
arbitration falls out correctly** (two masters hitting one DRAM contend at that slave's read channel),
and it is the natural home for the AXI-vs-DDR fidelity caveat (a DDR slave that shares R/W bandwidth
*declares* half-duplex). For the current single-master-per-slave examples it is behavior-identical.

The master's `read_channel`/`write_channel` retire; `Region` reaches the slave's channel + span
**through the interconnect** (the crossbar resolves the slave for the address). `bus_timing` becomes a
property configured on the **platform/slave**, not poked onto a port by an accelerator.

## Environment / Vitis (READ FIRST)

**Vitis HLS 2025.1 IS installed and the toolchain auto-detects it.** Do NOT check `which vitis_hls` /
`PATH` (the unified 2025.1 flow has no PATH'd `vitis_hls`; it is `vitis-run.bat`, found by
`waveflow/toolchain/toolchain.py`). Verify:
```
PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe -c "from waveflow.toolchain import toolchain; print(toolchain.find_vitis_path())"
# -> C:\Xilinx\2025.1\Vitis\bin\vitis-run.bat
```
Run all Python via `../pysilicon-venv/Scripts/python.exe`. **Note on what needs Vitis:** the FIR
calibration **gates are computed in SIM** against the *committed* `results/cosim_grid.json` — so
"gates unchanged" is verifiable **without** re-running cosim. A cosim **bit-exact re-run** (which
*does* need Vitis) is the stronger optional check that the functional data path still works; run it if
any functional slice plumbing changes. Never soft-skip; if Vitis errors, report the actual output.

## B1 — split the slave bus into per-direction channels (full-duplex default)

**Goal:** `MMIFSlave` exposes `read_channel` + `write_channel` (independent capacity-1), full-duplex by
default; a declared-half-duplex slave re-couples them. No timing move yet.

1. **`MMIFSlave.__post_init__`** (`memif.py:~235`): replace the single
   `self.bus = simpy.Resource(capacity=1)` with `self.read_channel` + `self.write_channel`
   (capacity-1 each). Add a field `half_duplex: bool = False`; when `True`, make `write_channel`
   **alias** `read_channel` (one shared resource ⇒ R/W mutually exclude, the old behavior). Keep a
   `bus` **property** returning `read_channel` as a temporary back-compat alias **only if** anything
   outside `memif.py` references it (grep first; prefer removing it).
2. **`AXIMMCrossBarIF`** read paths (`~1097`, `~1124`) → `slave_ep.read_channel.request()`; write
   paths (`~1057`, `~1070`) → `slave_ep.write_channel.request()`. Same for **`DirectMMIF`** (`~1222`
   write, `~1239` read).
3. **Watch the FIR concurrency interaction (surface, don't absorb).** FIR's `load` (read) and `store`
   (write) are **concurrent** processes; their *functional* movements both currently pass through the
   shared `slave_ep.bus` and so serialize there. After B1 they become independent. The calibrated
   span is held on the **master** channel (still, until B2), so FIR timing *should* be unchanged — but
   **verify the gates are byte-identical**. If they move, that is a real finding (the old shared bus
   was silently contributing); report it rather than papering over it.

**B1 validation:** full non-vitis suite = `main`'s known-failing set (zero regressions); FIR gates
**0.11 / 0.14 / 0.60%** unchanged and `results/*` byte-identical; VMAC byte-identical; `ruff` clean.

## B2 — move `bus_timing` to the interconnect; route transfers through the crossbar

**Goal:** the **slave** owns the occupancy model and the channel; the `Region` slice passes
`num_trans`/`nwords` down and the **crossbar** acquires the slave's channel and holds it for the
slave's span. The master-level `bus_timing` + channels retire.

1. **Move `BusTiming` ownership to `MMIFSlave`** (configured per platform). Update `BusTiming`'s
   docstring (currently "owned by an `MMIFMaster`"). `None` model ⇒ `word_bw` fallback, **preserved
   exactly** (VMAC depends on it).
2. **Region slices resolve the slave through the interconnect.** `read_slice*`/`write_slice*` stop
   reading `self.master.bus_timing` / acquiring `self.master.read_channel`; instead they go through
   the master's `interface` (the crossbar), which decodes the address → `slave_ep`, acquires
   `slave_ep.{read,write}_channel`, and computes the span from `slave_ep.bus_timing`. Thread
   `num_trans` (and the early anchor / `min_span`) down the existing
   `read_array_pipelined`/`write_array_pipelined` → `interface.read`/`write` path so the **channel
   acquire + span hold happen inside the crossbar**, replacing the master-channel `with` blocks.
3. **Remove the accelerator poke.** Delete `fir.py:340`
   (`self.m_mem.bus_timing = self.timing.bus_timing(...)`); configure the FIR slave's `bus_timing` on
   the **platform/slave** where the memory is declared (the host/sim wiring), so the accelerator never
   touches platform bus params. `FIRTiming.bus_timing(...)` stays as the *source* of the calibrated
   models, but it's applied to the slave at wire-up, not in the component's `pre_sim`.
4. **Retire the master channels** (`MMIFMaster.read_channel`/`write_channel`/`bus_timing`/`channel()`)
   once nothing reads them.

**B2 validation (the load-bearing gate):**
- FIR gates **0.11 / 0.14 / 0.60%** unchanged; `results/{cosim_grid,fir_calibration}.json` and
  `fir_figures.py --check` byte-identical (the calibration is sim-side vs committed cosim — no Vitis).
- **VMAC byte-identical** (the `word_bw` fallback path is preserved end-to-end).
- Optional but recommended: **FIR cosim bit-exact re-run** (Vitis) to confirm the functional data path
  through the re-plumbed crossbar still matches the golden.
- Full non-vitis suite = `main`'s failure set; `ruff` clean.

## B3 — cleanup + docs + memory

1. **Docs:** update the guide pages that currently say the **m_axi port / master owns the channels**:
   `docs/guide/timing/aximm.md` and `docs/guide/timing_model/double_buffered.md` (the "Per-direction
   channel resources" bullet that attributes them to the `m_axi` port). Reframe: the **interconnect**
   owns per-direction contention + occupancy + duplex; the accelerator supplies only the access
   pattern. Add a short note that a half-duplex slave *declares* it (single-port BRAM / DDR
   R/W-sharing — the fidelity caveat's home). Link/anchor check.
2. **Memory:** update `project-memory-modeling-unification` and `project-matrix-lt-fir-build` — B's
   "interconnect owns all three" landed; record the slave-endpoint home and that `bus_timing` is now
   platform/slave-configured (the `fir.py:340` poke is gone).

## Acceptance
- `MMIFSlave` owns per-direction `read_channel`/`write_channel` (full-duplex default; `half_duplex`
  re-couples) **and** `bus_timing`; `MMIFMaster` no longer owns channels or a span model; `fir.py` no
  longer pokes `bus_timing`.
- FIR gates **0.11 / 0.14 / 0.60%** unchanged; FIR `results/*` + figure byte-identical; **VMAC
  byte-identical**; full non-vitis suite = `main`'s failure set; `ruff` clean.
- Guide pages reattribute channel/timing/duplex ownership to the interconnect; links + anchors resolve.

## Out of scope
- The free-running ring-kernel / streaming Gate-3 codegen (a separate major milestone).
- The pluggable BRAM / word-array `Region` backends (the broader unification; B is the contention/
  timing/duplex slice of it).
- Migrating VMAC to the rowwise dataflow architecture.

## Reference files
- Change: `waveflow/hw/memif.py` (`MMIFSlave`, `MMIFMaster`, `BusTiming`, `Region` slices,
  `AXIMMCrossBarIF`, `DirectMMIF`); `examples/rowwise_fir/fir.py` (drop the `bus_timing` poke) +
  wherever the FIR slave/memory is wired (apply `bus_timing` there).
- Preserve byte-identical: `examples/vmac/{vmac.py, vmac_host.py}` (word_bw fallback),
  `examples/rowwise_fir/results/*`.
- Docs: `docs/guide/timing/aximm.md`, `docs/guide/timing_model/double_buffered.md`.
