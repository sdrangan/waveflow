# Shared kernel frame (read this before the spec)

A **shared kernel** is one expensive kernel K used by several clients, or whose output comes back to its
own input, built as one composite: an arbiter in front of K, a tag beside it, the results routed back.
This file says what the frame **fixes** and what a spec **must decide** -- the checklist Stage 1
answers.  Where the spec and this frame disagree, the spec wins; say so in Stage 1.

The pattern is `docs/guide/systems/shared_kernels.md` (*Sharing a kernel*): read it whole first.  The
references are `os_filterbank` (one FFT shared by three channels, forward then inverse) and `block_eq`
(a composite, its links, each stage alone).

## Fixed by the frame

1. **A composite `FreeRunMod`** as in `stream_pipeline`: children (`add_comp`), links
   `StreamIF(framed=True, depth=...)` (`add_if`), a `boundary`; no `run_iter` of its own.
2. **The clients merge in a `StreamArbiter`**, whole frames; feedback, if any, on input 0 with
   `policy="priority"`, so work in the loop finishes before new work enters.  A frame reaches it only
   once complete: whatever produces one reads all of it first.
3. **A tag travels beside K**, one word a frame, from the stage before K to the stage after it, in the
   order the frames enter K -- or in the frame's header if K passes it through.
4. **The results return by `StreamDemux`**: each client its own output and status ports, so each port's
   recording does not depend on timing and RTL can be compared with pysim.
5. **The loop is bounded by credit**: a client sends a frame only with a credit, the stage where its work
   leaves the loop returns it, and every loop link's depth holds what the credits admit -- stated
   beside the link.  The stage after K never blocks on a result or feedback write.
6. **A stage that polls several inputs** is modelled with `wait_any` on the inputs it can act on now.
7. **Verification**: each stage alone in its harness, then the composite, pysim against the golden and
   RTL against pysim; every gate asserts counts; one test shows the loop deadlocking without its
   credit while the run still returns.

## Decided by the spec

- K, the clients, and why K is shared rather than replicated; which of the six ways
  (`shared_kernels.md`) and why;
- the tag's fields; each link's width and depth, with the credit that bounds it;
- the policy (priority or round-robin), and for a feedback path its priority;
- what lives at K in slots (loaded once) and what travels with each frame;
- the boundary messages per client, the operation bit-exactly, the errors and which stage detects each;
- the part and the clock: csynth targets a Zynq-7020 at 100 MHz unless the top's script names others
  (`render_tcl(top, part=..., period_ns=...)`; `RFSOC4X2_PART` at 4 ns is the framework's fast part,
  the one `SsrFft` is built for);
- the scenarios: more work per client than its credit admits, clients of different lengths so they
  finish out of order, and their expected outputs per client.
