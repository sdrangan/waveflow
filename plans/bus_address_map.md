# Plan: a hierarchical bus address map -- per-type layouts, per-system bases

> **Status (2026-10-03): BUILT, Stages 1-5** (branch `bus-address-map`). D6 (a kernel as bus master)
> is exercised by the two-kernel example, not built here. Departures are in **Built** at the end.

## Why

A bus master needs two addresses to reach a view: **where the slave instance sits** (its base) and
**where the view sits inside it** (the offset). Today:

- pysim flattens them into one absolute map (`MemSlaveMap`, from `adaptor.s_mem.addr_range` +
  `adaptor.offset_of(view)`);
- the RTL crossbar gets the base separately (`AxiXbarConfig(mi=[AxiXbarRange(REGS, ...)])`), and the
  adaptor's decoder applies the offset rule separately (`VIEW_LAW = 12` vs `VIEW_BYTES = 4096`) --
  kept consistent only by shared constants, with no test;
- the XSI testbench host gets a flattened header (`mm_fir_xsi.map_header`), included by hand.

That cannot express two instances of one kernel type, and it puts a placement (the base) wherever an
offset is needed.

## The design

Split the map the way driver stacks do (AMD: `xparameters.h` per system, `x<ip>_hw.h` per IP type):

| | contents | changes when | generated from |
|---|---|---|---|
| **layout**, per kernel type (+ parameters) | each view: name, kind, offset within the slave, window, depth, word counts. Offsets inside a view (COMMIT, status, the control half) stay fixed rules on `MmView` / `ViewEntry` | the type's adaptor changes | the type's declared views -- no base |
| **bases**, per system | each slave instance's base and span | placement changes | `assign_address_ranges` -- the same numbers that configure the crossbar |
| **device**, on the master | `MmDevice(base, layout)` -- each endpoint's address = base + offset | -- | combined at run time |

### D1. The adaptor belongs to the kernel type

For a layout to be a property of a type, the type must declare its views. A kernel type (or a wrapper
type, kernel + adaptor -- decide at Stage 1 which reads better) declares its views and their order;
building an instance builds its adaptor. `MmFirSystem` then places two things (a kernel instance and
a host) instead of assembling an adaptor. This also closes the slave page's "the adaptor is assembled
by example code" item.

### D2. Layout and device, in Python

- `MemSlaveLayout`: plain data, no base -- `ViewEntry` without `base`, plus the slave's span. Built
  from a type (`Layout.of(KernelType, **params)`) or an adaptor instance.
- `layout.at(base)` gives today's `MemSlaveMap`, so nothing downstream breaks.
- `BoundMemSlaveAdaptor(layout, base, master)` -- the Python `MmDevice`. Two instances of one type
  share one layout object and differ only in base.

### D3. Layout and bases, in C++

- `<type>_layout.h`: `namespace <type>_layout { constexpr MmViewLayout qin = {...offset 0x1000...}; }`
  -- the offsets relative to the slave.
- `<system>_bases.h`: `constexpr uint64_t FIR_A_BASE = 0x4000'0000, FIR_A_SPAN = 0x4000; ...`.
- `MmDevice dev(FIR_A_BASE)` over a layout namespace hands out endpoints at base + offset -- the C++
  twin of D2.
- **Instances with different parameters are different layouts** (one per type + parameter digest,
  as Vitis generates one driver config per IP configuration).

### D4. One source for the crossbar

`AxiXbarConfig.from_crossbar(xbar, name)` builds the RTL crossbar's ranges from the pysim crossbar's
bound slaves (`addr_range` base + span). mm_fir's hand-written `XBARS` goes away. The decoder's 4 KB
rule becomes one shared constant (`VIEW_BYTES` / `VIEW_LAW` derived from one).

### D5. Discovery and auto-include

- A master endpoint bound to a crossbar depends on that crossbar's **bases** header and on the
  **layout** header of every slave type it reaches.
- Generated XSI testbench hosts get the includes from that walk -- mm_fir's hand-written
  `#include "mm_fir_map.h"` comes from the generator.
- The headers live in `include/` and are hashed by the staleness guard like any source: moving an
  address correctly marks dependent RTL stale.

### D6. Kernels as bus masters

A kernel that writes another kernel's queue (the two-kernel example) includes the **target's layout**
at compile time -- it moves only if the target type changes, which needs a rebuild anyway -- and gets
the **target's base at run time**, in its config (a register-bank message) or in each command. The
kernel's RTL is then independent of where either kernel is placed.

A compile-time base is allowed for a system that is truly fixed, but only as an explicit template
argument, never a hidden include.

## Stages

1. **D1:** the adaptor declared by the kernel type; mm_fir switched; same addresses, same cycle
   counts (pysim and RTL) -- the check that nothing moved.
2. **D2 + D3:** `MemSlaveLayout`, `layout.at(base)`, the device in both languages, the two headers.
   Test: two instances of one type at two bases, reached by one host through one layout.
3. **D4:** `AxiXbarConfig.from_crossbar`; one decoder constant; a test that the generated headers,
   the pysim map and the crossbar config agree for mm_fir's both topologies.
4. **D5:** discovery and auto-include in the generated testbench host; mm_fir's include generated.
5. **Docs:** the slave pages (reaching a view = layout + base), the crossbar page (its ranges come from
   the pysim crossbar), mm_fir.

D6 is exercised by the two-kernel example, built after this.

## Open

- Wrapper type vs kernel-declared views (D1). The kernel staying "streams only" argues for a wrapper
  (kernel + adaptor as one placeable type); simplicity argues for the kernel declaring its views.
- Real host software: the same layout/bases split as a C header for software on the processor and a
  JSON/Python form for PYNQ -- generated from the same objects; the bases then come from the Vivado
  block design's address assignment (`plans/board_packaging.md`), which the system map is checked
  against.

## Built

- **D1 decided: views declared on the kernel** (the user's choice over a wrapper type).  `mm_views` on
  the kernel class -- `RegBank` / `QueueIn` / `QueueOut` / `BramWindow` specs naming its ports
  (`waveflow/hw/mm_device.py`).  The RTL adaptor is still assembled by the example's testbench top,
  not by `wrapper_gen`; that is the wrapper path's job, not taken.
- **D2:** `MemSlaveLayout` (entries are `ViewEntry`s whose `base` is the offset within the slave),
  `.of(KernelType)`, `.from_adaptor`, `.at(base)`; `build_mm_device` / `MmSlaveDevice.ranges(base)`;
  `BoundMemSlaveAdaptor.at(layout, base, master)`.  An instance's view MODULES carry a prefix
  (`a_qin`); the layout is keyed by the type's names (`qin`).
- **D3:** `MemSlaveLayout.to_cpp_header` (`MmViewLayout`, `SPAN`), `bases_to_cpp_header`
  (`<INSTANCE>_BASE` / `_SPAN`), `wfbfm::at(view, base)`.  The C++ endpoints hold `MmView` by value.
- **D4:** `AxiXbarConfig.from_crossbar`; mm_fir's configs from it are identical to the old literals
  (IP digest included), so the RTL did not change.  `VIEW_LAW` derives from `VIEW_BYTES`.
- **D5:** `bus_address_headers(xbar, system=)` -- found by a walk from the crossbar (each bus-facing
  module records its device as `mm_device`, excluded from `structure_signature`); refuses a slave with
  no layout.  mm_fir's testbench takes its headers and `#include` lines from it.
- mm_fir: same addresses and cycle counts throughout (pysim 498 / 545 / 341, RTL 520 / 529).
