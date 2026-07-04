This document gives the sequence of examples in the repo.  Each example has "vehicle", meaning some hardware to be built to demonstrate concepts.  The other bullet points are the new key concepts learned in each example.  You can see that there are two critical options for rowwise-fir:  

- regmap:
   - vehicle:  simple relu
   - Simple AXI-Lite interface
   - register maps, memory addressing
   - visualizing AXI timing
- stream_inband
   - vehicle:  polynomial acceleartor
   - Creating simple DataSchemas for command and vectors of data
   - AXI stream interface
   - extracting AXI streaming bursts
- shared_mem
   - vehicle:  histogram with shared memory for data and AXI streaming for control
   - Creating a shared memory and AXI-MM interfaces on accelerator and host
   - Using the AXI-MM interface for transfering data
   - Visualizing and capturing AXI-MM timing
- rowwise_fir
   - vehicle:  row-wise FIR filter with shared memory for the data
   - **Reuses** shared_mem's interfaces — AXI-stream for control (the command rides `s_in`,
     the response `m_out`), AXI-MM for data — so it introduces **no new interface**.
   - New concepts: modeling and writing **load-compute-store** kernels as a **free-running streaming
     pipeline** — `load`/`compute`/`store` as concurrent stages with the **simplest inter-stage
     hand-off: a plain `hls::stream` carrying control + data in-band** (like poly). The FIR is a
     sliding window, so `compute` streams it naturally (a T-tap shift register) — **no buffer between
     stages is needed.** Plus **fitting timing models** from a cosim sweep.
- vmac:
   - vehicle:  A complex vector map accelerator
   - This should be migrated to a row-wise dataflow architecture similar to rowwise_fir to get decent performance
   - New concept 1: complex fixed-point arithmetic.
   - New concept 2: the **AXI-MM command queue** for control (control *also* in memory).
   - New concept 3: the **double-buffer inter-stage hand-off** — vmac's vector ops need the operand(s)
     **resident** for random access, so streaming element-by-element doesn't suffice. The load→compute
     hand-off becomes a **ping-pong BRAM (`hls::stream_of_blocks`)** with a **separate control stream**
     for the metadata. This is the step up in dataflow hand-off design after rowwise_fir's plain stream.

**Decision (2026-06-23): Option 1 for rowwise_fir — AXI-stream control, no command queue.**
rowwise_fir's genuine new concepts are the load-compute-store dataflow and timing-model fitting,
both novel and substantial; they are *orthogonal* to how the command arrives. The AXI-MM queue is a
third, unrelated control-plane concept that makes too big a jump from shared_mem and dilutes the
focus. So rowwise_fir reuses shared_mem's AXI-stream control, and the **queue is introduced in vmac**
— which already owns the "control in a memory queue" framing and is better positioned to absorb an
orthogonal interface concept alongside its (well-grounded) complex-fixed-point concept.

**Order:** … shared_mem → **rowwise_fir** (dataflow + timing, reusing shared_mem's interface) →
**vmac** (adds the AXI-MM queue + complex fixed-point). rowwise_fir comes *before* vmac.

> Note: drop the word "capstone" from the example docs — more complex examples will follow, so it
> wrongly implies a final example.

**Refinement (2026-06-24): the inter-stage hand-off is the real teaching axis.** rowwise_fir and vmac
both teach load-compute-store dataflow, but the lesson that actually matters in real dataflow kernels is
*how stages hand off data* — and FIR vs VMAC is the natural easy→hard split:

- **rowwise_fir = plain `hls::stream`, control + data in-band.** The FIR is a sliding window, so
  `compute` consumes the stream sample-by-sample into a T-tap shift register — it never needs random
  access to a buffer, so the simplest hand-off (one FIFO, in-band control like poly) is sufficient.
- **vmac = double-buffer (`stream_of_blocks` ping-pong BRAM) + separate control stream.** Vector ops
  need the operand **resident** for random access, so a scalar FIFO won't do; you hand off a *block*
  (a ping-pong BRAM bank, acquired/released via the lock) with the metadata on a side control stream.

So the progression is **streaming hand-off (rowwise_fir) → buffered/double-buffer hand-off (vmac)** —
which is *why* FIR is the simpler dataflow example and VMAC the harder one, beyond just complex
arithmetic + the queue. (Why no shared buffer between stages: independent dataflow processes can't share
a bare dual-port BRAM safely — a RAW hazard with no shared schedule — so the hand-off needs either
back-pressure [`hls::stream`] or a lock [`stream_of_blocks`]; the FIR can use the former, VMAC needs the
latter.)
