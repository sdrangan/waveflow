---
title: Conjugate Gradient Matrix Inverse
parent: Example Projects
nav_order: 2
has_children: false
---

# Conjugate Gradient Matrix Inverse

> **Superseded (2026-09-30).** The implementation work moves to `examples/mimo_cg/`, under
> [`plans/mimo_cg/mimo_cg_paper_sims.md`](mimo_cg/mimo_cg_paper_sims.md). The code below has been corrected.
> As first written it did not converge (relative error near 1.5 at every iteration count),
> for three reasons, each enough alone: it had `X = X - P*alpha` for `X + P*alpha` and
> `P = R - P*beta` for `R + P*beta`, and its `rnorm = rnorm` never stored the new norms.
> Two smaller fixes: `X` now starts as an n×n matrix rather than a vector (harmless before,
> since broadcasting widened it), and the function now returns `X`.

##  IP definition

The IP will perform conjugate gradient (CG) descent for matrix inversion.  Matrix inversion is a fundamental  operation in many scientific computing applications and can be computationally intensive, making it a good candidate for hardware acceleration.
The CG algorithm provides an iterative method for computing the matrix inverses that can be implemented well
in hardware since it replaces Gaussian elimination with matrix multiplications that are more easily parallelized. 
The python equivalent algorithm is as follows:

```python
def cginv(Q, nit):
    """
    Computes the matrix inverse for a nxn Hermitian positive-definite matrix `Q`.

    Parameters
    ----------
    Q : nxn matrix
    nit : number of iterations

    Returns
    -------
    X : approximation of inv(Q)
    """
    n = Q.shape[0]
    X = np.zeros((n, n), dtype=complex)
    R = np.eye(n, dtype=complex)
    P = R.copy()
    rnorm = np.ones(n)
    for i in range(nit):
        # Update S with matrix multiplication
        S = Q.dot(P)

        # Update X
        ps = np.real(np.sum(np.conj(P)*S, axis=0))  # columnwise inner products (real: Q is Hermitian PD)
        alpha = rnorm / ps
        X = X + P*alpha[None,:]

        # Compute matrix-matrix product QX
        QX = Q.dot(X)

        # Update residual (explicit form; the recurrence R = R - S*alpha[None,:]
        # saves this second matrix multiplication)
        R = np.eye(n) - QX
        
        # Update P
        rnorm_new = np.sum(np.abs(R)**2, axis=0)  # column norms of R
        beta = rnorm_new / rnorm
        rnorm = rnorm_new
        P = R + P*beta[None,:]
    return X
```

The processing system will send `Q` and `nit` to the IP via shared memory.  The IP will compute `X` will indicate to the PS that it is completed and send the result back via shared memory.

## IP architecture:

We divide the IP into two sub-modules:

- A matrix multiplication unit that performs the multiplications `Q,dot(P)` and `Q.dot(X)`.
- A vector unit that performs all the other columnwise operations

At the beginning the PS will write the data to shared memory and send a command to the matrix multiply unit with location of the data. 
Since the matrix multiply unit operates on data in shared memory, it can access the data directly without further intervention from the PS.  When completed its
half-iteration, it will send a command to the vector update unit with the location of the data.  The vector update unit will perform the vector updates and write the results back to shared memory.
and send a command back to the matrix-multiply unit to indicate that it can proceed with the next half-iteration.  It will continue until complete and send a message back to the PS
when done. 