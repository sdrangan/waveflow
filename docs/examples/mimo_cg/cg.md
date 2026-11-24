---
title: Conjugate gradient
parent: MU-MIMO whitening with CG
nav_order: 2
summary: "How the whitening matrix's inverse is computed. Multi-right-hand-side conjugate gradient on A P = I, with A = I + HᴴH Hermitian positive definite and K × K: each iteration is one matrix product S = A·D and a few vector steps per column with two divides. Why CG: it fits A's structure, its iteration count is a latency/accuracy knob that depends on how correlated the users are, it can warm-start after a channel update, and it splits into two reusable hardware blocks. Honest costs: in multiply count CG is no cheaper than a direct inverse, and with K ≪ N_rx neither dominates a job; the reasons are structure and the knob, not operation count."
---

# Conjugate gradient

**Status: under construction.** The algorithm is fixed; the formats, the stopping rule and the
options at the end are open.

## The system to solve

The [whitening matrix](problem.md#the-whitening-matrix) is $G = P H^H$ with $P = A^{-1}$ and

$$
A = I + H^H H \qquad (K \times K) .
$$

$A$ is Hermitian positive definite, and every eigenvalue is at least 1. CG solves

$$
A X = B
$$

for several right-hand sides at once, one per column of $B$. Two choices of $B$ give $G$:

| Right-hand side | Then | Multiply-accumulates |
|---|---|---|
| $B = I$ ($K$ columns) | $X = P$, then $G = P H^H$ | $t K^3 + K^2 N_{rx}$ |
| $B = H^H$ ($N_{rx}$ columns) | $X = G$ directly | $t K^2 N_{rx}$ |

The first is cheaper whenever $t > 1$ and $K < N_{rx}$ (at $K = 8$, $N_{rx} = 64$, $t = 4$: 6,144
against 16,384), and it also gives $P$'s diagonal, the users' SINRs. **We use $B = I$.**

## The iteration

Each column $j$ of $X$ is its own CG, with its own step sizes. Start from $X_0 = 0$, $R_0 = B$,
$D_0 = R_0$, and $\rho_j = \lVert r_j \rVert^2$. Each of the $t$ iterations does:

$$
\begin{aligned}
S &= A D && \text{the matrix product} \\
\alpha_j &= \rho_j \,/\, d_j^H s_j \\
x_j &\leftarrow x_j + \alpha_j d_j \\
r_j &\leftarrow r_j - \alpha_j s_j \\
\rho_j' &= \lVert r_j \rVert^2 \\
\beta_j &= \rho_j' / \rho_j, \qquad \rho_j \leftarrow \rho_j' \\
d_j &\leftarrow r_j + \beta_j d_j
\end{aligned}
$$

Here $D$ holds the search directions (the CG model calls them `P`; on these pages $P$ is the
inverse). Two guards keep it defined:

- If $d_j^H s_j = 0$, then $\alpha_j = 0$.
- A column whose residual reaches $\rho_j = 0$ freezes.

The iteration splits in two:

- **The matrix product** $S = AD$: $K^2$ complex multiply-accumulates per column, a regular
  product a systolic array does well.
- **The vector steps**, per column: two inner products ($d_j^H s_j$ and $\lVert r_j \rVert^2$), three
  multiply-adds and two divides.

That is the split of the bit-exact fixed-point model already in Waveflow, `waveflow/linalg/cg.py`.
It solves exactly this problem ($A X = B$, $A$ Hermitian positive definite, $K \times K$, many
right-hand sides, each column with its own $\alpha$ and $\beta$, both guards), with `mm_step` for the
product and `vec_step` for the rest. It was the golden model of the earlier hardware
([The 2024 CG detector](detector.md)), which matched it bit for bit at RTL.

## Why CG

**It fits $A$.** CG needs a Hermitian positive definite matrix, and the noise term bounds $A$'s
conditioning:

$$
\kappa(A) = \frac{1 + \lambda_{max}(H^H H)}{1 + \lambda_{min}(H^H H)} \le 1 + \sum_{j \in S} p_j .
$$

The error after $t$ iterations falls at least as fast as
$2\big((\sqrt{\kappa} - 1)/(\sqrt{\kappa} + 1)\big)^t$ (in the $A$-norm). In exact arithmetic CG is
exact after at most $K$ iterations.

**Its iteration count is a knob, and it depends on the users.**
- Users far apart in space have nearly orthogonal $u_j$, so $A$ is nearly diagonal, and each column
  of $B = I$ is nearly an eigenvector of $A$: CG is close to exact after one iteration.
- Users close together (correlated $u_j$) need more iterations.

So the scheduler, which sees the correlations, can choose $t$ per job, trading delay for accuracy.
That trade is the heart of [the problem](problem.md#what-is-measured-delay). A direct method has a
fixed cost and no such knob.

**It can warm-start.** When one user's channel changes, $A$ changes only in that user's row and
column. Starting CG from the old $P$ (when the set is unchanged apart from that channel) may need
fewer iterations than starting from zero. This is an option, not yet tested.

**It makes two reusable blocks.** The product is a systolic matrix multiply, and the vector steps
are a small fixed-function unit. There are no square roots and no triangular solves, whose serial
chains are hard to pipeline at small sizes. The same two blocks serve any Hermitian positive
definite solve. The vector unit also works in fixed point: for detection, the earlier study found that
8–10-bit registers were enough for QPSK and 12–14-bit for 64-QAM, with 8 guard bits on the two inner
products.

## The costs, honestly

- **In multiply count, CG is no cheaper than a direct inverse.** A full $K \times K$ inverse by
  Cholesky costs about $K^3$ multiply-accumulates; CG with $B = I$ costs $t K^3$. For small $K$ (4
  or fewer) a direct solve is a fair competitor, and the design study could include it.
- **With $K \ll N_{rx}$, the inverse is not where the work is.** The Gram and the combiner cost
  $2 K^2 N_{rx}$ together, against $t K^3$ for CG: at $K = 8$, $N_{rx} = 64$, $t = 4$, that is 8,192
  against 2,048.
- **Each CG iteration is a serial chain**: the product, an inner-product reduction, a divide, the
  updates, another reduction and divide. The Gram and the combiner are wide and parallel, and an
  array of multipliers shortens them. So in delay, $t$ is likely to count through the chain's
  latency rather than its multiplies. That is a claim to measure on the accelerator, not to assume.

## Open

- The fixed-point formats, from the untimed model on channels of the target scenario.
- A fixed $t$ per job, or a cap plus a tolerance (stop early once $\rho_j$ is small).
- Diagonal (Jacobi) scaling: from $u_j$ and $p_j$, $A = I + \Pi^{1/2} U^H U\, \Pi^{1/2}$ with
  $\Pi = \mathrm{diag}(p_j)$. Scaling by $\mathrm{diag}(A)$ is nearly free and evens out users of
  very different power.
- Warm start after a channel update.
