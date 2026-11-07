---
title: Programming the SIMD Core
parent: SIMD Core
nav_order: 2
summary: "How to write programs for the SIMD core: a job and its arguments, input and output windows (wf.qpop, wf.qpush), strip-mining with vsetvli, complex data as e32 pairs with the wf. instructions and their shifts, piecewise functions by masks, reductions and argmax, lookup tables, many registers as the rows of a matrix (CG), what each instruction costs, and the RVV habits that do not carry over. Every listing assembles."
---

# Programming the SIMD Core

A program is RISC-V assembly ([the instruction set](isa.md)); it runs from address 0 to `ecall`. This
page is the patterns. Every listing on it assembles (`tests/docs/test_simdcore_listings.py`).

## The job

A job's arguments arrive in `a0 .. a7`; the other registers start at 0, and so does data memory. A job
reads input through windows and writes output through windows; its status (`OK` or an error) goes out
when it reaches `ecall`.

## Windows

A **window** is a run of vector registers that a loader fills from a stream, or a storer drains to one.
The build declares them; the matched filter's:

```python
SimdCoreConfig(L=2, vlen=2048, windows=(
    Window("X", base=0, n=1, direction="in"),        # v0
    Window("R", base=8, n=8, direction="in"),        # v8 .. v15: antenna j in v8+j
    Window("Y", base=16, n=8, direction="out")))     # v16 .. v23
```

A block is a `k x n` matrix on the stream, row-major. **Column `j` lands in register `base + j`, row `i`
in its element `i`.** So a matrix whose rows are the data's natural vectors should arrive transposed
(CG's `S` arrives as `N x K` so that each register is one of its `K` rows).

`wf.qpop q` takes the next block of input window `q` (windows are numbered in the order they are
declared); `wf.qpush q` hands output window `q`'s rows `0 .. vl-1` to the storer. Each window has two
copies: the loader fills the next block while the program works on this one, and the storer drains the
last block while the program fills the next. After `wf.qpush`, a two-copy window's registers are
**unspecified until written** -- write every register of the window before the next push. An output
window built with `copies=1` keeps its values instead (the program waits, if it writes too soon, for
the storer).

## Strip-mining

`vsetvli` sets how many elements the next instructions process: `min(a0, VLMAX)`. A frame longer than
`VLMAX` is processed a strip at a time -- here, a block a strip:

```asm
    csrwi   wfsh, 14              # complex products: Q2.14 x Q3.13 -> Q3.13
    csrwi   vxrm, 2               # round down (floor)
chunk:
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0                     # X: k x 1
    wf.qpop 1                     # R: k x 8
    wf.vcmulc.vv v16, v0, v8      # Y_j = conj(X) * R_j, one an antenna
    wf.vcmulc.vv v17, v0, v9
    wf.vcmulc.vv v18, v0, v10
    wf.vcmulc.vv v19, v0, v11
    wf.vcmulc.vv v20, v0, v12
    wf.vcmulc.vv v21, v0, v13
    wf.vcmulc.vv v22, v0, v14
    wf.vcmulc.vv v23, v0, v15
    wf.qpush 2                    # Y
    sub     a0, a0, t0
    bnez    a0, chunk
    ecall
```

That is `examples/simdcore_mf`, bit for bit the vector unit's matched filter.

## Complex data

A complex 16-bit sample is one `e32` element, `re` low. The `wf.` instructions take it whole:

```asm
    vsetvli     t0, a0, e32, m1, ta, ma
    wf.qpop     0
    wf.qpop     1
    wf.vcmulc.vv v2, v0, v1       # z   = conj(x) * y, Q15 (csh = 15 at reset)
    wf.vcabs2.v  v3, v2           # |z|^2: a 32-bit real, Q30 at rsh = 0
    wf.qpush    2
    wf.qpush    3
    ecall
```

**Scaling is the program's.** A product of formats with `fa` and `fb` fraction bits has `fa + fb`;
`csh` (bits 4:0 of `wfsh`) shifts complex results back to 16 bits, `rsh` (bits 12:8) shifts the 32-bit
real ones. Both round by `vxrm` and saturate. Set them before the instructions that need them:

```asm
    li      t2, (9 << 8) | 10     # rsh = 9, csh = 10
    csrw    wfsh, t2
    ecall
```

A **real scalar times a complex vector** is a `.vx` with the scalar as `a + 0j`; a real vector `a`
(32-bit elements holding 16-bit values) becomes complex by clearing the upper half:

```asm
    vsetvli     t0, a0, e32, m1, ta, ma
    li          s3, 0xFFFF
    vand.vx     v2, v1, s3        # alpha (real) -> alpha + 0j
    wf.vcmacc.vv v4, v5, v2       # X += P * alpha
    ecall
```

Real 16-bit data runs at `e16` with the standard instructions (`vsmul` is the Q15 multiply).

## Piecewise functions

Compute every piece, then select with a mask:

```asm
    vsetvli    t0, a0, e16, m1, ta, ma
    vsmul.vx   v4, v1, a2         # lo = s0 * x
    vsmul.vx   v5, v1, a3         # hi = s1 * x + c
    vsadd.vx   v5, v5, a4
    vmslt.vx   v0, v1, a1         # mask: x < t
    vmerge.vvm v3, v5, v4, v0     # y = mask ? lo : hi
    ecall
```

`vmerge.vvm vd, vs2, vs1, v0` takes `vs1` where the mask is set. There are no divergent branches: a
branch is the whole program's (`bnez` on a scalar).

## Reductions and argmax

A reduction folds a vector into element 0 of its destination, starting from element 0 of `vs1`:

```asm
    vsetvli    t0, a0, e32, m1, ta, ma
    vmv.s.x    v4, a1             # start: the smallest value (a1 = 0x80000000)
    vredmax.vs v4, v1, v4         # v4[0] = max(v4[0], max_i v1[i])
    vmv.x.s    t1, v4
    vmseq.vx   v0, v1, t1         # where the maximum is
    vfirst.m   a0, v0             # its first index
    ecall
```

A scalar read of a vector result (`vmv.x.s`, `vcpop.m`, `vfirst.m`) waits for the vector unit to
finish it; put independent vector work before it, not after.

## Tables

`vluxei` reads per-element addresses, only from the LUT region (the top of data memory). Index by
the top bits, interpolate with the rest -- a 64-segment table:

```asm
    vsetvli    t0, a0, e16, m1, ta, ma
    vsrl.vi    v4, v1, 10         # segment i (6 bits)
    vsll.vi    v4, v4, 1          # its byte offset
    vand.vx    v5, v1, a2         # the fraction (a2 = 1023)
    vsll.vi    v5, v5, 5          # as Q15
    vluxei16.v v6, (a1), v4       # B[i]  (a1 = the table's address)
    addi       t1, a1, 128
    vluxei16.v v7, (t1), v4       # D[i]
    vsmul.vv   v7, v7, v5
    vsadd.vv   v3, v6, v7         # B[i] + D[i] * f
    ecall
```

## Many registers: a matrix a row a register

When the work is per column of a `K x N` matrix -- each column its own problem -- put **row `k` in
register `k`** with `VL = N`. A column's dot product is then an elementwise accumulation over `K`
registers, and its scalars are vectors of `N`:

```asm
    vsetvli      t0, a0, e32, m1, ta, ma
    vmv.v.i      v1, 0
    wf.vcdotra.vv v1, v16, v8     # ps += Re(conj(P_0) S_0), every column at once
    wf.vcdotra.vv v1, v17, v9     # ... one a row
    vsll.vi      v6, v3, 11       # alpha = (rz << 11) / ps (the vdiv build option)
    vdiv.vv      v2, v6, v1
    vmseq.vi     v0, v1, 0        # guard: ps == 0 -> alpha = 0
    vmerge.vim   v2, v2, 0, v0
    ecall
```

That is `examples/simdcore_cg`: CG on 32 right-hand sides at once. The 32 registers bound such
programs: CG keeps `S` in its input window, `P` in its one-copy output window, `R` in registers and
`X` in data memory.

## What it costs

The cycle model (`waveflow.simdcore.model.Timing`):

- a vector instruction occupies the lanes `ceil(vl / (L · e))` cycles, `e` the elements a lane takes a
  cycle: 1 at `e32` and for the complex instructions, 2 at `e16`;
- a dependent instruction starts at least `D` (`depth`) cycles after its producer: free when
  `vl / L >= D`, a stall otherwise;
- the scalar unit issues one instruction every `scalar_ii` cycles (3: it decodes the vector
  instructions too), ahead of the vector unit by up to `vq_depth` instructions; `D` is 9; `div` takes about 34 cycles; a scalar read of a vector result waits for it;
- `vdiv` (option) occupies the lanes like any instruction but its result comes `vdiv_latency` later.

So: keep `vl / L` at least `D`, keep scalar reads of vector results rare, and count instructions --
`S · N/L` is the bill.

## RVV habits that do not carry over

| Habit | Here |
|---|---|
| `LMUL` 2, 4, 8 | `vill`: only `m1` |
| widening `vwmul`, narrowing `vnclip` | absent: accumulate at `e32`, shift with `vssra`, or use the `wf.` shifts |
| `vslide*`, `vrgather`, `vid.v`, `vcompress` | absent: lanes never exchange data except through a reduction |
| relying on tail-agnostic ones | tails are left undisturbed |
| `vle`/`vse` for the streams | the streams are windows (`wf.qpop`, `wf.qpush`); `vle`/`vse` are for data memory |
| floating point | absent: integers and shifts |
