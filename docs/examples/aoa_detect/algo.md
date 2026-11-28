---
title: The algorithm
parent: Emitter detection and AoA
nav_order: 2
summary: "The per-channel algorithm, a two-mode state machine on blocks of B samples. Searching: an energy gate on the block; if it fires, the block's spatial covariance (Gram matrix, n_rx × n_rx), a scan of uᴴQ̂u over L candidate directions, the best direction's SNR estimate, and a detection test. Tracking: the block's energy, and if it drops, the power in the tracked direction, to decide whether the emitter has stopped. Each stage's cost in complex multiply-accumulates, and each threshold's false-alarm statistics (chi-square). A search costs several times a tracking check."
---

# The algorithm

**Status: under construction.** The stages are fixed; the thresholds' values, the sizes and the
formats are open.

Each channel runs the same state machine on its blocks, independently of the others. For channel $j$,
block $n$ is the $n_{rx} \times B$ matrix

$$
X = \big[\, x_{nB,\,j},\ x_{nB+1,\,j},\ \dots,\ x_{nB+B-1,\,j} \,\big] ,
$$

one column per sample. The [signal model](problem.md#the-signal) and the notation ($u(\theta)$ with
$\lVert u \rVert = 1$, the array SNR $\gamma$, noise of power 1 per antenna) are on the problem page.

## Two modes

```text
                    gate fires and the scan detects
        ┌──────────┐ ───────────────────────────────► ┌──────────┐
        │ SEARCH   │                                  │ TRACK θ̂  │
        └──────────┘ ◄─────────────────────────────── └──────────┘
          a.1 every block;                 b.1 every block;
          a.2 – a.4 only when a.1 fires    b.2 only when b.1 drops
                        the emitter has stopped (b.2)
```

## Searching (no emitter detected yet)

**a.1 Energy gate**, every block:

$$
e = \frac{1}{B\, n_{rx}} \lVert X \rVert_F^2 \ \ge\ \tau_e \ ?
$$

Under no emitter, $2 B n_{rx}\, e$ is chi-square with $2 B n_{rx}$ degrees of freedom, so $\tau_e$ is set
from a target false-alarm probability per block. With an emitter, $\mathbb{E}[e] = 1 + \gamma / n_{rx}$.
If the gate does not fire, the block is done. If it fires, the search continues.

**a.2 Gram matrix**, the block's spatial covariance:

$$
\hat{Q} = \frac{1}{B}\, X X^H \qquad (n_{rx} \times n_{rx},\ \text{Hermitian}) .
$$

**a.3 Direction scan** over a fixed grid of $L = L_\phi L_\varepsilon$ directions $\theta_0, \dots, \theta_{L-1}$
($L_\phi$ azimuths by $L_\varepsilon$ elevations covering the sector), with
$U = [u(\theta_0), \dots, u(\theta_{L-1})]$ a constant $n_{rx} \times L$ matrix:

$$
p_\ell = u(\theta_\ell)^H\, \hat{Q}\, u(\theta_\ell), \qquad \ell^* = \arg\max_\ell p_\ell, \qquad \hat{\theta} = \theta_{\ell^*} .
$$

Computed as $W = \hat{Q} U$, then $p_\ell = \mathrm{Re}\left(u(\theta_\ell)^H w_\ell\right)$.

**a.4 SNR estimate and detection test:**

$$
\hat{\gamma} = \max\left(p_{\ell^*} - 1,\ 0\right), \qquad \text{detect if } p_{\ell^*} \ge \tau_p .
$$

Under no emitter, each $2B\, p_\ell$ is chi-square with $2B$ degrees of freedom (the $p_\ell$ are
correlated across $\ell$), so $\tau_p$ is set from a target false-alarm probability by a union bound
over the $L$ directions, or by simulation. On a detection the channel switches to **tracking** with
$\hat{\theta}$ and $\hat{\gamma}$, and the host is told. Otherwise it stays **searching**: the gate's
alarm was false, or the emitter is too weak to detect from this block alone.

## Tracking (an emitter at $\hat{\theta}$)

**b.1 Energy**, every block, as in a.1. While $e \ge \tau_e'$ the emitter is taken to be still there
and the block is done.

**b.2 Power in the tracked direction**, only when b.1 drops below $\tau_e'$:

$$
p_b = \frac{1}{B} \left\lVert u(\hat{\theta})^H X \right\rVert^2, \qquad \hat{\gamma} = \max\left(p_b - 1,\ 0\right) .
$$

If $\hat{\gamma} < \tau_\gamma$, the emitter has **stopped**: the host is told and the channel returns to
**searching**. With the emitter still present at $\hat{\theta}$, $2B\, p_b / (1 + \gamma)$ is chi-square
with $2B$ degrees of freedom, which sets the probability of declaring a stop too early.

## Cost per block

In complex multiply-accumulates, for one channel and one block:

| Stage | When | Cost | At $n_{rx} = 16$, $B = 256$, $L = 256$ |
|---|---|---|---|
| a.1 energy | every searching block | $B\, n_{rx}$ (magnitudes only) | 4,096 |
| a.2 Gram | gate fired | $B\, n_{rx}(n_{rx}+1)/2$ | 34,816 |
| a.3 scan | gate fired | $L\, n_{rx}^2 + L\, n_{rx}$ | 69,632 ($L = 32 \times 8$) |
| a.4 test | gate fired | a compare and a subtraction | -- |
| b.1 energy | every tracking block | $B\, n_{rx}$ (magnitudes only) | 4,096 |
| b.2 direction power | b.1 dropped | $B\, n_{rx}$ | 4,096 |

So **a search (a.2 and a.3) costs about 25 times a tracking check** at these sizes, and it reads the
whole block. Which blocks need a search depends on the data: emitters' onsets, weak emitters that fire
the gate repeatedly without being detected, and the gate's false alarms. a.2 and a.3 are regular
matrix products, suited to a systolic unit; a.1 and b.1 are streaming sums that can run beside the
capture.

## In fixed point

The stages above are written in exact arithmetic. In the hardware, sums are accumulated exactly, but
the values kept between stages ($\hat{Q}$, the scan's powers $p_\ell$, $u^H X$ and $p_b$) are stored
narrower, with saturation: a strong emitter can saturate $\hat{Q}$, which flattens the scan's peak. The
bit-exact golden reproduces this exactly, so its accuracy metrics are those of the hardware. The widths
and modes are on [The parameters](param.md#number-formats).

## The thresholds

| Threshold | Sets | Raising it |
|---|---|---|
| $\tau_e$ (search gate) | how often a search runs | fewer searches, slower detection of weak emitters |
| $\tau_p$ (detection) | false detections | fewer false alarms, lower detection probability per block |
| $\tau_e'$ (tracking energy) | how often b.2 runs | more b.2 checks |
| $\tau_\gamma$ (stop) | early or late stops | stops declared earlier, more spurious stops |

All four are per-block statistics that the untimed model scores. What they do to the **delay** depends
on the load they create on the shared hardware, which is the point of the study.

## Open

- Whether b.2 runs only when b.1 drops (as above) or on every tracking block.
- A finer angle than the grid (for example a parabolic fit around $\ell^*$).
- Using more than one block for a weak emitter (averaging $\hat{Q}$ over consecutive gated blocks).
