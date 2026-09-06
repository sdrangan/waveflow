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

```mermaid
flowchart LR
    HOST["host<br/>(AXI DMA)"]
    MEM[("BRAM")]
    RFDC["Rfdc"]

    subgraph K["RfShotTx — one kernel"]
        L["ShotTxLoader"]
        P["ShotTxPlayer"]
        R["re-layout<br/>wired for you"]
    end

    HOST -- "ShotTxHdr, samples, TLAST" --> L
    L -- "ShotTxResp" --> HOST
    L <-- "lock: take, write, give back" --> MEM
    P <-- "lock: play, yield on request" --> MEM
    P --> R
    R -- "samp_out" --> RFDC
```

**The memory is inside the design but outside the kernel**, which is why the diagram draws it apart
from the box. It is hand-written Verilog a generated wrapper joins to the tasks: the generated HLS
kernel cannot contain it, because Vitis turns an array shared between two tasks into a synchronizing
channel and refuses one port used both ways. Nothing outside `RfShotTx` sees it either way — see
[the boundary](#the-boundary).

**Neither task owns the memory outright.** Both reach it through the same lock, and that is what lets
a new waveform be loaded while an old one is still playing — see
[two play modes](#two-play-modes-and-what-a-load-does-to-each).

The re-layout stage between the player and `samp_out` re-packs samples from the dense packing the
memory holds into the slot packing the converter wants. You do not wire it; the composite does.

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

### Why `for_word` and not a constructor argument

**The converter's word type *is* the parameter** — it is just passed to a classmethod rather than a
field, and that is forced rather than preferred. `HwModule.__post_init__` wraps every `HwParam` in
`HwParamValue(int(value))`, so **a type cannot survive as one**. `for_word` is the seam where the
type becomes the three integers the module actually stores:

| derived from `word` | what it fixes |
|---|---|
| `bitwidth` | the AXI-Stream width of all three ports, and the memory's |
| `samp_per_word` | how many samples ride in one beat |
| `shift` | how far a sample sits above the bottom of its converter slot |

So you never type a width, and the buffer cannot disagree with the converter about packing. What is
left for you to decide is the **geometry**: `depth`, `nword`, `base` and `blk_words`.

### The geometry, in the units it is actually in

| | unit | meaning |
|---|---|---|
| `depth` | **words** | how big the memory is |
| `nword` | **words** | how big one shot is |
| `base` | **words** | where this shot's region starts |
| `blk_words` | **words** | words per chunk — the lock poll period |
| `nsamp` | **samples** | `nword × samp_per_word`, and the only value the header may carry |

{: .warning }
**The numbers above are a trap, and it is worth naming.** With `depth=256` and `samp_per_word=4`,
`depth / samp_per_word` happens to equal `nword`. **That relationship does not exist.** `depth` and
`nword` are both word counts and `samp_per_word` converts words to samples — the arithmetic mixes
units and only lands on 64 by coincidence of this configuration. A shot is as long as you declare it,
independently of how big the memory is. These are the gated numbers, so they stay; the coincidence is
called out rather than designed away.

**`nword` is build-time structure, not a command field.** How long a shot is, is declared once, here.
A header that disagrees is *refused*, never truncated — that is what `SHOT_WRONG_LEN` is for. `nsamp`
exists on the header because it is what the **host believes** it is sending, and catching that belief
disagreeing with what arrived is the response's whole job.

### Why `base` is not zero

Not to leave memory unused, and **not because multiple regions need it** — the lock speaks in
addresses, so a design that wants two regions asks for `[0, 128)` and then `[128, 256)` at runtime and
needs no build-time parameter at all. That is exactly what [`RfShotRx`](./rx.md) does: it has
`N_REGION = 2`, computes each region's bounds itself, and puts the base address **on the wire** in its
window header. It has no `base` parameter.

`base` on `RfShotTx` is a **build-time placement of the one region this design asks for**, and the
honest reason the example makes it non-zero is that it exercises the offset arithmetic. `base +
offset` is the shape of the byte-versus-word addressing bug that had every BRAM design in this repo
mis-addressed while `bram_toy` stayed green — because in a small enough region every address is in
range either way, so the design round-trips perfectly right up to the point its memory wraps. Setting
`base = depth − nword` puts the region at the very top, which is the placement that catches it.

So: any `base` that fits works, `0` included. It is a parameter because a design that always placed
its region at zero would never test the arithmetic that a design placing it elsewhere depends on.

**`base + nword` must fit inside `depth`**, and `blk_words` must divide `nword` so a chunk never
straddles the end of the region.

{: .note }
**`dac_word_rate` is a modelling input, not hardware.** At RTL the player is paced by `TREADY` and
needs nothing. In pysim it still has to be handed over — **but not for the reason this note used to
give.** It said *"pysim does not back-pressure a burst write"*, and since
`plans/pysim_burst_backpressure.md` S2 that is false: a burst write now blocks until its consumer has
room. What back-pressure paces is the **rate**, and measured (S3) that is not sufficient on its own:
with the metronome removed the throughput stays right — no underrun, the same number of blocks
delivered — while the player runs *ahead* of the data and fills the downstream queues with filler, so
the first real sample appears three times later than it does at RTL. Back-pressure controls how fast
the player may go, not how far ahead of the shot it may get. The rate is
`samp_rate / samp_per_word` — which the design could in principle derive rather than have you
compute, and that it does not is a known wart rather than a decision.

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

## The protocol

Every exchange is the same three moves: **a header, its payload, one verdict.** What differs is what
happens to the playout while that is going on.

### The verdict answers the load, not the playout

This is the one thing most likely to surprise you. `ShotTxResp` goes out as soon as the payload has
landed and the memory has been handed back — **before the first sample reaches the converter**, and
long before the last one does. It tells you whether the *transfer* was good. It does not tell you the
waveform has finished playing.

```mermaid
sequenceDiagram
    participant H as host
    participant T as RfShotTx
    participant D as Rfdc
    H->>T: ShotTxHdr(SHOT_LOAD, tid=1, nsamp, nrepeat=3)
    H->>T: payload words, TLAST on the last
    T-->>H: ShotTxResp(tid=1, SHOT_LOADED, nsamp)
    Note over T,D: only now does anything play
    T->>D: pass 1 of 3
    T->>D: pass 2 of 3
    T->>D: pass 3 of 3
    Note over T,D: goes quiet — filler, never silence
```

There is no "playout finished" message. A host that needs to know a finite shot is done discovers it
by being accepted again: while it is still playing, every load is answered `SHOT_BUSY`.

### Replacing a waveform that is playing forever

A `SHOT_LOOP` never ends on its own, so a new load is the only thing that can end it. The player
yields the memory, the loader writes into it, and the player picks the new waveform up **from its
beginning**.

```mermaid
sequenceDiagram
    participant H as host
    participant T as RfShotTx
    participant D as Rfdc
    H->>T: ShotTxHdr(SHOT_LOOP, tid=1, nsamp)
    H->>T: payload A, TLAST
    T-->>H: ShotTxResp(tid=1, SHOT_LOADED, nsamp)
    T->>D: waveform A, repeating
    H->>T: ShotTxHdr(SHOT_LOOP, tid=2, nsamp)
    H->>T: payload B, TLAST
    Note over T,D: player yields the region; filler goes out meanwhile
    T-->>H: ShotTxResp(tid=2, SHOT_LOADED, nsamp)
    T->>D: waveform B, from its start, repeating
```

**The gap is real and it is filler, not silence.** The converter is fed a defined value throughout
the handover — see [the rules that bite](#the-rules-that-bite).

### The refusal you are expected to hit

`SHOT_BUSY` is not an error. It is the design protecting a finite shot from being truncated
mid-flight, and it is **the only verdict a retry repairs** — the other four are faults in the command
and will fail identically forever.

```mermaid
sequenceDiagram
    participant H as host
    participant T as RfShotTx
    Note over T: a finite shot is still playing
    H->>T: ShotTxHdr(SHOT_LOAD, tid=2, nsamp)
    H->>T: payload, TLAST
    T-->>H: ShotTxResp(tid=2, SHOT_BUSY, 0)
    Note over H: wait, then send the same frame again
```

**Send the payload even when you expect a refusal.** The design drains it whatever the verdict, and a
frame left half-consumed makes its leftover words the *next* header — after which every command is
garbage for reasons that look nothing like the cause.

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

[The protocol](#the-protocol) is the same whether the command stream is a file-driven bundle in
simulation or an AXI DMA on hardware. What changes is only the transport:

| | simulation | hardware |
|---|---|---|
| `s_in` | a bundle of frames on disk | AXI DMA **MM2S** |
| `resp_out` | a bundle the sink writes back | AXI DMA **S2MM** |
| `samp_out` | the `Rfdc` model's DAC process | the RFDC IP |

One DMA carries both directions, which is why the payload rides in-band on `s_in` rather than
arriving through a second port or an `m_axi` master.

**A driver that pushes every frame back to back without reading verdicts** will see `SHOT_BUSY` for
everything queued behind a finite shot — the frames are consumed and refused, not held. That is the
design working, not a limitation of the testbench, and it is why a scenario that wants two accepted
finite loads needs the verdicts read between them.

## Next

- [Receive — `RfShotRx`](./rx.md) — the other half of the family.
- [Internals](./tx_internal.md) — the tasks, the lock protocol, the on-wire layouts, and the findings.
  You do not need it to use this design.
- [Choosing a sample buffer](../choosing.md) — whether this is the right family at all.
- The worked example: `examples/rf_shot_tx/`, with its pysim golden and its RTL gate.
