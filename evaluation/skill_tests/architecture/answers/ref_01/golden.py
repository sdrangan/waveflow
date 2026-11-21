"""golden.py -- the MMSE detector's executable definition (plans/spec.md), untimed, numpy only.

    x_hat = W y,   W = (H^H H + sigma^2 I)^{-1} H^H      per resource element, W per filter group

``mmse_filter`` is the operation as the paper states it, in floating point; ``detect`` applies a filter
to a block of resource elements.  ``cg_filter`` is alternative A's solve: conjugate gradient on
``G W = H^H`` (multi right-hand side, ``nit`` iterations from zero), in floating point, so its
accuracy against the direct solve is measured before anything is built.  ``quantize`` and
``fixed_detect`` are the proposed formats (plans/spec.md, *Formats*), checked against the
floating-point golden by ``evm_db``.

Edge cases (plans/spec.md): ``U = 0`` (no user scheduled) gives an empty output and no work;
``sigma2 <= 0`` is refused (with no noise term ``H^H H`` can be singular: zero-forcing is not this
operation); ``U > B`` is refused (more layers than antennas); a non-finite input is refused.
"""
from __future__ import annotations

import numpy as np

B_MAX, U_MAX = 32, 8


def _check(H: np.ndarray, sigma2: float) -> None:
    if H.ndim != 2:
        raise ValueError("H must be B x U")
    b, u = H.shape
    if not 1 <= b <= B_MAX or u > min(b, U_MAX):
        raise ValueError(f"H is {b} x {u}: need 1 <= B <= {B_MAX}, U <= min(B, {U_MAX})")
    if not np.isfinite(sigma2) or sigma2 <= 0:
        raise ValueError("sigma2 must be finite and > 0 (MMSE, not zero-forcing)")
    if not np.all(np.isfinite(H)):
        raise ValueError("H is not finite")


def gram(H: np.ndarray, sigma2: float) -> np.ndarray:
    """``G = H^H H + sigma^2 I`` (U x U, Hermitian, positive definite for sigma^2 > 0)."""
    _check(H, sigma2)
    return H.conj().T @ H + sigma2 * np.eye(H.shape[1])


def mmse_filter(H: np.ndarray, sigma2: float) -> np.ndarray:
    """``W = G^{-1} H^H`` (U x B), by a direct solve -- the paper's definition."""
    if H.shape[1] == 0:
        return np.zeros((0, H.shape[0]), complex)
    return np.linalg.solve(gram(H, sigma2), H.conj().T)


def cg_filter(H: np.ndarray, sigma2: float, nit: int) -> np.ndarray:
    """``W`` by ``nit`` iterations of conjugate gradient on ``G W = H^H``, every column at once, from
    ``W = 0`` -- the solve alternative A runs on the vector unit (a matrix product per iteration)."""
    if H.shape[1] == 0:
        return np.zeros((0, H.shape[0]), complex)
    G, Bm = gram(H, sigma2), H.conj().T
    X = np.zeros_like(Bm)
    R = Bm.copy()
    P = R.copy()
    rz = np.sum(np.abs(R) ** 2, axis=0)
    for _ in range(nit):
        S = G @ P
        ps = np.real(np.sum(P.conj() * S, axis=0))
        alpha = np.divide(rz, ps, out=np.zeros_like(rz), where=ps != 0)
        X = X + P * alpha
        R = R - S * alpha
        rz2 = np.sum(np.abs(R) ** 2, axis=0)
        beta = np.divide(rz2, rz, out=np.zeros_like(rz), where=rz != 0)
        P = R + P * beta
        rz = rz2
    return X


def detect(W: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """``X_hat = W Y``: ``Y`` is B x n (n resource elements of one filter group), ``X_hat`` U x n."""
    if not np.all(np.isfinite(Y)):
        raise ValueError("Y is not finite")
    return W @ Y


def quantize(x: np.ndarray, bits: int, int_bits: int) -> np.ndarray:
    """Round to nearest and saturate, per real part, to a signed format of ``bits`` bits of which
    ``int_bits`` are integer (sign included): ``ap_fixed<bits, int_bits, AP_RND, AP_SAT>``."""
    lsb = 2.0 ** (int_bits - bits)
    lo, hi = -(2.0 ** (int_bits - 1)), 2.0 ** (int_bits - 1) - lsb

    def q(v):
        return np.clip(np.floor(v / lsb + 0.5) * lsb, lo, hi)

    return q(np.real(x)) + 1j * q(np.imag(x))


#: The proposed formats (plans/spec.md): (bits, integer bits) per real part.
FMT_Y = (16, 1)     # the FFT's output, scaled to |re|, |im| < 1
FMT_H = (16, 1)     # the estimator's output, the same scale
FMT_W = (16, 2)     # W = G^-1 H^H: |w| reached 1.19 at 8 users (scenario, 20 seeds): headroom to 2
FMT_X = (16, 3)     # QAM symbols at unit average energy, 256-QAM's corner at 1.53: headroom to 4


def fixed_detect(H, sigma2, Y, nit: int | None = None):
    """The datapath at the proposed formats: H and Y quantized as they arrive, W quantized once, the
    product exact and rounded once to ``FMT_X`` (as the systolic engine rounds)."""
    Hq, Yq = quantize(H, *FMT_H), quantize(Y, *FMT_Y)
    W = mmse_filter(Hq, sigma2) if nit is None else cg_filter(Hq, sigma2, nit)
    return quantize(detect(quantize(W, *FMT_W), Yq), *FMT_X)


def evm_db(x: np.ndarray, ref: np.ndarray) -> float:
    """Error against a reference, in dB relative to the reference's power."""
    return float(10 * np.log10(np.sum(np.abs(x - ref) ** 2) / np.sum(np.abs(ref) ** 2)))


def scenario(b=32, u=8, n=168, snr_db=20.0, seed=0):
    """An i.i.d. Rayleigh channel scaled into the input format, QPSK symbols, noise at ``snr_db`` per
    antenna: ``(H, sigma2, Y, X)``.  An assumption (plans/spec.md, *Workload*): real channels are
    correlated and need a scenario of their own."""
    rng = np.random.default_rng(seed)
    scale = 0.25 / np.sqrt(u)            # |y| stays inside the input format with headroom
    H = scale * (rng.standard_normal((b, u)) + 1j * rng.standard_normal((b, u))) / np.sqrt(2)
    X = (rng.choice([-1, 1], (u, n)) + 1j * rng.choice([-1, 1], (u, n))) / np.sqrt(2)
    p_rx = scale ** 2 * u
    sigma2 = p_rx / 10 ** (snr_db / 10)
    N = np.sqrt(sigma2 / 2) * (rng.standard_normal((b, n)) + 1j * rng.standard_normal((b, n)))
    return H, sigma2, H @ X + N, X


if __name__ == "__main__":
    H, s2, Y, X = scenario()
    ref = detect(mmse_filter(H, s2), Y)
    for nit in (2, 3, 4, 8):
        print(f"CG, {nit} iterations: {evm_db(detect(cg_filter(H, s2, nit), Y), ref):6.1f} dB from the direct solve")
    print(f"fixed point, direct W:   {evm_db(fixed_detect(H, s2, Y), ref):6.1f} dB from the float golden")
    print(f"fixed point, CG 3:       {evm_db(fixed_detect(H, s2, Y, nit=3), ref):6.1f} dB")
    print(f"max |W| = {np.abs(mmse_filter(H, s2)).max():.3f} (FMT_W holds < 2)")
