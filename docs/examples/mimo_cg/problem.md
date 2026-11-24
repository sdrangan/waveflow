---
title: The problem
parent: MU-MIMO whitening with CG
nav_order: 1
summary: "The problem statement. Every user's channel is held in memory as one vector and a power, estimated beforehand. A scheduler pairs users for multi-user MIMO and needs, for each user set, a whitening (MMSE) matrix G = P Hᴴ with P = (I + HᴴH)⁻¹ (K × K). Each scheduling decision sends the accelerator a computation request, and the uplink grant waits for the result. A new channel invalidates every stored matrix that uses it, so requests come in bursts. What is measured is the delay from a request to its result in memory (the accelerator's response time, independent of the scheduling policy) and the accuracy the iteration count buys."
---

# The problem

**Status: under construction.** This page states the problem. Sizes and several choices are still
open (listed at the end).

## The users' channels, in memory

A base station has $N_{rx}$ receive antennas and $N_{ue}$ connected users, each sending one stream.
Every user regularly transmits sounding reference signals (SRS). From $N_{SRS} = 8$ SRS snapshots
the base station finds the low-rank subspace that user's channel lies in, of rank $r$, and keeps an
$N_{rx} \times r$ basis and the relative power on each basis direction. Over a rural macrocell the
channels are dominated by a single path, so **$r = 1$**. Each user $j$ then has a unit vector
$u_j \in \mathbb{C}^{N_{rx}}$ and a power $p_j$ (its SNR: the noise power is normalized to 1),
and its channel is

$$
h_j = \sqrt{p_j}\, u_j .
$$

**Here the channels are given.** The subspace estimation is done upstream and is not part of this
problem. A later architecture could run it on the same systolic engine.

In external memory, the channels are an $N_{rx} \times N_{ue}$ array with one column per user,
plus the $N_{ue}$ powers. A user's column is contiguous, so it is read in one burst.

## The whitening matrix

The scheduler puts a set $S$ of $K$ users on the same uplink resource. Stack their channels as
$H = [h_j]_{j \in S}$, an $N_{rx} \times K$ matrix. The received vector is

$$
y = H x + n, \qquad \mathbb{E}[x x^H] = I, \quad \mathbb{E}[n n^H] = I ,
$$

where interference from outside the cell is counted in the noise. The linear MMSE estimate of the
$K$ symbols is $\hat{x} = G y$, with the **whitening matrix**

$$
G = (H^H H + I)^{-1} H^H = P\, H^H, \qquad P = (I + H^H H)^{-1} .
$$

The push-through identity $(I + H^H H)^{-1} H^H = H^H (I + H H^H)^{-1}$ gives the same $G$ from an
$N_{rx} \times N_{rx}$ inverse. **With $K \ll N_{rx}$, only the $K \times K$ matrix $P$ needs inverting.**

Why "whitening": row $k$ of $G$ is user $k$'s combiner,

$$
g_k^H = \frac{h_k^H Q_k^{-1}}{1 + h_k^H Q_k^{-1} h_k}, \qquad Q_k = I + \sum_{j \in S,\, j \ne k} h_j h_j^H ,
$$

so it whitens the interference from the other $K - 1$ users plus noise, then matches user $k$'s
channel. User $k$'s SINR comes from the diagonal of $P$, and could be used for link adaptation:

$$
\gamma_k = h_k^H Q_k^{-1} h_k = \frac{1}{P_{kk}} - 1 .
$$

In terms of what memory holds, $A = I + H^H H$ has entries

$$
A_{ij} = \delta_{ij} + \sqrt{p_i p_j}\; u_i^H u_j ,
$$

so it depends only on the users' powers and on their pairwise correlations $u_i^H u_j$. The work
for one set is:

1. **Gather** the $K$ columns of $H$ from memory.
2. **Gram:** $A = I + H^H H$, about $K^2 N_{rx}$ complex multiply-accumulates (half of that, since
   $A$ is Hermitian).
3. **Inverse:** $P = A^{-1}$, by conjugate gradient ([next page](cg.md)).
4. **Combiner:** $G = P H^H$, $K^2 N_{rx}$ multiply-accumulates.
5. **Store** $G$ ($K \times N_{rx}$) in the matrix store.

## Scheduling and the matrix store

The base station keeps a **store of whitening matrices**, one entry per user set. A matrix is
computed only on demand:

- **A scheduling decision.** A user sends a scheduling request, or a HARQ transmission fails and
  needs a retransmission. The scheduler decides which users share the resource (the MU-MIMO
  pairing), giving a set $S$. It then issues a **computation request** to the accelerator: compute
  $G$ for $S$ with $t$ CG iterations. **The uplink grant is held until $G$ is in memory.**
- **A channel update.** A new SRS estimate replaces user $j$'s $u_j$ and $p_j$. **Every stored
  matrix whose set contains $j$ becomes invalid.** Nothing is recomputed when that happens. The
  next scheduling decision that needs one of those sets issues a request for it.
- **A completion.** The accelerator writes $G$ into the store, and the grant waiting on it is
  issued.

A valid matrix already in the store could save a request. For now that is the scheduler's
business: **the accelerator sees only a stream of requests**, and this study is about how it serves
them.

**The load is bursty.** When many users' channels update at once (SRS updates are often aligned),
the matrices that use them all become invalid together, and the scheduling decisions that follow
all need new matrices. The requests then reach the accelerator in clusters.

## What is measured: delay

For a computation request issued at $t_{req}$, whose result is in memory at $t_{done}$, the
**delay** is

$$
D = t_{done} - t_{req} .
$$

This is the accelerator's response time: the time the job waits behind other requests (a burst, for
example), plus its service time (reading the channels, computing, writing $G$), which depends on $K$
and on $t$. It does not depend on the scheduling policy, only on the stream of requests. The
scheduling delay of the grant is $D$ plus the scheduler's own decision time, so a long tail in $D$ is
a long tail in the users' delay. A HARQ retransmission is the most sensitive case.

**Performance is the distribution of $D$**: its mean and its tail (for example the 99th
percentile), for a given request stream.

The iteration count is the main trade:

- **Fewer iterations** give a shorter delay, but a less accurate $G$.
- **The cost of that inaccuracy** is the SINR lost against the exact MMSE matrix, which the untimed
  model computes directly.

The study therefore reports two numbers: the **delay statistics** and the **SINR loss**.

## What can be designed

- **The accelerator:** its architecture and sizes (page to come).
- **The iteration count** per request: fixed, or dependent on $K$ or on how correlated the users
  are.

The delay is queueing plus a service time that depends on the set and on $t$, under bursty
arrivals, and the tail is what matters. Neither the mean nor the tail can be read from the untimed
model: a hardware point is scored only by a timed simulation of the accelerator on a request
stream. That makes this a test of design-space exploration with a calibrated simulator (pysim)
against RTL simulation.

**Later:** the scheduler's side (keeping valid matrices, precomputing likely sets, recomputing
eagerly after an invalidation, priorities, the store's capacity) turns this into a policy study on
top of the same accelerator.

## Open

- **Sizes:** $N_{rx}$ (64 assumed so far), $N_{ue}$, and the largest set $K$. The iteration trade
  only exists while $t < K$, because CG is exact at $t = K$.
- **What the store holds:** $G$ ($K \times N_{rx}$), or only $P$ ($K \times K$) if the real-time
  detector forms $H^H y$ itself. Possibly the SINRs $\gamma_k$ too.
- **The request stream:** how requests arrive (scheduling requests, HARQ failures), the mix of set
  sizes, and how clustered the requests are after aligned channel updates.
- **Later:** rank $r > 1$, and the subspace estimation run on the shared systolic engine.
