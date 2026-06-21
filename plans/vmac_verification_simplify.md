# VMAC verification cleanup — converge to the histogram anatomy

**Status: IMPLEMENTED (2026-06-21) on branch `vmac-verification-simplify`. Goal: make VMAC
verification as simple as the histogram example.**

Done (4 commits; sim_timeline.json byte-identical throughout; full Vitis `topgen_cosim` of the
auto-extracted top passes **bit-exact**; non-vitis suite = the known 15-failure baseline, no new
failures):

1. **`MMIFMaster.region` / `Region`** (`waveflow/hw/memif.py`) — element-coordinate view (the sim
   twin of the C++ `read_array_slice`); the framework owns the element→byte conversion
   (`_word_bytes` consults `byte_addressable`).
2. **VMAC sim** rewired through the region; **`_byte_addr` deleted**.
3. **One golden** — `execute_mem` / `_region_idx` / `_writeback` deleted; the flat-memory image
   golden is now harness plumbing in `examples/vmac/vmac_golden_mem.py::apply_golden` (verbatim
   relocation), wired into the four vector generators. The accelerator's golden surface is just
   `execute` — the histogram anatomy.
4. **Host** (`vmac_host.py`) A/B writes + Y reads go through a `Region` too (same antipattern
   retired).
5. **Docs** — `Region` documented in `docs/guide/interface/aximm.md`.

Remaining / "more cleanup" candidates the user flagged: the broader memory-backend unification
(`plans/...` + memory note `project-memory-modeling-unification`); a fully *declarative* operand
spec so even `apply_golden` becomes generic; folding `_byte_addr`-style elem→byte fully out of any
remaining hand-rolled call sites; a strided `read_array_slice` for `row_stride != n_cols`.

The user will add corrections — treat the "Open questions / user corrections" section as the live
edit surface.

## The objection (why this exists)

VMAC currently makes the accelerator author write **two** goldens:

- `VmacAccel.execute(cmd, a, b, alpha)` — the pure-math golden (operand arrays in, dst out).
- `VmacAccel.execute_mem(cmd, mem)` — a flat-memory twin: read operands out of a memory image,
  call `execute`, write the dst back into the image. Used by `vmac_topgen.py`, `vmac_build.py`,
  `vmac_cosim_sweep.py`, `vmac_cosim_stage3.py` as the expected-image generator.

The histogram (`examples/shared_mem/hist.py`) makes the author write **one** golden
(`golden_counts`) and verifies — sim *and* cosim — by deserializing the kernel's output region
and comparing to that one golden. The memory marshalling is done by **generic, schema-driven
helpers**, not a hand-written memory golden:

- sim: `HistController` lays out the regions, runs the timed sim, reads counts back; `run_sim`
  compares `ctrl.counts` vs `golden_counts(...)` (`hist.py`).
- cosim: `hist_build.py` writes inputs with `_write_array_file(... Float32 ...)`
  (`hist_build.py:544`), reads the result with `read_uint32_file_array` / `HistResp.read_uint32_file`
  (`hist_build.py:551`), and compares to `np.bincount` (`hist_build.py:196`). **No `golden_counts_mem`.**

So `execute_mem` is avoidable boilerplate: it hand-rolls serialization the framework already owns
(`write_array` / `read_array` / `read_uint32_file`). For "a module that multiplies two arrays,"
asking every user to write `execute` **and** `execute_mem` + the image plumbing is too much.

## The target anatomy (the histogram pattern, made canonical)

> **The author writes one functional golden.** A reusable controller/harness marshals memory
> from the command's region descriptors using generic schema serialization. The test
> deserializes the kernel's output region and compares to the one golden.

Three roles, none of which is a second author-written golden:

1. **Functional golden** — `execute(cmd, operands…) -> dst`. The only thing the author writes.
2. **Memory marshalling** — generic, driven by the command's region/stride descriptors + the
   existing `DataSchema` serialize/deserialize. Lives in the harness/controller, shared across
   accelerators.
3. **Compare** — deserialize the kernel's (or sim's) output region → compare to `execute`.

## What VMAC actually needs (the genuine residue)

VMAC is harder than histogram in exactly one way: its regions are **strided / aliased / indirect**,
and the output format is **derived**:

- `_region_idx`: `addr + i·row_stride + j` (row-major, columns unit-stride).
- `ab_eq`: B aliases A.
- indirect `alpha`: per-row column read from memory.
- output element format derived (`output_format` / `derived_shift`), not in the command.

**But every one of those facts is already declared in `VmacCmd`** (the `a`/`b`/`y`/`alpha`
`addr`+`stride`+`direct` fields). So a generic harness can read the layout *from the command* and
marshal with array serializers — it does not need a hand-written adapter. The only framework gap
is a **strided** array read/write helper (histogram's regions are contiguous, so it never needed
one). That helper is a one-time, accelerator-agnostic capability.

## Proposed changes

### Framework (one-time, benefits everyone)
**Most of this already exists on the synth side — the gap is the sim twin.** The
element-coordinate, packing-aware slice/lane family is built and documented in C++
(`read_array_slice<W>(mem, i0, i1, x)`, `read_array_lane`; see
`docs/guide/custom_hooks/patterns.md` and `docs/guide/vectorization/hls/raw.md`). VMAC's hook
already uses it (`vmac_compute_impl.tpp:37-41`). It takes **element coordinates** and hides the
element↔word packing (PF / `elem_to_word`).

The Python sim `MMIFMaster`, by contrast, has only the **byte-address** `read_array` /
`read_array_pipelined` — so the accelerator hand-converts element→byte via `_byte_addr` before
calling. That asymmetry is the smell.

- **Add the Python sim twin of `read_array_slice` / `read_array_lane`** to `MMIFMaster` — element
  coordinates, packing handled internally (the element type already knows `nwords_per_inst`; the
  interface already knows `byte_addressable`). Same element-coordinate contract as the C++, so sim
  and synth read identically and `_byte_addr` is deleted.
- Strided (`row_stride != n_cols`) stays deferrable: VMAC's standing assumption is contiguous, and
  the lane loop already walks a running pointer, so v1 needs only the contiguous slice + the
  indirect `alpha` single-element read (which is exactly what the C++ `read_array_slice` already
  does for alpha).
- **Optional sugar:** a thin `Region(base=data_base, elem_type=…)` view binding the byte base so
  callers pass region-relative element indices (the PynQ-`allocate` analogue). Layered on the
  element-coordinate slice method — not a replacement for it.

### VMAC
- **Delete `VmacAccel.execute_mem`.** Keep `execute` as the sole golden.
- **`_region_idx` and `_writeback` die with it** — they are used *only* by `execute_mem`
  (`_region_idx` at `:418`/`:421` and inside `_writeback` at `:282`; `_writeback` only at `:427`).
  The strided-index math `addr + i·row_stride + j` is exactly the low-level memory addressing the
  framework's generic (strided) serializer should own, keyed off the command's `addr`/`row_stride`
  descriptor + element type. It does **not** get re-homed onto the accelerator — it leaves entirely.
- **Key simplification — the strided helper may not even be needed for v1.** The timed/synth path
  (`vmac_compute`) does **not** use `_region_idx`: it assumes contiguous (`row_stride == n_cols`,
  the standing assumption) and reads each operand as one whole-matrix block via
  `read_array_pipelined`. Only the untimed `execute_mem` ever exercised the strided generality. So
  under the standing assumption the **existing generic `read_array`/`write_array` already suffice**;
  `write_array_strided` becomes a deferrable extension, needed only when a command sets
  `row_stride != n_cols`.
- **`_byte_addr` (`:523`) — fold into the interface, don't keep on the accelerator.** It is the
  elem→byte conversion used by every *timed* read + the Y writeback. See "Addressing: element
  indices, not bytes" below — it should become an endpoint capability, after which `_byte_addr`
  is deleted too.
- Move the operand-read / Y-writeback (currently inside `execute_mem`) into the **generic harness**,
  driven by `VmacCmd`'s region descriptors + the generic serializer.
- Rewrite the four callers to use it:
  - `vmac_build.py` (`:331`, `:662`, `:944`, `:983`) — build the input image generically; read the
    Y region back and compare to `execute`.
  - `vmac_topgen.py` (`:144`) — expected-vector generator: `execute` + generic serialize.
  - `vmac_cosim_sweep.py` (`:136`), `vmac_cosim_stage3.py` (`:136`) — same.
- Sim side: the timed `vmac_compute` already delegates math to `execute`; the sim test should
  deserialize the sim's Y region and compare to `execute` directly (drop any `execute_mem`-as-golden
  in the sim path).

### Net author-facing result
Writing a new VMAC-like accelerator becomes: **one `execute` golden + the synthesizable shell/hook**
— the same surface the histogram asks for. No `*_mem` twin, no per-accelerator image plumbing.

## Addressing: element indices, not bytes (move the conversion into the interface)

**What the addresses are today.** `cmd.*.addr` is a **region-relative element index**, not a byte
address. `_byte_addr(elem_idx) = data_base + elem_idx · _elem_bytes` adds the data-region base (data
sits after the command ring) and scales element→byte by `_elem_bytes = nwords_per_inst · (mem_bw/8)`.
The host (`vmac_host.py:76`) and the generated C++ agree — the `.tpp` passes `cmd.a.addr` /
`cmd.a.row_stride` (element units) straight to the kernel (`vmac_compute_impl.tpp:219`), because HLS
`m_axi` is a typed pointer indexed by element.

**Keep element indices in the command (recommended), do not switch to byte addresses.** Reasons:
- HLS `m_axi` addresses by element index; element-indexed is the synth-natural representation.
- It is **width-agnostic** — the same command works at `mem_bw = 32` or `64`. Byte addresses bake
  the width in (an address computed for 32-bit words is wrong at 64-bit) and still need
  re-scaling by `elem_bytes` *inside* the kernel for strided access, so they don't remove the
  conversion — they push it into the datapath.
- The PynQ/host byte-address view is a **boundary** concern: the host owns the buffer base; the
  command indexes into it by element. (See the open decision below for the byte-address alternative.)

**The real problem: the byte-addressed assumption is duplicated and sim-only.** The interface already
owns byte-vs-word addressing — `AXIMMCrossBarIF.byte_addressable` + `_word_step()` (`mem_bw/8` byte,
`1` word). But `_byte_addr` re-hard-codes `· (mem_bw/8)` independently, and the **SimPy side is the
only place that converts to bytes at all** (the C++ stays in element index). So today the byte
assumption is asserted twice and creates a sim-vs-synth representation mismatch.

**Fix — mirror the EXISTING C++ `read_array_slice` element-coordinate contract on the sim master.**
This already exists on the synth side; the sim side is what's missing (see "Framework" above). The
Python `read_array(element_type, count, addr)` already has the element type and word width — the gap
is only that it takes a *byte* address instead of *element coordinates*.
- Add the **element-coordinate** `read_array_slice` / `read_array_lane` twin to `MMIFMaster`; the
  interface does elem→byte using its **own** `byte_addressable` knowledge + the element type's
  packing. A thin `Region` bound to `data_base` is optional sugar.
- **Delete `_byte_addr`.** The byte-addressed assumption then lives in exactly one place (the
  interface), and the SimPy model indexes by element just like the generated C++ — removing both the
  duplication and the sim/synth representation mismatch (the sim becomes symmetric with the hook).
- This is the "abstract low-level memory ops into the framework" principle applied to addressing —
  the same one that moves `_region_idx` out of the accelerator.

## Open questions / user corrections (LIVE — user is adding to this)

- **Command addressing basis — element index (recommended) vs byte address.** Element-indexed is
  width-agnostic + synth-natural (above); byte-address would match a raw PynQ-host view but bakes in
  `mem_bw` and pushes scaling into the kernel. Decide before reworking the endpoint API.
- **Endpoint shape for the elem→byte fix — RESOLVED toward mirroring the existing C++ idiom.** The
  synth side already standardized on free calls taking element coordinates
  (`read_array_slice(mem, i0, i1, x)` / `read_array_lane`), so the sim master should get the matching
  element-coordinate method (symmetry + zero new concepts). A `Region(base, elem_type)` view is
  optional sugar on top (carries `data_base`, the PynQ-`allocate` analogue), not the primitive.
  Open sub-question: ship the `Region` sugar in v1, or just the bare element-coordinate method first?

- _(user to fill in additional corrections — the goal is histogram-level simplicity; flag anything
  above that still asks more of the author than `golden_counts` does.)_
- Does the cosim compare at the **image level** (diff whole/Y-region bytes) or the **array level**
  (deserialize Y → compare to `execute`)? Array-level is simpler and matches histogram; confirm
  nothing downstream (figures, burst-count checks) needs the full expected image.
- Is the **derived output format** (`output_format`/`derived_shift`) something the generic
  deserializer can pull from the command/accel, or does it still need an accel-specific hook?
  (If the latter, that's the one unavoidable VMAC-specific bit — keep it tiny.)
- Keep `execute_mem` deprecated-but-present for one step, or delete outright? (Single-PR,
  multi-commit: probably one commit adds the generic path, the next deletes `execute_mem` + retargets
  callers.)
- Does this become the documented **canonical anatomy** (a `custom_hooks` / example-authoring guide
  update) so the next accelerator copies histogram, not VMAC?

## Beyond this cleanup — unify the memory-backend model (future, keep in mind)

This cleanup is one slice of a larger seam: Waveflow currently has **at least three scattered ways**
a Python model touches memory, and they should converge behind the *same* element-coordinate
interface (the `read_array_slice`/`lane` idiom + the optional `Region` view):

1. **AXI-MM** — `MMIFMaster` over a crossbar → lowers to `m_axi` (burst/streaming, byte-addressed).
2. **Raw word array** — a `Words` / word array (the golden/harness "just read from an array of
   words" case).
3. **BRAM / scratchpad** — `MemComponent` + a hand-wired `DirectMMIF` → should lower to
   `bram` / `ap_memory` (single-cycle random access).

**Goal:** one element-coordinate access (`region.read(i0, i1)` / lane loop) over a **pluggable
backend** — AXI, BRAM, or a plain word array — each **synthesizable to the right HLS port** (`m_axi`
vs `bram`/`ap_memory`). Then the golden harness, the timed sim, and a BRAM kernel all share one idiom
instead of three hand-wired styles. Not in scope for the VMAC verification cleanup; the `Region` /
sim-`read_array_slice` work here should be designed so a BRAM/word-array backend can slot in behind
the same interface later (don't bake AXI-only assumptions into the `Region`).

## Coordination

- Independent of `plans/vmac_cleanup.md` (that's the *deployable C++ kernel* complex-typing cleanup —
  different layer).
- Touches `vmac_build.py` / `vmac_topgen.py` / `vmac_cosim_*` + a small `arrayutils` addition. The
  `-m vitis` csim/cosim must still pass bit-exact after — the comparison reference moves from
  `execute_mem`'s image to `execute` + generic deserialize, so it must stay byte-identical.
- Relates to the "one canonical accelerator anatomy" convergence already noted in project memory.
