# `boundary_tlast` — converging the free-running flow onto the boundary the rest of the repo already has

**Status: SCOPED HERE, NOTHING BUILT.** Started 2026-08-31. Owns the boundary-stream framing
question across every design; does **not** own `FramedStreamIFSlave` / `FramedStreamIFMaster`
themselves, which shipped in `14f57d9` and are the mechanism this plan *applies*.

---

## Next session starts here — Stage 1

```
claude "Read plans/boundary_tlast.md, section 'Stage 1 — bram_access', and build it.
        The three-way classification in 'What a stream is' decides every port.  Cycle
        counts are MEASURED, never inherited -- see 'Traps'."
```

---

## Why it exists, and it is not the reason it looks like

The obvious framing — *"boundary streams never had TLAST, let us add it"* — is **false**, and
believing it produces the wrong plan.

`waveflow/build/hwgen.py:49-70` has always defaulted `stream_flavor` to `'axi4s'`:

> `'axi4s'` — `hls::stream<streamutils::axi4s_word<W>>` … **The default, and what every
> `control_driven_kernel` emits today.**

So **host-activated kernels have carried TLAST at the boundary the whole time.**
`examples/block_scale` is the visible proof: its top takes
`hls::stream<streamutils::axi4s_word<32>>& s_in`. Anyone whose mental model is *"every boundary
stream is AXI4 with TLAST"* formed that model correctly, from the flow they were working in.

What happened is that the **free-running composite flow** — `composite_gen.py`, built later — chose
the other default, and nobody noticed because for a long time nothing read a frame boundary.
`composite_gen.py:514` emits a plain `hls::stream<ap_uint<W>>` unless a port asks otherwise, and
until `14f57d9` there was no way to ask.

**This plan is therefore a convergence, not a feature.** Two realization flows disagree about what a
boundary port is. The older one is right. The task is to bring the newer one across, deliberately,
one design at a time — not to invent anything.

That reframing matters for scope: the schema layer, the codegen dispatch, the XSI models and the VCD
tooling were all built for `axi4s` already. Most of this is *wiring*, not construction.

## What the current state actually costs

Three costs, all read off the code rather than asserted.

**1. Timing analysis is degraded, silently.** `waveflow/utils/vcd.py:1355-1360`, on
`walk_handshake_bursts` with `last=None`:

> With `None` there is nothing to delimit on, so **all** accepted beats are returned as a single
> burst: inventing boundaries from idle gaps would split a packet at any stall, which is worse than
> declining to guess.

Every unframed boundary port in the repo therefore yields **exactly one burst for the entire run**.
There is no per-frame timing on any free-running composite. `'complete'` (`vcd.py:769`) can never be
`True`, because it is `True` only when closed by an observed TLAST, and `trace.py:32-34` carries
`tlast` in an `_OPTIONAL` set precisely to tolerate its absence.

**2. Error checking has a vocabulary with nothing to say it about.** `streamutils::tlast_status` is
already `{no_tlast, tlast_at_end, tlast_early}` — exactly "this frame ended short". On an unframed
port every answer is `no_tlast`.

**3. PYNQ integration is blocked at the port.** An AXI DMA S2MM channel uses the packet boundary to
know a transfer finished; without TLAST the `recvchannel` waits forever. This is the concrete reason
the question came up, and it is already written down in `FramedStreamIFMaster`'s docstring.

## What must not be lost

**`has_tlast` and `boundary_tlast` are two different facts and must stay two.** The first is about
pysim (is `get()` with no count defined; do bundles carry burst bounds). The second is about RTL (is
there a wire). A stream may legitimately be framed in pysim and unframed at RTL. Collapsing them is
what produced the original confusion; do not "simplify" them back together.

**Framing is a `ClassVar` on a subclass, and that was measured.** An endpoint's attribute set feeds
`structure_signature`, so a per-instance `boundary_tlast` moved every calibration key in the repo the
first time it was tried; `tests/calib/test_key_stability.py` caught it. Keep the subclass form.

**A port with TLAST is different hardware.** Every cycle count in this plan is therefore a
**measurement, not an inheritance**. See Traps.

## What a stream is — the three-way classification that decides every port

Not two categories. Three, and the third is the one a blanket flip would get wrong.

| kind | frame it? | why |
|---|---|---|
| **Host-facing, schema-carrying** (`cmd_*`, `resp_*`, `s_cmd`, `s_done`) | **yes** | The boundary is implied by the message's field count — a `ReadResp` is two words and word two is the last. Nothing to decide, and `write_axi4_stream` already emits it. This is where a DMA needs the pin. |
| **Host-facing, raw payload** (`data_w`, `data_r`, `s_in` payload) | **per design** | A payload has no intrinsic frame. Where does one end — per command? per `nsamp`? never? That is a design decision with a real answer that differs per design, and it cannot be made mechanically. |
| **Converter-facing** (`samp_out`, `rf_loopback`'s `s_in` / `s_out`) | **no** | The RFDC AXIS has no packet concept. A frame boundary here is meaningless and the pin is pure cost. |

`rf_shot_tx` already models this correctly and is the reference: `s_in` and `resp_out` are framed,
`samp_out` is not.

## The machinery that already exists — do not rebuild it

Verified present before any of this plan runs:

- `write_axi4_stream` / `read_axi4_stream` on every schema, with `tlast` in and `tlast_status` out.
- `write_axi4_stream_elem` / `read_axi4_stream_elem` — the array forms.
- `write_framed_stream` / `read_framed_stream` — internal-channel variants (`ap_axis` is refused on
  an internal FIFO, HLS 214-208, so internal framing rides as a bit above the payload).
- Codegen already dispatches on an `axi` flag: `dataschema.py:3478` and `:3517`.
- 45 of 251 generated schema headers already carry the twin.
- `streamutils::axi4s_word<W>` is `ap_axis<W,0,0,0>`; `framed_word` is the internal-only struct.
  These are **not** interchangeable — see Traps.
- The XSI models learn the pin through `port_opt`; `AxisMaster` drives TLAST from `bounds.bin` and
  `AxisSlave` records observed frames back into the bundle it dumps.

## Inventory — every free-running composite boundary port

Derived from `examples/*/gen/*.cpp`. `S` = schema/host, `P` = payload/host, `C` = converter.

| design | ports | proposed |
|---|---|---|
| `bram_access` | `cmd_w` `resp_w` `cmd_r` `resp_r` / `data_w` `data_r` | 4×S frame, 2×P defer |
| `mem_copy` | `s_cmd` `s_done` | 2×S frame |
| `fir_block` | `s_cmd` `s_done` | 2×S frame |
| `interleaver` | `s_cmd` / `m_out` `s_in` | 1×S frame, 2×P defer |
| `rf_blk_delay` | `rx_resp` `tx_resp` / `s_in` `s_out` | 2×S frame, 2×C none |
| `rf_loopback` | `s_in` `s_out` (and `_0` / `_1`) | C — none |
| `rf_relayout` | `s_in` `s_out` | C — none |
| `rf_repeat_play` | `cmd_in` `resp_out` / `wave_in` `samp_in` `samp_out` | 2×S frame, rest C/P |
| `rf_samp_buf_rx` | `s_cmd` `s_resp` / `s_in` `s_out` | 2×S frame, 2×C none |
| `rf_samp_buf_tx` | `s_resp` / `s_in` `s_out` | 1×S frame, 2×C none |
| `rf_shot_tx` | `s_in` `resp_out` framed; `samp_out` not | **done — the reference** |
| `block_scale` | `s_in` | **already `axi4s` — host-activated flow** |

Roughly **16 schema ports across 9 designs** in scope for Stage 2, and no converter port moves at
all.

## Stages

### Stage 1 — `bram_access`, the proving design

First because it is the deployment target, because its six ports split cleanly 4/2 across the first
two categories, and because its gate is a witness that must survive.

- Flip `cmd_w`, `resp_w`, `cmd_r`, `resp_r` to `FramedStreamIF{Slave,Master}`.
- Leave `data_w` / `data_r` plain, and **say why on the ports** — the payload's extent is carried in
  `nsamp`, so a frame adds nothing the command does not already state.
- Change the affected hand-written bodies from `read_stream` / `write_stream` to the `axi4_stream`
  twins. Note that the same bodies also touch internal `ap_uint` FIFOs, which must **not** change.
- Re-measure the XSI gate. It is currently 568. **Do not assume it stays 568.**
- Confirm the witness values (`100, 101, 107, 355, 228`) are byte-identical. They are the one gate
  checking Waveflow against something built independently of Waveflow.

Done when: `-m xsi` green with the gate count re-recorded, witness unchanged, and the wrapper shows
four TLAST pins and no more.

### Stage 2 — the remaining eight designs, one commit each

Same shape, mechanical once Stage 1 establishes the pattern. One design per commit so a moved cycle
count is attributable to the design that moved it. Converter-facing ports are explicitly **not**
touched, and each design's prose should say so — otherwise the next reader re-opens the question.

### Stage 3 — the guard that makes it un-recurrable

**Nothing today ties pysim framing to the RTL pin.** Verified: no reference to `has_tlast` or
`boundary_tlast` in `codegen_check.py`, `elaborate.py`, or `hw_module.py`.

The direct case is already caught by C++ — a body reading `.last` off an `ap_uint` port is a compile
error, which is why `ShotTxLoad` surfaced as *"the body cannot be written"* rather than as a mystery.
That is a good failure mode and needs no help.

What is **not** caught is the deployment case: a kernel that does not need the boundary while its
*host* does. `bram_access` is exactly that — it counts `nsamp`, gates green at RTL, and fails only in
Vivado when a DMA waits for a packet that never ends. A `check()` gate cannot decide this, because it
is a fact about the deployment target rather than about the design.

So Stage 3 is **not** a correctness gate. It is a `check()` warning plus a docs rule: a host-facing
schema stream that is not framed must say why, in the same way a converter-facing one must say why it
is not. Make the silence impossible, not the configuration.

## Traps

**`framed_word` is not `axi4s_word`, and the commit message of `14f57d9` says otherwise.** That
message claims a framed port's decl becomes `streamutils::framed_word<W>` "which is what makes Vitis
emit `<port>_TLAST`". It is inverted. `composite_gen.py:514` emits `axi4s_word` (an `ap_axis`), and
`framed_word` is the plain `{data, last}` struct that exists *because* Vitis rejects `ap_axis` on an
internal FIFO. `framed_word` at a boundary compiles and packs into one wide TDATA **with no pin at
all** — the exact silent failure this plan is about. The code is right; the message is wrong; fix the
message before someone follows it.

**Cycle counts are measurements.** A TLAST pin is a flip-flop and a scheduling fact. Every gate in
`tests/examples/*_xsi.py` (10 files, `WANT_XSI_GATES=63`) that touches a flipped design must be
re-run and re-recorded with its new number and the reason. Inheriting a count across this change is
the failure mode `plans/xsi_staleness_and_silent_skips.md` exists to prevent.

**Calibration keys move for flipped designs, and only for those.** That is the subclass working as
designed. Expect `tests/calib/test_key_stability.py` to want new keys for exactly the designs in the
inventory above, and treat a key moving for a design *not* in that list as a real defect.

**A task body wired into a composite must use the `'word'` flavor for internal FIFOs.**
`hwgen.py:57-59`: an `axi4s_word` body will not bind to an `ap_uint` FIFO. Flipping a *boundary* port
must not flip the internal channels the same body also touches.

**The IPI cost of an unused pin is close to zero, contrary to the docstring.**
`FramedStreamIFSlave`'s prose justifies opt-in partly with "an unused TLAST pin is still a wire
someone has to connect in a block diagram". In Vivado IPI you connect AXIS *interfaces* as bundles
via `connect_bd_intf_net`, so an extra TLAST inside the bundle costs nobody anything; the argument
holds only for pin-level or hand-written RTL integration. *(Assessment, not measured in Vivado —
confirm before leaning on it.)* This does **not** change the recommendation to stay opt-in for
payload streams, which rests on the frame boundary being undecidable there, not on wiring cost.

## Not in scope

- Changing `has_tlast`'s meaning, or its default.
- Framing internal channels. That is a separate decision made on the channel (`StreamIF.framed`),
  already available, and unaffected either way.
- Any converter-facing port.
- The `bram_access` Vivado / PYNQ deployment itself. This plan removes one blocker from it; it is not
  that plan.
