# In-band framed transfers for MemRStream / MemWStream

*Supersedes the mechanism sketched in `memstream_corr.md` (that draft's `xfer_msg`-carries-`MWCmd`
route is dead — a full `MWCmd` is 11×32-bit words but `xfer_msg` holds 8, and it needed a 32↔64
`reserialize`).*

## The problem

`MemWStream` takes its command on `s_cmd` and its data on `s_in` — two streams. Pairing the Nth
command with the Nth data block is only correct while **`s_cmd` order == `s_in` order**. That holds
today (one in-order reader feeds the writer), so this is **not a current bug** — it is an *implicit,
unenforced invariant*. It breaks silently the moment data comes from a different source, a reordering
producer, or multiple producers — i.e. exactly the SALSA tile case.

## The design — one stream, length-prefixed framing

Everything at `MEM_DWIDTH`, so header words *are* stream words (no 32↔64 reserialization):

```
frame = [ descriptor | payload (xfer_len words) | data (data_len words) ]
```

**`MemWStream`** (the consumer — decodes its own descriptor, treats the payload as opaque):

1. read `{dst_addr, data_len, xfer_len}` off `s_in`
2. read `xfer_len` words → local buffer  ← the ONE bounded array
3. read `data_len` words → pure-write to `m_mem`
4. emit `complete{data_len, xfer_len}` + the buffered payload on `s_done`

**`MemRStream`** (the producer — pure pass-through, decodes *nothing* of the payload):

1. read `{src_addr, data_len, fwd_len}` off `s_cmd`
2. forward `fwd_len` words from `s_cmd` straight to `m_out`   ← **no buffer needed**
3. fetch `data_len` words from `m_mem` → `m_out`

Those forwarded words *are* `MemWStream`'s descriptor + payload; the reader never parses them. So the
reader is application-agnostic — the same reader serves memcpy (payload = write descriptor) and a poly
job (payload = coefficients) without change. The consumer owns decoding.

## Why this is the right shape

- **Desync becomes structurally impossible** — descriptor, payload and data are contiguous on one
  stream, so they cannot be mispaired.
- **The max leaves the protocol and becomes a buffer bound.** The wire carries `xfer_len` words *per
  instance*; only `MemWStream` needs a sized array (it reorders — it echoes the payload *after* the
  write). `max_xfer_len` stops being a constant baked into every command struct.
- **`xfer_msg`-as-a-struct-field disappears**, and with it the 11-vs-8 overflow and the reserialize.
- It is the **already-synthesized** `examples/stream_inband/poly.py` pattern (`s_in.get(PolyCmdHdr)`
  then `get_pipelined(count=cmd_hdr.nsamp)`), extended with a variable-length payload.

## Decisions recorded

- **No opcode at the transport.** An opcode is a *routing* tag; it only earns its place at a router
  demuxing one stream to several handlers. Point-to-point needs none — the consumer knows its payload.
  A `StreamRouter` reading `{opcode, len}` is a later, optional layer (the SALSA multi-app path).
- **TLAST IS required on the framed channels — this reverses an earlier call.** The first draft of
  this plan said TLAST was redundant "because the consumer already knows the lengths." Implementing it
  disproved that. Two facts, found the hard way (Stage 1):
  1. **The pysim stream model is burst-granular.** `StreamIFSlave.get()` consumes the *whole* next
     burst and truncation is an error signal (*"a burst that was truncated … indicates a late/missing
     TLAST"*). A descriptor and its payload therefore **cannot share a burst** — each logical segment
     is its own burst.
  2. **Relaying an opaque segment requires packet boundaries.** A countless `get()` (the only read
     that does not require knowing the contents) is *refused* unless `has_tlast=True`. The reader must
     relay bursts it refuses to parse, so it cannot supply a word count — it needs the boundary.

  The "lengths make TLAST redundant" argument holds only for a consumer that *parses*. A **relay** that
  by design refuses to parse has nothing to count with. So the framed streams set `has_tlast=True`, and
  the payload's boundary and its declared `xfer_len` cross-check each other (a mis-framed producer
  fails loudly instead of silently shifting the data behind it).

  **DECISION (2026-07-18), REVISED after a csynth probe:** internal framed channels carry a
  **`{data, last}` user struct** ("framed word"), **not** the `ap_axis` type. The probe
  (`experiment/hls_task/probe_axi4s.cpp` vs `probe_framed.cpp`) found that Vitis 2025.1 **rejects
  `ap_axis` on an internal FIFO** — `ERROR [HLS 214-208]: ap_axis ... must only be used for AXI-Stream
  ports in the interface` — but a plain `struct {ap_uint<W> data; ap_uint<1> last;}` internal FIFO
  **csynths clean (Fmax 195 MHz)** with the same relay-until-`last` shape. So:
  - **Internal edges = a struct-typed FIFO** carrying the boundary bit; the relay reads until `.last`
    and forwards verbatim (opacity preserved). VCD still shows `.last`, so boundaries stay visible.
  - **`ap_axis` (real TLAST) stays on the top-level port**, converted `{data,last}` ↔ `ap_axis` by a
    boundary adapter — exactly where the existing `axi4s_word` boundary convention already lives.

  Why TLAST at all: everything here is packet-based, and the boundary bit is what lets the reader relay
  an opaque packet it refuses to parse (a countless `get()` needs a boundary, not a count).

  **The codegen surface** (unchanged estimate, different channel type): a new **`FramedEdge`** flavor in
  `composite_gen` (struct-typed `hls_thread_local` FIFO) alongside `StreamEdge`; the framed task bodies
  (proven pattern in `probe_framed.cpp`); a boundary adapter at the top port. The legacy `StreamEdge`
  (`ap_uint<W>`) bodies are untouched — this is additive.

  **Sequencing — de-risk on the framed path first, do NOT big-bang flip the working bodies:**
  - **Stage 2** writes the framed `mem_stream` bodies on `'axi4s'` and csynth **+ cosim**s them. This is
    both the needed work and the **probe** for the one unverified toolchain question — *does
    `hls::stream<ap_axis<W>>` as an internal `hls_thread_local` FIFO between `hls::task`s cosim clean in
    2025.1?* (Should — it is a FIFO-of-structs between tasks — but the project does not assert cosim
    without a gate.)
  - **If green**, migrating the other 10 channels to `'axi4s'` (uniformity, VCD-legibility) is its
    **own separate gated plan**, re-verifying 158/176/3469 as it goes — not risked in the same stroke
    as new work.
  - SOBIF/block edges are out of scope (already framed by block size).
  - **Helper gap:** today's `axi4s` helpers (`write_axi4_word`, `flush_axi4_stream_to_tlast`) are
    boundary-oriented; the **relay** use ("read one whole packet, forward it boundary-intact") likely
    needs a small `read_axi4_packet` / `relay_axi4_packet` helper added in `streamutils_hls.h`.
- **`VarDataArray` is not on the critical path.** Dynamic-sized schemas would make the *declaration*
  nicer later, but they cannot remove the bound in RTL (fixed hardware needs a buffer; `VarDataArray`
  itself carries `len_max`), and its codegen is unwired (`can_gen_include = False`).

## Codegen shape (settled during Stage 2 step 1)

- **`framed_word<W> { ap_uint<W> data; ap_uint<1> last; }`** in `streamutils.hpp` — a strict subset of
  `ap_axis`, **same member names** (`.data`/`.last`) so one template serves both.  (`ap_axis`'s C++
  member is `.last`, not `.tlast` — the *signal* is TLAST; the *field* is `.last`.)
- **Two read/write realizations, not three.** `read_stream`/`write_stream` for a bare `ap_uint<W>`
  (the beat IS the data); **`read_boundary_word`/`write_boundary_word<WordT,W>`** shared by *both*
  boundary-carrying words (`axi4s_word` at a port, `framed_word` internal) — the body only touches
  `.data`/`.last`.  The write's `keep`/`strb` difference is isolated in `set_axis_aux` (overloaded:
  no-op for `framed_word`, sets them for `axi4s_word`).  A template is **not** a runtime conditional —
  each instantiation is separate, branch-free RTL, so DRY is free.
- **Three *type*-flavors on one axis** — `word` / `framed` / `axi4s` = **no boundary / internal
  boundary / interface boundary**.  The flavor selects the channel *type*; the two boundary flavors
  share the one helper realization.
- **`StreamIF.framed: bool = False`** (opt-in; default keeps everything byte-identical) — chosen over
  reusing `has_tlast` because endpoints already default `has_tlast=True` and internal edges are always
  `ap_uint` today; a fresh flag avoids silently re-typing existing edges.
- **`FramedEdge`** (in `composite_gen`, mirrors `SobEdge`) emits the `framed_word` FIFO; produced by
  `derive_internal_edges` only for a `framed` `StreamIF` (not wired yet — Stage 2 step 2).

## Stage 2 step 2 — per-schema framed methods (mapped; approach settled, one decision open)

**Approach = (b), refined.** Because `framed_word` is **field-identical** to `axi4s_word` (`.data`/
`.last`), the generated framed method is the `axi4_stream` method *typed on `framed_word`* — the whole
recursive `.data`/`.last` machinery is reused verbatim, not duplicated (even more sharing than calling
`read_boundary_word`, which stays for the hand-written task bodies). So `framed_stream` is added as a
`src_type`/`dst_type` that the recursive generators treat exactly like `axi4_stream`, differing only in
the method signature's stream type + name (`read_framed_stream`/`write_framed_stream`).

**Surface (mapped in `dataschema.py`):** ~30 `src_type=="axi4_stream"` / `dst_type=="axi4_stream"`
conditional sites across the base + `DataList` + `DataArray` + `VarDataArray` recursive read/write
generators, the two signature branches in `gen_read`/`gen_write`, and the validation guards
(465, 603). Mechanical (extend each `== "axi4_stream"` to include `"framed_stream"`), but wide and in
the most delicate module — do it site-by-site with a csynth probe.

**OPEN DECISION (footprint) — yours to make.** The generation loops
(`for src_type in ("array","stream","axi4_stream")` at 2632/2642/4174/4177) emit one method per type
per schema. Adding `"framed_stream"` there gives **every schema** a framed read/write method:
- **(i) all schemas** — simplest, uniform, but changes *every* schema header (additive but not
  byte-identical) and bloats headers with methods most schemas never use.
- **(ii) on-demand** — generate framed methods only for schemas actually used on a framed stream (a
  per-schema opt-in / driven by the graph). Minimal footprint, byte-identical for untouched schemas,
  but needs plumbing "which schemas are framed."

I recommend **(ii)** — it matches the `framed=False`-default, nothing-else-moves discipline the whole
plan has held. But it's a real footprint tradeoff, so it's a decision to confirm before the sweep.

## Staging (each stage leaves every gate green)

**Blast radius:** `MemRStream`/`MemWStream` are used by **mem_copy** and the **standalone
`mem_r_stream`/`mem_w_stream` kernels** only — the interleaver has its own `IlMemR`/`IlMemW`. Four
fixed C++ bodies (144 lines total).

### Stage 1 — pysim, opt-in, nothing else touched
`inband: HwParam[bool] = False` on both components. New descriptor/complete schemas. Implement the
in-band `run_iter` for both; the legacy path is untouched and remains the default. A focused pysim test
frames a sequencer → reader → writer → memory transfer and checks the payload echo.
**Gate:** fast loop at the 6-failure baseline; `-m xsi` unchanged (158/176/2835/3469) because no
existing component changes behavior.

### Stage 2 — C++ task bodies for the in-band variant
New fixed bodies (`mem_*_inband_task.h`) matching the pysim `run_iter` exactly. csynth probe for the
bounded payload loop (`#pragma HLS LOOP_TRIPCOUNT max=MAX_XFER`) — a new shape for these bodies.
**Gate:** csynth clean; standalone gates still 158/176 (they stay on the legacy bodies).

### Stage 3 — migrate mem_copy, re-baseline the cycle gate
`MemCopy` wires Sequencer → reader → writer as one framed stream. **The 2835 gate WILL move** — the
frame adds `descriptor + payload` beats per job. Re-baseline *with an explanation of the delta*
(expected ≈ +(descriptor + xfer_len) beats/job), never by blindly accepting a new number.

### Stage 4 — retire the legacy two-stream path
Once mem_copy and the standalone kernels are on in-band, delete the `inband` flag, the old schemas and
the legacy bodies. (The transitional flag lives only as long as needed — the `CompositeComp`/`bundle`
lesson.)

## Risks
- **Cycle-count delta (Stage 3)** — the one real loss of a safety net; re-derive it, don't accept it.
- **II on the payload loop** — a short serial preamble on the same stream as the data. The
  throughput-critical `data_len` burst is unchanged and still pipelined, so no II regression is
  *expected*, but that is an assumption to verify at csynth, not assert.
- **Opacity trades type-safety** — the payload is words in transit; the consumer must parse correctly.
  `xfer_len` is the minimum validation the descriptor must carry.
