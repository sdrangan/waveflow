---
title: Understanding AXI Memory-Mapped
parent: Shared Memory (histogram)
nav_order: 1
has_children: false
---

# Understanding AXI Memory-Mapped

This is the first Waveflow example whose **data lives in memory** rather than on
the control bus or a stream. The accelerator is handed *pointers* — byte
addresses into a shared DRAM — reads its inputs from there over an AXI4
memory-mapped (`m_axi`) master, and writes its outputs back the same way. It
exists to introduce one idea: how a kernel moves bulk data to and from shared
memory, and why that is a different interface from the control registers
([regmap](../regmap/)) and the data streams ([stream_inband](../stream_inband/))
of the earlier examples.

## The data plane: AXI memory-mapped

When an accelerator processes more than a handful of scalars — an array of
samples, an image, a model's weights — that data is too big to pass through
control registers and often does not arrive as a neat stream. Instead it sits in
**shared DRAM** that both the host CPU and the FPGA kernel can address. The host
writes the input buffers, hands the kernel their addresses, and reads the output
buffers back afterwards. The kernel reaches into that memory directly.

**AXI memory-mapped** (`m_axi` in Vitis HLS) is the AXI family member for exactly
this. Unlike AXI-Lite — which reads or writes one 32-bit register at a time — an
`m_axi` master issues **burst transfers**: it presents a base address and a
length, and the memory streams back (or accepts) many consecutive words in one
transaction. Bursting is what makes it bandwidth-efficient; a 37-element read is
a couple of bus transactions, not 37 round-trips. The kernel sees memory as a
flat, byte-addressed pointer (`ap_uint<32>* m_mem` in the generated C++), and the
tool turns its array accesses into AXI read/write bursts.

The three AXI interfaces divide up cleanly, and this example is where the third
one finally appears:

| Interface | Role | Access pattern | Introduced in |
| --------- | ---- | -------------- | ------------- |
| **AXI-Lite** | control plane — scalar args, start/done | one register at a time | [regmap](../regmap/) |
| **AXI-Stream** | streaming data — flows through, no addresses | sequential, with `TLAST` | [stream_inband](../stream_inband/) |
| **AXI memory-mapped** | bulk data in shared DRAM | random-access bursts at byte addresses | **this example** |

## The shared-memory architecture

A memory-mapped accelerator still needs a control plane: someone has to tell it
*which* buffers to process, *how many* elements, and *where* to put the result —
and the kernel has to report when it is done and whether it succeeded. This
example keeps that control on a **dedicated AXI4-Stream**: the host pushes one
**command** descriptor in on the input stream and reads one **response** back on
the output stream, while all the bulk data moves over `m_axi`.

> **Why split control from data?** The command is a few small fields; the data is
> kilobytes. Putting the command on its own narrow stream keeps it cheap and
> ordered (one command in, one response out, like a function call), while the
> wide `m_axi` master is reserved for the high-bandwidth payload. The kernel is
> *launched* by the same `ap_start`/`ap_done` handshake the [regmap](../regmap/)
> example drove through registers — a launched kernel then blocks waiting for a
> command word to arrive. Launch and command are separate: the host says *go*
> over AXI-Lite, the command says *what* over the stream.

This is the **shared-memory pattern**: control over a stream, payload in memory
addressed by pointers carried in the command. (A *memory-backed* control queue —
where the commands themselves live in DRAM — is the separate, later `mem_queue`
example; here the command path is a plain stream.)

## The example: a histogram accelerator

The vehicle is a **histogram**. Given `ndata` floating-point samples and a set of
bin edges, it counts how many samples fall into each of `nbins` bins. That is a
natural fit for shared memory: the samples and edges are arrays the host stages
in DRAM, and the counts are an array the kernel writes back.

Everything the kernel needs to find its data is packed into the **command
descriptor** it reads off the input stream:

```python
# examples/shared_mem/hist.py — HistCmd
class HistCmd(DataList):
    """Command descriptor for the histogram accelerator."""
    elements = {
        "tx_id":          {"schema": TxIdField},   # correlate command & response
        "data_addr":      {"schema": AddrField},   # base address of the input samples
        "bin_edges_addr": {"schema": AddrField},   # base address of the nbins-1 edges
        "ndata":          {"schema": NdataField},  # number of samples
        "nbins":          {"schema": NbinField},   # number of bins
        "cnt_addr":       {"schema": AddrField},   # base address of the output counts
    }
```

Three of those fields are **addresses** (`data_addr`, `bin_edges_addr`,
`cnt_addr`) and two are **sizes** (`ndata`, `nbins`). The kernel reads the two
input buffers, computes the histogram, and writes the counts buffer — three
separate regions in the one shared memory, at independent addresses the command
chose.

## Byte addresses, words, and the allocator

The command's addresses are **byte addresses** — the same currency a CPU pointer
uses. Inside the kernel, memory is a pointer to 32-bit **words** (`ap_uint<32>*`),
so a byte address is divided by the word size (4 bytes) to index it. The
generated kernel does exactly this with a helper,
`memmgr::byte_addr_to_word_index<32>(...)`, before each burst — the
[code generation](codegen.md) page shows the lowered C++.

Who decides the addresses? On the simulation and testbench side, a small
**allocator** (`MemMgr` / the SimPy `MemoryMod`) hands out non-overlapping
regions in declaration order. The histogram needs three: `data`, then
`bin_edges`, then `counts`. The allocator places them back-to-back and reports
each region's base byte address, which the host stamps into the command. The
kernel never allocates — it only follows the pointers it is given. This mirrors
how a real host driver would `malloc` (or pin) the buffers and pass their
addresses to the accelerator.

## The execution model

One histogram transaction is a fixed sequence:

1. **Stage the inputs.** The host writes the `data` samples and the `bin_edges`
   into their allocated regions in shared memory.
2. **Issue the command.** The host pushes one `HistCmd` (the three addresses +
   the two sizes + a transaction id) onto the input stream.
3. **Validate.** The kernel checks the sizes and address alignment; if anything
   is out of range it selects a `HistError` and skips straight to the response —
   no memory is touched.
4. **Read.** The kernel bursts `ndata` samples from `data_addr` and `nbins-1`
   edges from `bin_edges_addr` over `m_axi`.
5. **Compute.** It bins the samples (the count of edges each sample meets or
   exceeds) into `nbins` counts.
6. **Write.** It bursts the `nbins` counts back to `cnt_addr`.
7. **Respond.** It emits one `HistResp` (the echoed `tx_id` + the status) on the
   output stream.
8. **Read the result.** The host reads the counts back from `cnt_addr`.

Steps 3–7 are the kernel; steps 1–2 and 8 are the host. The validation in step 3
is what makes the response carry a **status** rather than just data — the host
learns *why* a transaction failed (bad `ndata`, bad `nbins`, a misaligned
address) without the kernel ever reading garbage memory.

### What the Python model abstracts away

In the simulation you do not hand-assemble bursts or compute word indices. The
kernel issues typed array operations against its memory master, and the framework
lowers them to the right AXI bursts:

```python
# examples/shared_mem/hist.py — HistAccel.on_start (excerpt)
data = yield from self.m_mem.read_array(
    Float32, cmd.ndata, cmd.data_addr, max_count=self.max_ndata)
edges = yield from self.m_mem.read_array(
    Float32, cmd.nbins - 1, cmd.bin_edges_addr, max_count=self.max_nbins)
...
yield from self.m_mem.write_array(
    counts, Uint32Field, cmd.cnt_addr, cmd.nbins, max_count=self.max_nbins)
```

The **same** `read_array` / `write_array` calls drive the SimPy simulation, and
the **same** Python source is lowered to the Vitis HLS kernel's `m_axi` bursts —
so the buffer layout and the access pattern can never drift between the model and
the hardware.

---

The rest of this example follows the same accelerator through the full Waveflow
flow: [the Python model](python.md), [running it in SimPy](pysim.md),
[generating the Vitis kernel](codegen.md),
[validating the RTL](rtlsim.md), and
[viewing the timing and burst diagrams](timing.md).
