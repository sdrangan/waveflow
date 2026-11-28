# Plan: a kernel on port B of a BRAM window, wired at RTL

**Status: READY for the lab machine, 2026-11-28, after `plans/bram_fullrate_port.md`** (both touch the
BRAM window's generated sub-top; do them in that order, on a branch from `origin/main` once the first is
merged). Written in the cloud planning session for `docs/examples/aoa_detect` (its
[architecture](../docs/examples/aoa_detect/arch.md), *The DUT and its devices*).

**For the agent picking this up:** the same rules as `plans/bram_fullrate_port.md`: read this file and
*Read first*, work on a new branch, commit and push after each stage, no merge or PR unless asked, the
venv, the tool version next to every number, at most two hypotheses before stopping, never loosen a
tolerance.

## Why

The emitter detection example keeps its samples in a ring: a true dual-port BRAM whose **port A** is on
the AXI bus (a `MemSlaveBramWindow`, a view of the front-end device's slave adaptor; downstream units
read it over the crossbar) and whose **port B** the capture kernel writes, a word a cycle, without the
bus. Standard hardware; Waveflow has each half but not the join:

- **pysim is fine as it is.** Port B is `MemSlaveBramWindow.port_b_read` / `port_b_write`: untimed
  method calls, the intended model of a BRAM port (the data is there; the kernel's own model charges
  the cycles), like `BramIF`'s scalar accesses. Nothing in this plan changes pysim's behaviour.
- **RTL stops.** The generated system top cannot connect a kernel's HLS `bram` port to port B of the
  window's memory. `waveflow/build/system_top.py`, `_view_of`, raises *"a BRAM window shared with a
  kernel is not wired by system_top yet"*. The pieces around it exist: the adaptor generator's
  `BramView` already names the kernel's port B (`kport`, `waveflow/build/mm_adaptor_gen.py`), the
  memory's unused kernel ports are tied off (`idle_memory_ports`), and `examples/bram_access` wires
  kernels' `bram` ports to a `T2pBram` shared by two kernels.

## What to build

1. **The binding in Python.** A kernel declares its port-B access in a way the system-top walk can
   find: either the window's port B as a `BramIFSlave` endpoint that the kernel's `BramIFMaster` binds
   (`waveflow/hw/bram.py`, as a `T2pBram` is bound in `examples/bram_access`), or the window naming the
   kernel and its port. Choose in Stage 1, with the reason; keep the accesses untimed either way, and
   keep `port_b_read` / `port_b_write` working for existing code (`tests/hw/test_mm_bram.py`).
2. **The lowering.** `system_top._view_of` builds the `BramView` for a window bound to a kernel: the
   kernel's `bram` port wired to the window memory's port B inside the device's sub-top, port A to the
   slave adaptor's leaf (`mm_bram_port`, `mm_bram_port_p1`, or the full-rate leaf of the first plan).
   A window with no kernel on port B keeps today's behaviour (port B tied off).
3. **The checks the binding allows** (as `BramIF` gives `T2pBram`): the word width and depth agree,
   the access (`"write"` for the capture) matches the kernel's port, and a mismatch is refused at
   construction, not at RTL.
4. **The read-during-write hazard manifest** for the window's memory, as the wrapper's
   `bram_hazard_manifest` gives `bram_access`'s, so a traced run can be scanned with
   `waveflow.utils.bram_trace.find_read_during_write`.

## Tests and gates

| Gate | What it shows | Marker |
|---|---|---|
| Existing designs unchanged | every generated system top of the repository's tests and examples byte-identical | fast |
| The binding | a kernel bound to a window's port B: widths, depth and access checked; a mismatch refused | fast |
| A kernel writes port B, a master reads port A | a small device: a kernel writing blocks through port B while a `MemRStream` (or the host) reads them over the crossbar; every word read equals what was written, in pysim and at RTL | `-m xsi` |
| Collisions seen when made | a deliberate scenario where a read meets a write of the same address (`bram_access`'s `collision_scenario` method): the hazard scan is not empty; the normal scenario's is | `-m xsi` |
| With the full-rate leaf | the same gate with `mm_bram_port_pipe` on port A | `-m xsi` |

Add the new XSI gates to `WANT_XSI_GATES`.

## Platform files

None of its own: the window's timing records come from `plans/bram_fullrate_port.md`. If the binding
changes the window's identity (the key its records are filed under), say so and re-key the records
filed by the first plan, as `plans/dse_cg.md` re-keyed the retired core's.

## Docs to update

| Page | What changes |
|---|---|
| `docs/guide/interface/axi_mm/slave_views.md` | the BRAM window with a kernel on port B: how to bind it, what is wired |
| `docs/guide/interface/primitive/bram.md` | a window as the third kind of BRAM a kernel can bind, beside `T2pBram` |
| `docs/guide/systems/devices.md` | one line: a device's slave adaptor can hold a memory its kernel also writes directly |
| `waveflow/hw/mm_bram.py` docstring | remove "not yet joined to `T2pBram` / `BramIF`" |
| `docs/examples/aoa_detect/arch.md` | *The DUT and its devices*: the gap closed |

## Stages

| Stage | What | Done when |
|---|---|---|
| 0 | Baseline: `tests/hw/test_mm_bram.py`, `tests/examples/test_bram_access*.py`, `tests/build/test_mm_adaptor_gen.py`, and the `-m xsi` BRAM gates | pass counts recorded |
| 1 | The binding (choose its form, with the reason) and its checks | the binding gate passes; existing tests unchanged |
| 2 | The lowering and the hazard manifest | the byte-identical gate passes; the new device's top generates |
| 3 | The RTL gates | port B written, port A read, bit-exact; collisions seen when made |
| 4 | The docs | pages updated; `pytest tests/docs` passes |
| 5 | A *Record* section at the end of this file | committed and pushed |

## Read first

- `waveflow/hw/mm_bram.py`, `waveflow/hw/bram.py` (`BramIFMaster`, `BramIFSlave`, `BramIF`, `T2pBram`)
- `waveflow/build/system_top.py` (`_view_of`, `idle_memory_ports`), `waveflow/build/mm_adaptor_gen.py`
  (`BramView.kport`), `waveflow/build/wrapper_gen.py` (`bram_hazard_manifest`)
- `examples/bram_access/` and its tests (`tests/examples/test_bram_access*.py`)
- `docs/guide/interface/primitive/bram.md`, `docs/guide/interface/axi_mm/slave_views.md`
- `plans/bram_fullrate_port.md` (the leaf this one must also work with)
