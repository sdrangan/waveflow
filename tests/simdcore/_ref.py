"""A plain-Python reference for the SIMD core's instructions, written from the RVV 1.0 spec text and
``plans/simdcore_records.md`` D1 -- one element at a time, Python integers, no numpy and nothing from
:mod:`waveflow.simdcore.model` (``plans/simdcore.md`` Stage 2: the interpreter is checked against a
reference that was not derived from it)."""

from __future__ import annotations


def sext(v: int, n: int) -> int:
    v &= (1 << n) - 1
    return v - (1 << n) if v >> (n - 1) else v


def rbit(v: int, d: int, vxrm: int) -> int:
    """RVV 1.0 section 3.8 (vxrm): the increment r for a right shift of v by d."""
    if d == 0:
        return 0
    bit = lambda i: (v >> i) & 1  # noqa: E731
    low = lambda n: v & ((1 << n) - 1)  # noqa: E731  bits n-1..0
    if vxrm == 0:
        return bit(d - 1)
    if vxrm == 1:
        return bit(d - 1) & int(low(d - 1) != 0 or bit(d) == 1)
    if vxrm == 2:
        return 0
    return int(bit(d) == 0) & int(low(d) != 0)


def roundoff(v: int, d: int, vxrm: int) -> int:
    return (v >> d) + rbit(v, d, vxrm)


def clip(v: int, lo: int, hi: int) -> tuple[int, int]:
    return (lo, 1) if v < lo else ((hi, 1) if v > hi else (v, 0))


def elem(op: str, a: int, b: int, vd: int, sew: int, vxrm: int) -> tuple[int, int]:
    """One element of an elementwise op.  ``a`` = vs2, ``b`` = vs1 / rs1 / imm (raw SEW bits),
    ``vd`` the destination's old value.  Returns (raw SEW-bit result, saturated)."""
    m = (1 << sew) - 1
    ua, ub, _uvd = a & m, b & m, vd & m
    sa, sb, svd = sext(a, sew), sext(b, sew), sext(vd, sew)
    smin, smax = -(1 << (sew - 1)), (1 << (sew - 1)) - 1
    sh = ub & (sew - 1)
    r, s = 0, 0
    if op == "vadd":
        r = ua + ub
    elif op == "vsub":
        r = ua - ub
    elif op == "vrsub":
        r = ub - ua
    elif op == "vminu":
        r = min(ua, ub)
    elif op == "vmin":
        r = min(sa, sb)
    elif op == "vmaxu":
        r = max(ua, ub)
    elif op == "vmax":
        r = max(sa, sb)
    elif op == "vand":
        r = ua & ub
    elif op == "vor":
        r = ua | ub
    elif op == "vxor":
        r = ua ^ ub
    elif op == "vsll":
        r = ua << sh
    elif op == "vsrl":
        r = ua >> sh
    elif op == "vsra":
        r = sa >> sh
    elif op == "vsaddu":
        r, s = clip(ua + ub, 0, m)
    elif op == "vsadd":
        r, s = clip(sa + sb, smin, smax)
    elif op == "vssubu":
        r, s = clip(ua - ub, 0, m)
    elif op == "vssub":
        r, s = clip(sa - sb, smin, smax)
    elif op == "vsmul":
        r, s = clip(roundoff(sa * sb, sew - 1, vxrm), smin, smax)
    elif op == "vssrl":
        r = roundoff(ua, sh, vxrm)
    elif op == "vssra":
        r = roundoff(sa, sh, vxrm)
    elif op == "vmul":
        r = sa * sb
    elif op == "vmulh":
        r = (sa * sb) >> sew
    elif op == "vmulhu":
        r = (ua * ub) >> sew
    elif op == "vmacc":  # vd = +(vs1 * vs2) + vd
        r = sb * sa + svd
    elif op == "vnmsac":  # vd = -(vs1 * vs2) + vd
        r = -(sb * sa) + svd
    elif op == "vmadd":  # vd = (vs1 * vd) + vs2
        r = sb * svd + sa
    elif op == "vnmsub":  # vd = -(vs1 * vd) + vs2
        r = -(sb * svd) + sa
    # RVV 1.0 section 11.11: quotient rounds toward zero; x/0 = all ones (-1), x%0 = x;
    # the overflow (most negative)/-1 gives the dividend and remainder 0
    elif op == "vdivu":
        r = m if ub == 0 else ua // ub
    elif op == "vremu":
        r = ua if ub == 0 else ua % ub
    elif op == "vdiv":
        if sb == 0:
            r = -1
        elif sa == smin and sb == -1:
            r = sa
        else:
            q = abs(sa) // abs(sb)
            r = q if (sa < 0) == (sb < 0) else -q
    elif op == "vrem":
        if sb == 0:
            r = sa
        elif sa == smin and sb == -1:
            r = 0
        else:
            q = abs(sa) // abs(sb)
            q = q if (sa < 0) == (sb < 0) else -q
            r = sa - q * sb
    else:
        raise KeyError(op)
    return r & m, s


def compare(op: str, a: int, b: int, sew: int) -> bool:
    m = (1 << sew) - 1
    ua, ub, sa, sb = a & m, b & m, sext(a, sew), sext(b, sew)
    return {"vmseq": ua == ub, "vmsne": ua != ub, "vmsltu": ua < ub, "vmslt": sa < sb, "vmsleu": ua <= ub,
            "vmsle": sa <= sb, "vmsgtu": ua > ub, "vmsgt": sa > sb}[op]


def cparts(w: int) -> tuple[int, int]:
    return sext(w, 16), sext(w >> 16, 16)


def cword(re: int, im: int) -> int:
    return ((im & 0xFFFF) << 16) | (re & 0xFFFF)


def cplx(op: str, a: int, b: int, vd: int, csh: int, rsh: int, vxrm: int) -> tuple[int, int]:
    """One element of a ``wf.`` complex op (records D1).  ``a`` = vs2, ``b`` = vs1 / rs1, ``vd`` old."""
    ar, ai = cparts(a)
    br, bi = cparts(b)
    xr, xi = cparts(vd)
    sat = 0

    def s16(v):
        nonlocal sat
        r, s = clip(v, -32768, 32767)
        sat |= s
        return r

    def rs(p):
        return s16(roundoff(p, csh, vxrm))

    if op == "vcmul":
        return cword(rs(ar * br - ai * bi), rs(ar * bi + ai * br)), sat
    if op == "vcmulc":  # conj(vs2) * vs1
        return cword(rs(ar * br + ai * bi), rs(ar * bi - ai * br)), sat
    if op == "vcmacc":
        return cword(s16(xr + rs(ar * br - ai * bi)), s16(xi + rs(ar * bi + ai * br))), sat
    if op == "vcmadd":  # vd = rs(vd * rs1) + vs2
        return cword(s16(rs(xr * br - xi * bi) + ar), s16(rs(xr * bi + xi * br) + ai)), sat
    if op == "vcdotr":
        r, s = clip(roundoff(ar * br + ai * bi, rsh, vxrm), -(1 << 31), (1 << 31) - 1)
        return r & 0xFFFFFFFF, s
    if op == "vcdotra":  # vd = sat32(vd + rs32(Re(conj(vs2) vs1)))
        t, s1 = clip(roundoff(ar * br + ai * bi, rsh, vxrm), -(1 << 31), (1 << 31) - 1)
        r, s2 = clip(sext(vd, 32) + t, -(1 << 31), (1 << 31) - 1)
        return r & 0xFFFFFFFF, s1 | s2
    if op == "vcconj":
        return cword(ar, s16(-ai)), sat
    raise KeyError(op)
