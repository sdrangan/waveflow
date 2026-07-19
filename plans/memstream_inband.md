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
- **No TLAST on internal channels.** Internal task-to-task channels are `hls::stream<ap_uint<W>>`
  (`'word'` flavor); TLAST would require switching every fixed task body to the `'axi4s'` flavor
  (`ap_axis<W,0,0,0>`), since *"an `axi4s_word` body will not bind to an `ap_uint` FIFO"*. And it would
  be **redundant**: the consumer already knows `xfer_len`/`data_len` from the descriptor, so TLAST buys
  only an assertion the generator already guarantees. Verify by **counting in the consumer** instead.
  TLAST stays where it earns its keep — the top-level AXIS boundary.
- **`VarDataArray` is not on the critical path.** Dynamic-sized schemas would make the *declaration*
  nicer later, but they cannot remove the bound in RTL (fixed hardware needs a buffer; `VarDataArray`
  itself carries `len_max`), and its codegen is unwired (`can_gen_include = False`).

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
