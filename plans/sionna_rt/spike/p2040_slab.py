"""Step 2 evidence (plans/sionna_rt/sionna_rt.md): the reflector scenes against closed forms.

Reads the reflected-path gains rt_spike.py printed (rt_spike.out) and compares the reflection
coefficient |R|^2 -- the path's power gain over free-space loss on the unfolded path -- with:
a perfect conductor (|R| = 1), a single Fresnel interface, and the ITU-R P.2040 slab formula.
Geometry: Tx (-10, 0, 10), Rx (10, 0, 10), plane z = 0, so 45 deg incidence and an unfolded path of
sqrt(20^2 + 20^2) m; vertical polarization lies in the x-z plane of incidence, i.e. TM.
"""
import numpy as np

C, F = 299_792_458.0, 3.5e9
LAM, W, E0 = C / F, 2 * np.pi * F, 8.8541878128e-12
FS_DB = 20 * np.log10(LAM / (4 * np.pi * np.hypot(20.0, 20.0)))  # free space, unfolded path

#: Reflected-path power gains from rt_spike.out (sionna-rt 2.2.0, llvm_ad_mono_polarized).
SIONNA_DB = {"simple_reflector (ITU metal)": -72.362, "floor_wall (ITU concrete)": -84.808}


def slab(eps: complex, d: float, theta: float) -> tuple[complex, complex, complex, complex]:
    """TE/TM single-interface and slab (ITU-R P.2040) reflection coefficients."""
    ci, si = np.cos(theta), np.sin(theta)
    root = np.sqrt(eps - si**2)
    r_te = (ci - root) / (ci + root)
    r_tm = (eps * ci - root) / (eps * ci + root)
    q = 2 * np.pi * d / LAM * root

    def thick(r):
        return r * (1 - np.exp(-2j * q)) / (1 - r**2 * np.exp(-2j * q))

    return r_te, r_tm, thick(r_te), thick(r_tm)


def db(r: complex) -> float:
    return 10 * np.log10(abs(r) ** 2)


print(f"free space on the unfolded path: {FS_DB:.4f} dB")
for name, g in SIONNA_DB.items():
    print(f"Sionna |R|^2, {name}: {g - FS_DB:.4f} dB")
concrete = 5.24 - 1j * 0.123087 / (W * E0)
r_te, r_tm, s_te, s_tm = slab(concrete, 0.1, np.deg2rad(45.0))
print(f"concrete 0.1 m, 45 deg: single TE {db(r_te):.4f}  single TM {db(r_tm):.4f}  "
      f"slab TE {db(s_te):.4f}  slab TM {db(s_tm):.4f} dB")
metal = 1.0 - 1j * 1e7 / (W * E0)
print(f"metal 0.01 m, 45 deg: slab TM {db(slab(metal, 0.01, np.deg2rad(45.0))[3]):.4f} dB")
