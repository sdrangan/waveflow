# Example accelerator prompts (streaming, in-band)

These are example specifications of the kind a student would hand to an AI
coding tool in the "design your own accelerator" lab. The AI builds each
design **the Waveflow way, with the full machinery**: schemas, a
`HostActivated` `HwModule` with a timing model, a hand-written C++ compute
hook, a generated kernel and testbench, and a `BuildDag`. The reference
design is Waveflow's `examples/stream_inband`.

| File | What it is |
| --- | --- |
| [frame.md](frame.md) | The part that stays the same for every design: protocol, errors, flow, the two stages, the three comparisons, the report. In a course, the course supplies this file and the student writes only the function spec. |
| [00_gain_clip_simple.md](00_gain_clip_simple.md) | Gain and clip, **deliberately under-specified**, with no frame. Shows what the AI decides silently. |
| [01_gain_clip.md](01_gain_clip.md) | Gain, round, clip, count clips. Rounding and saturation stated bit-exactly. |
| [02_fir.md](02_fir.md) | FIR filter with up to 16 taps. State, latency and "streaming" as measurable claims. |
| [03_cmag_peak.md](03_cmag_peak.md) | Complex float magnitude with a peak search. Tolerance-based checking; near-ties and threshold edges. |

## Why the frame is modeled on `stream_inband`

The protocol is `stream_inband`'s: parameters in a `VitisRegMap`, `ap_start`,
then a persistent loop over `DATA` commands that ends on `END`, halting with
`halted`/`error`/`tx_id` on an error. This was the architecture that won out
after a long comparison of alternatives. Because the AI's only reference is
that example, every departure from it is a likely failure point. The frame
therefore departs in just four places, each marked **(new)** in
[frame.md](frame.md):

1. **A response footer** for per-transaction results (`nsamp_read` plus the
   function's statistics). The register map can hold only the last
   transaction's values, and the loop runs many transactions.
2. **TLAST on the last emitted data word, even after an early input
   TLAST.** `stream_inband` emits no output TLAST in that case.
3. **A `BAD_PARAM` error code** (6), checked at each `DATA` command.
4. **The command-header framing codes (1, 2) are listed as reserved.**
   `stream_inband` doesn't detect them either (the generated header read
   discards TLAST); the frame just says so explicitly.

## The three comparisons

In the Waveflow flow, the Python model and the kernel derive from the same
source, so "csim matches pysim" cannot catch a model that misreads the spec.
The frame therefore requires an **independent oracle** in Stage 1, written in
plain numpy without importing the accelerator module, and it accepts a
design only when all three comparisons pass:

| Comparison | Catches |
| --- | --- |
| oracle vs pysim | the model misread the spec |
| pysim vs csim/cosim | the hook doesn't match the model |
| pysim timing vs cosim cycles | the timing model is wrong |

When something fails, students have to say which layer failed. The third row
is a question the hand-written flow cannot ask.

## The under-specified prompt

[00_gain_clip_simple.md](00_gain_clip_simple.md) is the same function as 01,
written the way a student would write it on a first try. The point is not
whether the AI can build it; it probably can. The point is **which decisions
it makes without telling you**. Students run it, then audit the result
against this list, or against 01 plus the frame, which pin down every item:

| Decision the prompt leaves open | Pinned in 01 + frame as |
| --- | --- |
| type of `x` and `y` | int16 in, int16 out |
| Q8.8 × int16 product: the rounding mode on the shift back | round half up (ties to +inf) |
| whether `gain * x` can overflow before the clip | exact product, clip after |
| what happens if `lo > hi` | `BAD_PARAM`, halt |
| where the parameters live | **register map**. 00 says "command header", which is *not* how `stream_inband` does it, so a Waveflow-aware AI must deviate or push back. |
| word width, samples per word, odd-count padding | 32-bit, two per word, as in the Waveflow array utilities |
| framing, TLAST, what counts as malformed and what happens then | frame F3/F4 |
| any per-transaction status at all | response footer with clip counts |
| part, clock, tool version | xc7z020clg484-1, 10 ns, 2025.1 |
| "error vs floating point": which metric, against what bound | (only 00 asks) |
| any timing or resource target; the timing-model tolerance | II=1 per word, `T_total`, 20-cycle pysim/cosim match |
| who writes the tests, and whether the AI may change them | two-stage freeze, independent oracle |

Two traps are worth pointing students to:

- **"Bit-exact with a Python model" is trivially satisfied when the AI writes
  both.** In the Waveflow flow it is *doubly* trivial, because the kernel is
  derived from the model.
- **The floating-point comparison needs a metric the AI must choose.** Its
  choice shows whether it understood that clipping is not error.

00 also says nothing about Waveflow. Whether the AI uses the Waveflow
machinery at all then depends on what the MCP server and `AGENTS.md` tell it,
which is itself worth observing.

A good exercise is to run 00 on two different AI tools, diff the choices, and
then have students write the spec that would have made the two agree. That
spec should come out looking like 01.

## Things for the instructor to settle

- **Vitis version.** The frame says 2025.1; change it to match the lab
  machines and ECS.
- **Resource budgets.** These are my guesses, not measured. Set them after
  one run of each design.
- **Multiple transactions per run.** The `SeqTB` can express
  `DATA`, `DATA`, `END`, but only **pre-loaded**: Vitis csim needs every
  transaction in the input stream before the kernel runs. That is fine for
  every scenario here, because none makes an input depend on an earlier
  output. A student design that *does* need that (closed-loop control,
  adaptive parameters) needs a `FreeRunMod` with the concurrent BFM
  testbench, which is a different frame and a harder lab.
- **VCD timing.** `T_total` and `L_first` assume the AI can find and use
  Waveflow's VCD tooling.

## Status

None of these has been run through an AI tool yet. The next step is the
baseline: give each prompt to Claude, Codex and Gemini with **no new MCP
tools**, and record where each gets stuck. Those points decide the tool set,
the scaffold and `AGENTS.md`.
