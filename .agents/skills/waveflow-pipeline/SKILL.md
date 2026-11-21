---
name: waveflow-pipeline
description: Build a Waveflow composite kernel -- a pipeline of child kernels that run concurrently, wired by internal streams -- deciding when to split a kernel into stages, the children and links (add_comp, add_if, boundary), each link's depth and width, StreamBroadcast when two stages need one stream (drain your own copy before waiting on a sibling), a token per job through every stage, the command travelling with the data so the last stage answers, and testing each stage alone then the whole. Use whenever a Waveflow design has stages, a composite FreeRunMod, internal streams between kernels, or a pipeline that hangs.
---

# Waveflow pipelines: composite kernels

A **composite kernel** is a `FreeRunMod` with child kernels (`add_comp`) wired by streams (`add_if`)
and no body of its own; its children run concurrently, a task each, so job *j+1* is in one stage while
job *j* is in the next (`systems/kernels.md`). Every stage is a kernel: a leaf is the `waveflow-kernel`
skill (its HLS body `waveflow-kernel-hls`). A composite whose children include a memory reader or
writer, or that sits behind bus views, is a **device**: that part is `waveflow-device`. This skill is the
pipeline itself -- its links and the rules that keep it from hanging.

## The rules (all of them, every time)

1. **Split only for concurrency.** A composite when stages should overlap (load / compute / store, or a
   part that needs a whole job before the next part can start); otherwise one leaf is simpler. A body
   or children, never both.
2. **Declare the graph, nothing more.** `add_comp` each child (the order is the task order), one framed
   `StreamIF(..., framed=True, depth=D)` per link with `add_if`, `self.boundary = [...]` naming the
   unwired child ports in `add_comp` x `add_endpoint` order, and attributes aliasing those ports
   (`self.s_in = self.first.s_in`). The composite has no `run_iter`.
3. **Every link's depth is stated and sized** against what can pile up in it -- a burst of results
   while the next stage is busy, what is queued ahead of a message a stage waits for. Never a silent
   default. A link may be narrower or wider than the boundary (`systems/streams.md`, *Between kernels*).
4. **The command travels with the data; the last stage answers.** Each stage reads the command (its
   own copy, or one forwarded ahead of its input), does its share, and forwards what the next stage
   needs ahead of its output. One status per job comes from the last stage; an error found early
   travels in the forwarded message to it.
5. **A token per job through every stage.** A stage that consumes a job and emits nothing -- no result,
   an empty result, an error -- leaves the next stage waiting forever. Forward a message for every
   job, always. An empty data burst cannot be sent (a zero-element read has no answer): say "none" in
   the header's count and send no data burst, or end with an in-band END.
6. **Two stages need one stream: `StreamBroadcast`.** Each reads its copy and skips what is not its
   own. Its outputs move in **lockstep**: a stage that waits on a sibling must first consume
   everything queued ahead in its own copy (into a local buffer of a stated maximum), or its copy's
   link must be deep enough to hold all of it -- state which, and why. Otherwise the copy fills, the
   broadcast stalls, the sibling starves, and nothing moves.
7. **pysim may not show rule 6.** pysim admits a burst longer than a link's depth once the link has
   room for part of it, so a stage waiting with one long burst unread in its copy passes in pysim and
   hangs at RTL. Check the read order and the depths by reading the code, not by a passing run.
8. **The command paces the pipeline.** Each stage waits for its job's command (or token) before it
   takes the job's data, so at most a job's worth is in flight per stage. Across a shared bus, a stage
   writing another device's queue waits on credit (`waveflow-device`).
9. **Test bottom-up and count.** Each stage alone in its harness (`harness(Stage, workload=kind)`, its
   workload giving every input stream, the sibling's included), then the composite. Assert the number
   of bursts on every output; place each error mid-scenario with jobs after it. A deadlock looks like a
   run that ended.

## Which page to read before which step

Read the page **before** writing the code, with `waveflow_get_doc` (MCP) or the `docs/guide/` checkout.

| step | page |
|---|---|
| leaf or composite; a composite's children | `systems/kernels.md` |
| declaring the graph: `add_comp`, `add_if`, `boundary`; when to split | `flows/concurrent_python.md` |
| between kernels: stream blocks, depths, widths, a token per job, credit | `systems/streams.md` |
| the command through a pipeline; the last stage answers | `systems/command_response.md` |
| `StreamBroadcast`: lockstep, keep consuming, its build step | `streams/broadcast.md` |
| how a composite lowers to one HLS top, a task per child | `comp_codegen/freerunning_composite.md` |
| each stage alone, then the parent | `flows/units_harness.md`, `flows/units_composing.md` |
| what each test compares; counting | `systems/verification.md` |

Worked examples: `block_eq` (a broadcast, two stages, the second draining its copy first, link depths
stated), `interleaver` (load / compute / store, the command as an internal descriptor, pacing),
`memcpy` (a three-stage device, the writer answering). If a name is not on those pages,
`waveflow_search` it -- **do not guess an API name.**

## The shape

```python
@dataclass
class Pipe(FreeRunMod):                         # a composite: children and links, no body
    cpp_kernel_name: ClassVar[str | None] = "pipe"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bcast = StreamBroadcast(name=f"{self.name}_bcast", sim=self.sim, n_out=2, bitwidth=W,
                                     clk=self.clk)
        self.a = StageA(name=f"{self.name}_a", sim=self.sim, clk=self.clk)   # reads copy 0, sends m_tok
        self.b = StageB(name=f"{self.name}_b", sim=self.sim, clk=self.clk)   # copy 1 + s_tok; answers
        for c in (self.bcast, self.a, self.b):
            self.add_comp(c)
        for name, m, s, depth in (("copy_a", self.bcast.m_out_0, self.a.s_in, DA),   # each depth: why
                                  ("copy_b", self.bcast.m_out_1, self.b.s_in, DB),
                                  ("tok", self.a.m_tok, self.b.s_tok, DT)):
            link = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=W,
                            framed=True, depth=depth)
            link.bind("master", m)
            link.bind("slave", s)
            self.add_if(link)
        self.boundary = ["s_in", "m_out", "m_status"]
        self.s_in, self.m_out, self.m_status = self.bcast.s_in, self.b.m_out, self.b.m_status

# StageB.run_iter: its copy of the command, then ALL of its copy of the job's data (rule 6),
# then the token from A (one per job, errors included), then the response, the data, the status.
```

## Before you finish, check your own work

- [ ] The composite has children, links and a boundary, and no body; each stage is a leaf kernel.
- [ ] Every link has a stated depth, sized by what can queue in it.
- [ ] Every stage emits something for every job, errors and empty results included.
- [ ] No stage waits on a sibling while its own broadcast copy still holds data queued ahead
      (or that copy's link holds a whole job, stated).
- [ ] The last stage answers: one response and one status per job.
- [ ] Each stage tested alone, then the whole; every output's burst count asserted; a passing pysim
      run is not taken as proof that rule 6 holds.
