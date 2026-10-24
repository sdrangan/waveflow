# Stream pipeline frame (read this before the spec)

A **stream pipeline** is one accelerator built of two or more free-running stages that stream to each
other: one input stream, the results and a status out, no memory and no host on a bus.  This file says
what the frame **fixes** and what a spec **must decide** -- the checklist Stage 1 answers.  Where the spec
and this frame disagree, the spec wins; say so in Stage 1.

The pattern is command-response (`docs/guide/systems/command_response.md`, its pipeline form); the
references are `block_eq` (a broadcast, two stages, a link) and `stream_kernel` (one stage, the protocol,
the harness).

## Fixed by the frame

1. **A composite `FreeRunMod`**: a child per stage, added with `add_comp`; internal links
   `StreamIF(framed=True)` with an explicit `depth`, added with `add_if`; a `boundary` list re-exporting
   the children's outer ports.  The composite has no `run_iter` of its own.
2. **The protocol at the boundary** is `stream_kernel`'s: `s_in` a command and its data; `m_out` a `Resp`
   header and the results; `m_status` one `Status` per command.  An error is fatal.
3. **The command travels with the data**: each stage forwards what the next needs -- the command's
   identity, and any error it found -- so **the last stage answers**, including for an error an earlier
   stage saw.
4. **Two stages needing parts of one input** get a copy each from the framework's `StreamBroadcast`
   (`docs/guide/streams/broadcast.md`), and **each keeps consuming its own copy**, skipping what is not
   its own -- never waiting on a sibling for something queued behind its own unread data.
5. **One hand-written body per stage**, behind `kernel_task()` with `framed=` naming its ports, so the
   generator supplies each port's word type: the same body runs as a stage alone and inside the
   composite.  It reads and writes through `streamutils::read_msg` / `write_msg` and the element types'
   `read_boundary_lane` / `write_boundary_lane`
   (`docs/guide/comp_codegen/freerunning_override.md`, *One body, wherever it sits*).
6. **Verification, stage by stage**: each stage in its own harness (`harness(Stage, workload=...)`),
   driven by the messages the golden says its neighbour sends; then the composite's harness; pysim
   against the golden, then RTL against pysim (`compare`), every recording.

## Decided by the spec

- the stages, what each computes, and why the split is where it is;
- each internal link: what it carries per command, its layout, its width and its depth;
- the boundary messages, the operation bit-exactly, the errors and which stage detects each;
- the word width and the elements per word;
- the part and the clock, if the spec sets a clock: csynth targets a Zynq-7020 at 100 MHz unless the
  top's script names others (`render_tcl(top, part=..., period_ns=...)`; `RFSOC4X2_PART` at 4 ns is
  the framework's fast part) -- a 7020 does not close 250 MHz;
- the scenarios, their expected outputs, and any throughput target.
