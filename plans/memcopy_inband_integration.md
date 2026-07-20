# Integrate the inband/framed protocol into mem_copy

Turns the framed toolbox (plans/memstream_inband.md, all built + csynth-proven) into a working framed
mem_copy.  **Additive: `MemCopy(inband=False)` default**, so the existing two-stream kernel stays
byte-identical and XSI-green (2835) while the framed variant is built and gated beside it; the legacy
path is retired only in Stage 4, once framed is XSI-proven.

## Structural change

Two-stream (today) → linear framed chain:

```
                 [FwdCmd | WrCmd | payload]                 [WrCmd | payload | data]
Sequencer ─────────────────────────────────▶ MemRStream ───────────────────────────▶ MemWStream ──▶ mem
  frames the command stream                   relays the opaque prefix (2 bursts),    decodes WrCmd,
  (owns framing — it is the                   fetches src data                        writes dst,
   synthesizable component)                                                            echoes [WrComplete|payload]
```

- **Two framed internal edges** (`Sequencer→reader.s_cmd`, `reader.m_out→writer.s_in`) instead of
  three plain ones; the writer loses `s_cmd` (everything on one framed `s_in`) so a descriptor can
  never pair with the wrong data.
- **The payload is the tx_id cookie** (1 word) — same correlation as today's `xfer_msg`, but carried
  in-band and echoed on `WrComplete`, not a fixed 8-word array.

## Per-job framing (matches the Stage-1 run_iter already built)

- Sequencer emits 3 bursts to the reader: `FwdCmd{addr=src_off, len=n, fwd_bursts=2}`, then the
  `WrCmd{addr=dst_off, len=n, xfer_len=1}` burst, then the `[tx_id]` payload burst.
- Reader reads `FwdCmd` (`read_framed_stream`), relays the 2 opaque bursts (`WrCmd`, payload) verbatim,
  fetches `n` src words, emits `[WrCmd | payload | data]`.
- Writer reads `WrCmd`, buffers the 1-word payload, writes `n` words to `dst`, echoes
  `[WrComplete{len=n, xfer_len=1} | payload]`.

## Stages (each gated)

1. **pysim** — `Sequencer.inband` + `MemCopy.inband` (default False); framed Sequencer `run_iter`;
   the two internal edges `framed=True`; reader/writer `inband=True`.  **Gate: mem_copy pysim golden
   bit-exact for inband=True; inband=False byte-identical + fast loop 6-baseline.**
2. **framed C++ bodies** — the hand-written framed `mem_r`/`mem_w`/`mem_seq` task bodies; mark the
   command schemas `framed` at their `DataSchemaStep`; the composite top declares the two `FramedEdge`
   FIFOs.  **Gate: csynth.**  **DONE** — `waveflow/build/mem_seq_framed_task.h` /
   `mem_r_stream_framed_task.h` / `mem_w_stream_framed_done_task.h` (copied by `MemStreamStep`);
   `kernel_task()` inband branches on all three components; `generate(inband=True)` emits the framed
   schema set (`INBAND_SCHEMA_CLASSES`, `FwdCmd`/`WrCmd` `framed=True`) and a top with two
   `framed_word<64>` FIFOs + `mem_w_stream_framed_done_task<64, 8>`.  **csynth GREEN**
   (`WAVEFLOW_CSYNTH_OK`, Fmax 111 MHz, no hook TUs); default two-stream byte-identical, fast loop at
   the 6-failure baseline.  The boundary ports stay word-flavor (`ap_uint` + axis pragma, as every
   existing top) — only the two internal edges are framed; the writer's `s_done` echo is a word
   boundary (`WrComplete` + payload words), self-describing via `WrComplete.xfer_len`.
3. **XSI re-baseline** — drive framed mem_copy through RTL; the cycle count moves from 2835 by the
   added descriptor/payload beats (≈ `2835 + 16·(WrCmd_words + 1)`); **derive it and confirm
   measured==derived WITH the user before accepting.**
4. **retire** the two-stream path + the `inband` flag once framed is green.

## Notes
- Keep `inband=False` the default until Stage 3 is XSI-green — nothing existing moves before then.
- The Sequencer owns the framing (decided with the user): the RTL kernel does it, not just the TB.
