---
title: Python model
parent: Histogram with shared memory
nav_order: 2
has_children: false
---

# Python model

This page builds the histogram accelerator as a Waveflow `HwModule`. The
[concept page](aximm.md) covered *why* the data lives in memory and the control
on a stream; here we write the Python that says *how* — the command/response
schemas, the kernel's three data ports, and the `on_start` body that validates,
reads, computes, and writes back. Every excerpt is from
[`examples/shared_mem/hist.py`](../../../examples/shared_mem/hist.py) and
[`hist.py`](../../../examples/shared_mem/hist.py).

## The command and response schemas

The control plane is two small descriptors that ride the streams. A **command**
goes in, a **response** comes back. Both are `DataList` schemas — ordered structs
that serialize to the stream words — so the same field layout drives the SimPy
model, the generated C++ struct, and the host.

```python
# examples/shared_mem/hist.py
TxIdField   = IntField.specialize(bitwidth=16, signed=False)
NdataField  = IntField.specialize(bitwidth=32, signed=False)
NbinField   = IntField.specialize(bitwidth=32, signed=False)
Uint32Field = IntField.specialize(bitwidth=32, signed=False, include_dir=INCLUDE_DIR)
Float32     = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)
AddrField   = MemAddr.specialize(bitwidth=MEM_AWIDTH)    # 64-bit byte address
```

The command carries the three buffer addresses and the two sizes; the response
echoes the transaction id and reports a status:

```python
# examples/shared_mem/hist.py
class HistCmd(DataList):
    elements = {
        "tx_id":          {"schema": TxIdField},
        "data_addr":      {"schema": AddrField},
        "bin_edges_addr": {"schema": AddrField},
        "ndata":          {"schema": NdataField},
        "nbins":          {"schema": NbinField},
        "cnt_addr":       {"schema": AddrField},
    }

class HistResp(DataList):
    elements = {
        "tx_id":  {"schema": TxIdField},
        "status": {"schema": HistErrorField},
    }
```

The status is a typed enum, not a bare integer — `HistError` names the four
outcomes the host can distinguish:

```python
# examples/shared_mem/hist.py
class HistError(IntEnum):
    NO_ERROR      = 0
    INVALID_NDATA = 1
    INVALID_NBINS = 2
    ADDRESS_ERROR = 3

HistErrorField = EnumField.specialize(enum_type=HistError)
```

`AddrField` is a [`MemAddr`](../../guide/interface/index.md) — a 64-bit field
whose value is a **byte address** into the shared memory. That is the only thing
that makes these buffers "shared": the command does not carry the data, it
carries pointers to it.

## The accelerator component

`HistAccel` is the `HwModule`. Its `cpp_kernel_name` / `cpp_namespace` name the
generated kernel function and the hand-written-hook namespace; its **HwParams**
are the compile-time configuration (interface widths and the buffer maxima); and
its three **ports** are the two control streams and the one memory master.

```python
# examples/shared_mem/hist.py
@dataclass
class HistAccel(HostActivated):
    cpp_kernel_name: ClassVar[str | None] = "hist"
    cpp_namespace:   ClassVar[str | None] = "hist_impl"

    in_bw:      HwParam[int] = STREAM_DWIDTH   # command-stream width  (32)
    out_bw:     HwParam[int] = STREAM_DWIDTH   # response-stream width (32)
    mem_bw:     HwParam[int] = MEM_DWIDTH      # m_axi data width      (32)
    mem_awidth: HwParam[int] = MEM_AWIDTH      # m_axi address width   (64)
    max_ndata:  HwParam[int] = MAX_NDATA       # 1024 — sample-buffer bound
    max_nbins:  HwParam[int] = MAX_NBINS       # 32   — bin-buffer bound

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave( name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
        self.m_mem = MMIFMaster(    name=f'{self.name}_m_mem', sim=self.sim, bitwidth=self.mem_bw)
        # Control-only regmap: no application registers — the command rides in-band on s_in.
        self.regmap = VitisRegMap({})
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.m_mem, self.s_lite):
            self.add_endpoint(ep)
```

Four ports:

1. **`s_in`** — a `StreamIFSlave`. The kernel *receives* the `HistCmd` here; the
   host is the stream master.
2. **`m_out`** — a `StreamIFMaster`. The kernel *sends* the `HistResp` here.
3. **`m_mem`** — an [`MMIFMaster`](../../guide/interface/index.md). This is the
   AXI memory-mapped master, the new interface this example introduces. The
   kernel issues array reads and writes through it; the bulk data flows here.
4. **`s_lite`** — the AXI-Lite control slave, carrying **only** `ap_start` / `ap_done`.

### Why a control-only regmap

`VitisRegMap({})` declares **no application registers** — which looks odd until you see what it is for.
**The command stays in-band on `s_in`; that is this example's whole lesson.** Only the *control* plane
moves.

And the AXI-Lite slave is not new. `m_axi ... offset=slave` **already forced one into existence** to
carry `m_mem`'s base address at `0x10` — and that slave declared `0x00 : reserved` with no `ap_start`
logic at all. So the kernel needed **two masters**: a CPU to write the base address over AXI-Lite, and
something else entirely to pulse an `ap_start` wire. Deriving from
[`HostActivated`](../../guide/flows/modules.md) fills the reserved slot in the slave that was
already there — one master, one address space — and makes the component's kind explicit, so
[`check(HistAccel)`](../../guide/comp_codegen/index.md) can answer for it.

Compare [`simp_fun`](../regmap/python.md), where the regmap carries the arguments too; here it carries
nothing but the handshake.

The `max_ndata` / `max_nbins` HwParams are load-bearing for codegen: they become
the compile-time array bounds in the generated kernel (`float data[max_ndata]`),
the depth of the `m_axi` interface, and the upper limits the validation hook
checks against. They are the one place the buffer sizes are declared.

## The kernel body

`on_start` is the kernel — the body Vitis runs once per `ap_start`, the handshake
`HistAccel`'s `HostActivated` base exposes through its control-only regmap. It reads
exactly as the [execution model](aximm.md) described: get the command, validate it,
read the two inputs, compute, write the counts, respond.

```python
# examples/shared_mem/hist.py — HistAccel.on_start
def on_start(self) -> ProcessGen[None]:
    cmd: HistCmd = yield from self.s_in.get(HistCmd)

    status = yield from self.validate(cmd)
    if status != HistError.NO_ERROR:
        yield from self.respond(self.m_out, cmd.tx_id, status)
        return

    data = yield from self.m_mem.read_array(
        Float32, cmd.ndata, cmd.data_addr, max_count=self.max_ndata)
    edges = yield from self.m_mem.read_array(
        Float32, cmd.nbins - 1, cmd.bin_edges_addr, max_count=self.max_nbins)

    counts = yield from self.compute(data, edges, cmd.ndata, cmd.nbins)
    yield from self.m_mem.write_array(
        counts, Uint32Field, cmd.cnt_addr, cmd.nbins, max_count=self.max_nbins)

    # status is NO_ERROR on the success path — reuse it for the response.
    yield from self.respond(self.m_out, cmd.tx_id, status)
```

A few things are doing real work here:

1. **Validate first, touch memory never-if-invalid.** `validate` returns a
   `HistError`; a non-`NO_ERROR` status takes the early-return path — respond and
   stop, before a single `m_axi` access. A bad `data_addr` never gets
   dereferenced.
2. **The edges read is unconditional.** It reads `cmd.nbins - 1` edges — which is
   `0` when `nbins == 1` (one bin, no interior edges). A zero-length burst is a
   harmless no-op, so there is no `if (nbins > 1)` guard. That keeps the kernel
   branch-free where the code generator can't lower a `>` comparison anyway (see
   [`CODEGEN_NOTES.md`](../../../examples/shared_mem/CODEGEN_NOTES.md)).
3. **Two element types, one master.** `data`/`edges` are read as `Float32`;
   `counts` is written as `Uint32Field`. All three go through the same `m_mem`
   port — the multi-type, multi-buffer traffic this example exists to exercise.

## The `MMIFMaster` interface

`m_mem.read_array` / `write_array` are the memory-mapped equivalent of the
register `get`/`set` from the [regmap example](../regmap/python.md). Each call is
one typed, addressed burst:

```python
read_array(self, elem_type, count, byte_addr, *, max_count) -> ProcessGen[array]
write_array(self, values, elem_type, byte_addr, count, *, max_count) -> ProcessGen[None]
```

- **`elem_type`** — the schema (`Float32`, `Uint32Field`) the bytes are
  interpreted as. The master converts to/from raw words; the model and the kernel
  agree on the layout because they share the schema.
- **`count`** — the runtime number of elements. It can be a command field
  (`cmd.ndata`) or an expression (`cmd.nbins - 1`); the code generator lowers the
  same expression into the C++ burst length.
- **`byte_addr`** — the base byte address from the command. The master divides by
  the word size to index the underlying word array; the model and kernel use the
  same `byte_addr_to_word_index` convention.
- **`max_count`** — the compile-time upper bound (`self.max_ndata` /
  `self.max_nbins`). It sizes the on-chip buffer and the interface depth in the
  generated kernel; at runtime the actual transfer is `count` elements.

## The hand-written hooks

Three methods are marked `@synthesizable`. That decorator tells the code
generator the method is part of the kernel, but its **body is supplied by a
hand-written C++ file** rather than lowered from Python — the seam through which
the datapath stays human-owned. In simulation, the Python body runs; in the
generated kernel, codegen emits a call to the hook.

```python
# examples/shared_mem/hist.py
@synthesizable
def validate(self, cmd: HistCmd) -> ProcessGen[HistError]:
    ndata, nbins = int(cmd.ndata), int(cmd.nbins)
    word_bytes = self.mem_bw // 8
    if ndata <= 0 or ndata > self.max_ndata:
        return HistError.INVALID_NDATA
    if nbins <= 0 or nbins > self.max_nbins:
        return HistError.INVALID_NBINS
    if (int(cmd.data_addr) % word_bytes or int(cmd.bin_edges_addr) % word_bytes
            or int(cmd.cnt_addr) % word_bytes):
        return HistError.ADDRESS_ERROR
    return HistError.NO_ERROR
    yield  # unreachable — makes this a generator

@synthesizable
def compute(self, data, edges, ndata, nbins) -> ProcessGen[HistCountBuf]:
    counts = golden_counts(np.asarray(data)[:int(ndata)], edges, int(nbins))
    return counts
    yield  # unreachable — makes this a generator
```

- **`validate`** is the bounds/alignment logic — it reads the `max_ndata` /
  `max_nbins` HwParams to range-check the command and confirms every address is
  word-aligned. Its hand-written counterpart is `hist_validate_impl.cpp`.
- **`compute`** is the binning datapath — for each sample, the bin is the number
  of edges it meets or exceeds (`np.searchsorted(..., "right")`). Its hand-written
  counterpart is `hist_compute_impl.cpp`.
- **`respond`** builds the `HistResp` and writes it on `m_out`; its counterpart is
  `hist_respond_impl.tpp`.

Why hand-write these? `validate` and `respond` are control glue that the
generator emits *calls* to, and `compute` is the real algorithm — exactly the
body a future AI-assisted codegen pass would fill, but for now a small,
reviewable `.cpp` that lives next to the Python. The [code generation](codegen.md)
page shows the generated kernel calling into them.

## Next

- [SimPy simulation](pysim.md) — running the model end-to-end against the numpy
  golden, with a `MemoryMod` standing in for shared DRAM.
- [Code generation](codegen.md) — lowering `HistAccel` to the Vitis HLS kernel.
