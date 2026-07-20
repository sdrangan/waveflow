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
   measured==derived WITH the user before accepting.**  **DONE — gate = 2910 (user-accepted).**
   Fresh `generate(inband=True)` → csynth (Fmax 111 MHz) → XSI: `XSI_EXITCODE=0`, 16/16 regions
   bit-exact, 32 `s_done` words (16·`[WrComplete | payload]`), tx_ids 0..15 echoed.  Measured
   `done_cycle=2910`; derived closed-form `165 + 15·183 = 2910` (steady **183 cyc/job**, no fill
   transient).  Δ vs two-stream 2835 = `15·(183−177.6) + (165−171) = +81 − 6 = +75`: the +5.4 cyc/job
   is the in-band `WrCmd`(2) + payload(1) = 3 relayed beats prepended to each 128-word data burst
   (plus a couple of writer state-machine bubbles); the −6 is the shorter descriptor firing the first
   completion earlier.  The plan's rough `2883` had the right mechanism but modeled the beats as
   purely additive; the real effect is a throughput-cadence shift.  TB is inband-threaded
   (`make_xsi_tb`/`render_xsi_vectors`/`check_mem_copy_xsi_outputs`/`write_mem_copy_xsi_bundles`,
   `_done_words`, `DONE_WORDS=2`); the harness is framing-agnostic (a generic `AxisSlave` captures
   `s_done`).  **Note:** a fresh two-stream `generate()` is *broken* (tx_ids all 0) because its
   Sequencer body is generated + depends on hand-written hook stubs that regenerate as empty TODOs;
   the in-band Sequencer is fully hand-written (no hooks), so it synthesizes correctly straight from
   `generate()` — one less thing to maintain after Stage 4.
4. **retire** the two-stream path + the `inband` flag once framed is green.  **Decided (user): full
   retire** — flip the default to in-band, delete the two-stream bodies/edges/hooks/`MRCmd`/`MWCmd`/
   `MemComplete` usage + the `inband` flag itself, update the XSI gate to 2910.  One clean commit.

## Notes
- Keep `inband=False` the default until Stage 3 is XSI-green — nothing existing moves before then.
- The Sequencer owns the framing (decided with the user): the RTL kernel does it, not just the TB.
