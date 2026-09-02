---
title: Transmit — RfShotTx
parent: RfShotBuf
grand_parent: RF converters
nav_order: 1
audience: python
api: [RfShotTx, ShotTxHdr, ShotTxResp, Rfdc, RFSampIF, StreamIF]
summary: "Playing a stored waveform out of a converter: hand RfShotTx a shot once and it plays it a counted number of times or forever, answering every command with one verdict. The boundary ports, the in-band header and response as field tables, the two play modes and what a load arriving mid-play does to each, all five verdicts and which are transient, and the four rules that bite — including why the output is never silent and why a short transfer is a verdict rather than a hang."
---

# Transmit — `RfShotTx`

`RfShotTx` is the **transmit half** of the finite sample buffer. You hand it a waveform once; it
holds it in a BRAM and plays it at the converter's own rate — a counted number of passes, or forever
until you replace it. Every command you send is answered by exactly one verdict.

It is the design to use when the waveform is decided before it is played: a repeating test signal, a
channel-sounding sequence, a pulse train. If you need to change the samples *while they are going
out*, without a gap, this is the wrong buffer — see
[choosing a sample buffer](../choosing.md).

```
   host ──[ShotTxHdr | samples … TLAST]──▶ ┌─────────────┐
                                           │ ShotTxLoader│──┐
   host ◀────────── ShotTxResp ─────────── └─────────────┘  │ lock ⇄ region
                                                            ▼
                                                       ┌─────────┐
                                                       │  BRAM   │
                                                       └─────────┘
                                                            ▲
                                                            │ lock ⇄ region
                                           ┌─────────────┐  │
                        samp_out ◀─────────│ ShotTxPlayer│──┘
                        (to Rfdc)          └─────────────┘
```

Between the player and `samp_out` sits one more stage that re-lays the samples from the dense packing
the memory holds into the slot packing the converter wants. You do not wire it; the composite does.

## Instantiating one

```python
from waveflow.hw.rf_shot_tx import RfShotTx
from waveflow.hw.rfdc import Rfdc
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord

word = Rfsoc4x2SampWord.specialize(samp_per_word=4)
rfdc = Rfdc(name="rfdc", sim=sim, n_rx=0, n_tx=1, word=word)

dut = RfShotTx.for_word(
    word,
    depth=256,          # words in the memory
    nword=64,           # words in one shot  -> nsamp = 64 * 4 = 256 samples
    base=192,           # first element of the region
    blk_words=16,       # words per chunk: the poll period, and the pysim block quantum
    sim=sim, name="dut", clk=axis_clk,
    dac_word_rate=256e6 / 4,
)
```

`for_word` derives the width and the slot re-layout from the converter's word type, so the buffer
cannot disagree with the converter about packing. The three numbers you actually decide are `depth`,
`nword` and `base`.

**`nword` is build-time structure, not a command field.** How long a shot is, is declared once, here.
A header that disagrees is *refused*, never truncated — that is what `SHOT_WRONG_LEN` is for. `nsamp`
exists on the header because it is what the **host believes** it is sending, and catching that belief
disagreeing with what arrived is the response's whole job.

**`base + nword` must fit inside `depth`**, and `blk_words` must divide `nword` so a chunk never
straddles the end of the region.

## The boundary

`RfShotTx` presents **three** AXI-Stream ports. The memory is inside the design: the composite
instantiates its own BRAM and the two `buf_w` / `buf_r` wires only exist between the kernel and that
memory, joined by a generated wrapper. Nothing outside sees them.

| port | direction | carries |
|---|---|---|
| `s_in` | in | the header and, behind it, the payload — one frame, `TLAST` on the last word |
| `resp_out` | out | one `ShotTxResp` per header |
| `samp_out` | out | samples, slot-packed, straight into `Rfdc.tx_streams[0]` |

**The payload rides in-band on `s_in`.** There is no separate data port and no `m_axi` master: in
Vivado this is one AXI DMA, MM2S carrying header and samples, S2MM carrying the verdict. `TLAST` is a
real pin and it is load-bearing — without it a short transfer would *hang* rather than answer, because
a payload word and a header word are the same 64 bits.

## The messages

### `ShotTxHdr` — what you send

One 64-bit word, ahead of the samples on the same stream.

| field | bits | meaning |
|---|---|---|
| `opcode` | 8 | `SHOT_LOAD`, `SHOT_LOOP` or `SHOT_END` |
| `tid` | 16 | transaction id, echoed on the response |
| `nsamp` | 16 | samples the host is sending (0 for `END`) |
| `nrepeat` | 16 | times to play the shot once loaded |

### `ShotTxResp` — what comes back

One 64-bit word, one per header, in order.

| field | bits | meaning |
|---|---|---|
| `tid` | 16 | the header's transaction id |
| `status` | 16 | one of the five verdicts below |
| `nsamp_loaded` | 16 | samples **actually** written to the buffer |

`nsamp_loaded` is what landed, not what was asked for. On `SHOT_LOADED` the two agree; on
`SHOT_SHORT` the difference *is* the diagnosis — and it is a number a DMA cannot give you, because
`sendchannel.transfer()` knows it pushed bytes, not whether they were a whole waveform.

## Two play modes, and what a load does to each

The two opcodes differ in exactly one thing: **when the design stops.**

| | `SHOT_LOAD` | `SHOT_LOOP` |
|---|---|---|
| plays | `nrepeat` passes, then goes quiet | forever |
| `nrepeat` | how many passes | not read |
| a load arriving mid-play | **refused**, `SHOT_BUSY` | **accepted** — it preempts |

That asymmetry is deliberate and it is the design's central decision.

**Preempting a finite shot would silently truncate it.** The host said *play this three times*; a
design that stopped after two produces a perfectly good, shorter signal that every counter downstream
still adds up for. So while a finite shot is in flight, every load — of either opcode — is answered
`SHOT_BUSY` and the memory is not touched.

**Preempting an infinite shot is the only way to ever end it.** A design that answered `SHOT_BUSY`
there would refuse every load forever after the first loop. So a `SHOT_LOOP` in progress yields: the
loader takes the memory, writes the new waveform, hands it back, and the player starts the new one
**from its beginning**.

`SHOT_END` is neither. It is a **fence**: it takes no payload, changes nothing, and answers. There is
no loop to break in a free-running design, so what `END` is worth is what its *response* proves —
responses come back strictly in order, so a verdict for an `END` says everything ahead of it has been
processed. It is a quiescence probe, and it is how a testbench tells a finished run from a
deadlocked one.

## The verdicts

| status | meaning | transient? |
|---|---|---|
| `SHOT_LOADED` | the shot is in the memory and playable | — |
| `SHOT_SHORT` | `TLAST` arrived before the shot was full | no |
| `SHOT_WRONG_LEN` | `nsamp` disagrees with `nword * samp_per_word`, or the opcode is not one of the three | no |
| `SHOT_BUSY` | a **finite** shot is still playing | **yes — retry works** |
| `SHOT_ZERO_LEN` | `nsamp == 0` | no |

**Malformed is decided before transient**, and for a reason: a command that is both wrong *and* badly
timed should be told the thing it can fix. A retry repairs a `SHOT_BUSY`; nothing repairs a length
the buffer was not built for.

**A short shot is loaded and then never played.** It reaches the memory — you can see how much
arrived in `nsamp_loaded` — but the player is told to play zero passes, so half a waveform never
reaches the converter. `SHOT_SHORT` is the status this response exists for: a short DMA transfer
completes *cleanly*, so from the host side a half-loaded buffer is otherwise indistinguishable from a
full one.

## The rules that bite

**1. The output is never silent, and never stalls.** Between shots, during a handover, and after a
finite play-set ends, the player writes a **filler value** (zero) rather than stopping. That is not a
detail: a converter comes due on a fixed grid, and a design that stopped writing would back-pressure
it. Quiet is a *value*, not an absence. The RTL gate asserts the converter's grid never had to
zero-fill a block itself, on both play paths.

**2. A handover costs a gap.** `RfShotTx` holds **one** region and hands it back and forth, so while
the loader owns the memory the player has nothing to play and emits filler. Replacing a looping
waveform therefore produces *old waveform, gap, new waveform* — not a seamless splice. If you need
gapless, you need the streaming family or a two-region TX, and the latter is not built.

**3. A new waveform starts at its beginning.** The read pointer resets on every resume. Splicing the
tail of the old waveform's phase onto the new one would be wrong in every application and invisible
from a word count.

**4. `nsamp` is checked, never trusted.** See `SHOT_WRONG_LEN` above.

## Driving it from a host

The command stream is a file-driven bundle in simulation and an AXI DMA on hardware. The shape is the
same either way:

1. Write the header word, then the payload words, `TLAST` on the last one.
2. Read one response word.
3. Check `status`. On `SHOT_BUSY`, wait and retry — it is the only verdict a retry repairs.

A driver that pushes every frame back to back without reading verdicts will see `SHOT_BUSY` for
everything behind a finite shot. That is the design working, not a limitation of the testbench.

## Next

- [Receive — `RfShotRx`](./rx.md) — the other half of the family.
- [Internals](./tx_internal.md) — the tasks, the lock protocol, the on-wire layouts, and the findings.
  You do not need it to use this design.
- [Choosing a sample buffer](../choosing.md) — whether this is the right family at all.
- The worked example: `examples/rf_shot_tx/`, with its pysim golden and its RTL gate.
