---
title: MU-MIMO whitening with CG
parent: Examples
nav_order: 13
has_children: true
example_dir: examples/mimo_cg
summary: "Under construction. A multi-user MIMO uplink keeps every user's channel in memory and a store of whitening (MMSE) matrices, one per set of users scheduled together. A set without a matrix waits for an accelerator to compute it, so the accelerator's delay becomes scheduling delay. The matrix is G = P Hᴴ with P = (I + HᴴH)⁻¹, a K × K inverse found by conjugate gradient. The problem is meant for exploring agentic design-space exploration with Waveflow."
---
# MU-MIMO whitening with CG

**Status: under construction.** These pages state the problem and the algorithm. The accelerator
is not yet designed and nothing here is built yet. The code in `examples/mimo_cg/` is the earlier
study, recorded on [The 2024 CG detector](detector.md).

## Long-Term MU-MIMO

A base station with many antennas serves several users on the same uplink resource: multi-user MIMO or MU-MIMO.  A challenge in MU-MIMO is the computation.  In a given time-frequency resource element, let `H` be the `Nrx x K` channel where `Nrx` is the number of RX antennas and `K` is the number of UEs scheduled in that element.  Instantaneous MIMO requires computing the MMSE inverse of `H` over each coherence period, which can be computationally demanding for large numbers of antennas.

A common solution is **long-term beamforming** or **LTBF**.  In LTBF, the system estimates a subspace with some low-rank `r` for the user channel so that the small-scale channels are largely within that `r`-dimensional subspace over a period that only depends on large-scale path parameters.  In the simulations below, we will use `r=1`. The large-scale parameters are typically stable over many milliseconds.  Given a set of `K` users, the system computes a **whitening matrix** `G` of dimension `K x Nrx` that projects each symbol to scalar signals for each of the `K` users.  Each user can then perform a simple scalar channel estimate and demodulation.   

The whitening matrix depends on the set of users scheduled in a given resource block.  The base station keeps every user's channel in memory, and a store of whitening
matrices, one per user set. When the scheduler picks a set that has no matrix, it asks an
accelerator to compute one. **It does not grant the users their uplink transmission until the
matrix is ready**, so the accelerator's delay becomes the users' scheduling delay. A new channel
estimate for a user invalidates every matrix that includes that user, so the work comes in bursts.

The whitening matrix for K users needs only a **K × K inverse**, which the accelerator finds by
**conjugate gradient** (CG). The number of CG iterations trades the matrix's accuracy against the
delay.

The problem is how to select the parameters and architecture for the whitening matrix accelerator to trade off accuracy, resources and delay.

## Why this problem

This example will be used to explore **agentic design-space exploration with Waveflow**: an agent
chooses the accelerator's design, the policy that feeds it, or both. Here the result is a
**delay distribution**, and its tail depends on queueing behind bursts of work. Neither can be
read from an untimed model, so every candidate needs a timed simulation, which is the case for a
calibrated, fast simulator (pysim) over RTL simulation alone.

## Pages

| Page                               | What it covers                                                                                                                         |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| [The problem](problem.md)           | The users' channels in memory, the whitening matrix, the scheduler and the matrix store, invalidation, and the delay that is measured. |
| [Conjugate gradient](cg.md)         | The CG iteration that computes the whitening matrix, and why CG.                                                                       |
| The accelerator                    | *Not written yet*: the architecture is still to be decided.                                                                          |
| [The 2024 CG detector](detector.md) | The earlier design and its study (Vitis 2024.1), kept as a record.                                                                     |
