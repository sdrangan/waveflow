"""Reference programs for the agent test's kernels: they show that each golden is reachable with the
documented instructions (``test_agent_kernels.py``). Not shown to the agent under test."""

SOLUTIONS = {
    "beamform-2": """
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    wf.qpop 1
    vmv.v.x v4, a1                # w0 in every element
    vmv.v.x v5, a2
    wf.vcmulc.vv v6, v4, v1       # conj(w0) x0, rounded (csh 15, vxrm 0 at reset)
    wf.vcmulc.vv v7, v5, v2
    slli t1, a0, 1
    vsetvli t2, t1, e16, m1, ta, ma
    vsadd.vv v3, v6, v7           # per part, saturating
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpush 2
    ecall
""",
    "power-count": """
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    wf.vcabs2.v v2, v1            # re^2 + im^2, exact at rsh 0
    vmsgtu.vx v0, v2, a1
    vcpop.m a0, v0
    ecall
""",
    "rotate": """
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    wf.qpop 1
    wf.vcmul.vv v4, v1, v2
    slli t1, a0, 1
    vsetvli t2, t1, e16, m1, ta, ma
    vssra.vi v3, v4, 1            # rnu(y, 1) per part (vxrm 0)
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpush 2
    ecall
""",
    "normalize-peak": """
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    vrsub.vi v2, v1, 0
    vmax.vv v2, v2, v1            # |x|
    vmv.s.x v4, zero
    vredmax.vs v4, v2, v4
    vmv.x.s t1, v4                # m
    bnez t1, nonzero
    li t1, 1
nonzero:
    vsll.vi v5, v1, 10
    vdiv.vx v3, v5, t1
    wf.qpush 1
    ecall
""",
    "horner-4": """
    vsetvli t0, a0, e16, m1, ta, ma
    wf.qpop 0
    vmv.v.x v3, a5                # c4
    vsmul.vv v3, v3, v1
    vsadd.vx v3, v3, a4
    vsmul.vv v3, v3, v1
    vsadd.vx v3, v3, a3
    vsmul.vv v3, v3, v1
    vsadd.vx v3, v3, a2
    vsmul.vv v3, v3, v1
    vsadd.vx v3, v3, a1
    wf.qpush 1
    ecall
""",
    "argmin": """
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    li t1, 0x7FFFFFFF
    vmv.s.x v4, t1
    vredmin.vs v4, v1, v4
    vmv.x.s t1, v4
    vmseq.vx v0, v1, t1
    vfirst.m a0, v0
    ecall
""",
    "soft-limit": """
    vsetvli t0, a0, e16, m1, ta, ma
    wf.qpop 0
    vrsub.vi v2, v1, 0
    vmax.vv v2, v2, v1            # |x|
    vsub.vx v4, v2, a1
    vsra.vi v4, v4, 2
    vadd.vx v4, v4, a1            # A + ((|x| - A) >> 2)
    vrsub.vi v5, v4, 0
    vmsle.vi v0, v1, -1           # x < 0 (RVV has no vmslt.vi)
    vmerge.vvm v4, v4, v5, v0     # with x's sign
    vmsle.vx v0, v2, a1
    vmerge.vvm v3, v4, v1, v0     # |x| <= A: x
    wf.qpush 1
    ecall
""",
    "complex-energy-sum": """
    li t1, (8 << 8) | 15          # rsh 8
    csrw wfsh, t1
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    wf.vcabs2.v v2, v1            # rnu(re^2 + im^2, 8)
    vmv.s.x v4, zero
    vredsum.vs v4, v2, v4
    vmv.x.s a0, v4
    ecall
""",
}
