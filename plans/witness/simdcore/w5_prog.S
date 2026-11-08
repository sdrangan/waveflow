# W5's test program: vector setup, a reduction read back by the scalar unit, a scalar loop on it.
    addi a0, x0, 8
    vsetvli t0, a0, e32, m1, ta, ma
    addi a1, x0, 3
    vmv.v.x v1, a1
    vadd.vv v2, v1, v1
    vmv.v.x v3, x0
    vredsum.vs v4, v2, v3
    vmv.x.s a2, v4
    addi a4, x0, 0
loop:
    addi a4, a4, 1
    bne a4, a2, loop
    add a4, a4, t0
    ecall
