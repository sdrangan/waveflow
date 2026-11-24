---
name: waveflow-shared-kernel
description: Share one expensive Waveflow kernel among several clients, or feed a kernel's output back to its own input -- the arbiter in front of it (StreamArbiter), a tag beside it, results routed back (StreamDemux), credits that bound the loop, a feedback frame announced only once complete, resident data in slots, an FFT whose direction changes per frame (SsrFft direction="runtime"), a pool of identical workers each job sent to a free one (StreamDispatch), wait_any for a polling stage's model, and why a deadlock on a cycle looks like success. Use whenever a Waveflow design has one unit used by several channels, jobs or passes, several copies of a unit sharing one stream of jobs, a cycle inside a composite, a mux or arbiter in front of a kernel, out-of-order results to return, or a loop that hangs.
---

# Waveflow: sharing a kernel

A kernel **K shared by several clients**, or one whose output comes **back to its own input**: one FFT
used forward then inverse by every channel, a processing element serving several flows. A composite
with an arbiter in front of K and, often, a cycle. Its stages are kernels (`waveflow-kernel`, their HLS
bodies `waveflow-kernel-hls`), its links a pipeline's (`waveflow-pipeline`). This skill is the sharing.

**The concept page is the spec of this pattern: read it first, whole.**
`docs/guide/systems/shared_kernels.md` (*Sharing a kernel*), with `waveflow_get_doc` (MCP) or the
checkout. Then the example it points to. This skill does not restate them; it says what to read before
which step, and what to check at the end.

## Before which step, which page

| step | read |
|---|---|
| replicate K, or share it; which of the six ways; the four questions | `systems/shared_kernels.md`, the top three sections |
| the return path: K never blocks on a result; separate channels; poll, never write-then-wait; a feedback frame whole; a unit that flushes alone | `systems/shared_kernels.md`, *The return path* |
| merging the clients into K | `streams/arbiter.md` (`StreamArbiter`: whole frames, priority or round-robin, the index on `m_sel`) |
| returning each result to its client | `streams/demux.md` (`StreamDemux`: by `s_sel`, or by a header field) |
| a pool: M copies of K, each job to a free one | `systems/shared_kernels.md`, *A pool of workers*; `streams/dispatch.md` (`StreamDispatch`: credits, first-free or round-robin) |
| whose frame is this: the tag beside K | `systems/shared_kernels.md`, *The blocks* |
| one FFT, both directions | `dsp/ssr_fft/parameters.md`, *The direction chosen per frame* |
| a cycle: credit, link depths, feedback first, why counts | `systems/shared_kernels.md`, *A cycle inside a composite* |
| a stage that polls several inputs, in pysim | `systems/shared_kernels.md`, *Modelling a polling stage*; `interface/primitive/index.md` (`wait_any`) |
| the results, per command | `systems/command_response.md` |
| vendor switches and `streamNToOne` | `systems/shared_kernels.md`, *Why not AMD's blocks* |

**The reference design is `os_filterbank`**: `docs/examples/os_filterbank/index.md`, then its files
(`waveflow_get_example("os_filterbank", file=...)`: `os_filterbank.py`, `os_filterbank_build.py`,
`src/os_front_task.h`, `src/os_pre_task.h`, `src/os_post_task.h`). Copy its shapes. The smallest loop,
to read for the mechanics alone, is `examples/ring_stub/`.

## When Waveflow has no block for it

Use the library's blocks (`StreamArbiter`, `StreamDemux`, `StreamDispatch`, `SsrFft`) before writing your own. When the
design needs one that does not exist -- another policy, another unit with a run-time option -- do not
bend the design around the gap: propose the extension (what, where, its model, pysim module, HLS body,
tests and guide page), following the closest existing block. In a design outside the Waveflow
repository, ask the user before changing Waveflow.

## Before you finish, check your own work

- [ ] Replicating K, and a pool of copies (`StreamDispatch`), were considered, and the choice among the
      six ways is written down with why.
- [ ] Every client's frame reaches the arbiter whole: whatever produces one reads all of it first.
- [ ] The stage after K can never block on a result or feedback write: a credit bounds what is in the
      loop, and every loop link's depth holds what the credit admits, stated beside the link.
- [ ] Feedback has priority over new work, or the reason it does not is written down.
- [ ] Each frame's tag reaches the stage after K in the order the frames enter K.
- [ ] Each client's outputs have their own port, or the order on a shared one is shown not to depend
      on timing -- otherwise RTL and pysim cannot be compared.
- [ ] A polling stage's pysim waits with `wait_any` on only the inputs it can act on now.
- [ ] Every gate asserts counts -- every response, every status -- and one test shows the loop
      deadlocking without its credit, the run still returning.
