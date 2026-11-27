
- A receiver has `C` channels.
- RX data is of the form: `x[k,i,j]` where `k` is the sample index, `i=0,...,nrx-1` is the antenna index and `j=0,...,C-1` is the channel idnex
- Sample rate is `fsamp` 
- Let `Q[k,j]` be the spatial covariance of the data

```python
    Q[k,j] = E[ x[k,:,j]* x[k,:,j]^H ]
```

- At each time `k`, either there is no emitter:

```python
    Q[k,j] = I
```

or there is a single emitter so that:

```python
Q[k,j] = I + gam[k,j]*u(theta[k,j]) * u(theta[k,j])^H
```

where `gam[k,j]` is the received SNR, `theta[k,j]` is the AoA, and `u(theta)` is the spatial signature for that AoA.

- The presence of emitters changes slowly (e.g., Gilbert-Elliot process) and when they appear the AoA is constant.

- Problem is to detect emitters, and when detected, estimate AoA and SNR.

Algorithm:

- Process in blocks of size `B`.
- For blocks where there is currently no emitter detected, we search for the presence of a new emitter:
- Stage a.1 (Initial detection): compute blocks of energy of each block.  If exceeded go to next stage
- Stage a.2 (Gram matrix):  Compute Gram matrix on block and store `Nrx x Nrx` matrix `Qhat` in memory.  Memory is shared among multiple.  Prformed in systolic unit
- Stage a.3 (Initial detection):  Take a discrete a number of directional hypotheses and find maximimum energy `u(theta)^H*Qhat*u(theta)`.  Performed via a systolic array
- Stage a.4 (SNR estiamte):  `gamhat = ,,max(u^*Qhat*u - 1, 0)`

For each channel with an active emitter:
- Step b.1:  Compute energy as in a.1
If energy drop below threshold, go to next stage
- Step b.2:  Compute `gamhat = max(\| A*u(theta) \|^2-1,0)` where `A` is the block of data.  If `gamhat` below a threshold, consider that the emitter has stopped transmitting.

