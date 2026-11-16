"""Step 2 spike (plans/sionna_rt/sionna_rt.md): what Sionna RT 2.2.0 does on this CPU-only machine.

Scratch only, not committed.  Prints evidence for each question in step 2.
"""
import json
import time

import numpy as np

import mitsuba as mi  # noqa: E402  (variant chosen by sionna.rt on import)
import sionna.rt as rt
from sionna.rt import PathSolver, PlanarArray, Receiver, Transmitter, load_scene

C = 299_792_458.0
F = 3.5e9
LAM = C / F


def banner(s):
    print("\n" + "=" * 8, s, "=" * 8)


banner("versions and variant")
import drjit as dr

print("sionna.rt", rt.__version__, "mitsuba", mi.__version__, "drjit", dr.__version__,
      "variant", mi.variant())


def iso_scene(scene, rx_rows=1, rx_cols=1, pol="V"):
    scene.frequency = F
    scene.tx_array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization=pol)
    scene.rx_array = PlanarArray(num_rows=rx_rows, num_cols=rx_cols, pattern="iso",
                                 polarization=pol)
    return scene


def trace(scene, **kw):
    solver = PathSolver(deterministic=kw.pop("deterministic", True))
    t = time.perf_counter()
    paths = solver(scene, **kw)
    return paths, time.perf_counter() - t


def _c(a):
    return np.asarray(a[0]) + 1j * np.asarray(a[1]) if isinstance(a, tuple) else np.asarray(a)


def cir(paths, normalize_delays=False, **kw):
    a, tau = paths.cir(normalize_delays=normalize_delays, out_type="numpy", **kw)
    return _c(a), np.asarray(tau)


# ---------------------------------------------------------------- free space
banner("free space, empty scene, iso V, 100 m")
scene = iso_scene(load_scene())
d = 100.0
scene.add([Transmitter("tx", position=mi.Point3f(0, 0, 10)),
           Receiver("rx", position=mi.Point3f(d, 0, 10))])
paths, dt = trace(scene)
a, tau = cir(paths)
print("trace s", round(dt, 3), "a shape", a.shape, "dtype", a.dtype, "tau shape", tau.shape,
      "tau dtype", tau.dtype)
valid = np.asarray(paths.valid)
print("valid shape", valid.shape, "n valid", int(valid.sum()))
amp = a[np.abs(a) > 0]
taus = tau[tau > 0]
print("paths", amp.size, "tau", taus, "d/c", d / C, "dtau ps", (taus - d / C) * 1e12)
friis = (LAM / (4 * np.pi * d)) ** 2
print("|a|^2 dB", 10 * np.log10(np.abs(amp) ** 2), "Friis dB", 10 * np.log10(friis),
      "diff dB", 10 * np.log10(np.abs(amp) ** 2 / friis))
ph = np.angle(amp * np.exp(2j * np.pi * F * d / C))
print("phase of a relative to exp(-j2pi f d/c) [rad]", ph,
      "(float64 carrier phase", 2 * np.pi * F * d / C, "rad)")
print("doppler (static)", np.asarray(paths.doppler))
a_norm, tau_norm = cir(paths, normalize_delays=True)
print("normalize_delays=True: tau", np.asarray(tau_norm).ravel()[:3], "a phase",
      np.angle(a_norm.ravel()[np.abs(a_norm.ravel()) > 0]))

# ---------------------------------------------------------------- determinism
banner("determinism")
p1, _ = trace(scene)
p2, _ = trace(scene)
a1, t1 = cir(p1)
a2, t2 = cir(p2)
print("deterministic=True identical:", np.array_equal(a1, a2) and np.array_equal(t1, t2))

# ---------------------------------------------------------------- arrays
banner("1x4 receive array, synthetic_array True / False")
for synth in (True, False):
    sc = iso_scene(load_scene(), rx_rows=1, rx_cols=4)
    sc.add([Transmitter("tx", position=mi.Point3f(0, 0, 10)),
            Receiver("rx", position=mi.Point3f(d, 0, 10))])
    p, dt = trace(sc, synthetic_array=synth)
    a, tau = cir(p)
    print("synthetic", synth, "a", a.shape, "tau", tau.shape, "doppler",
          np.asarray(p.doppler).shape, "trace s", round(dt, 3))

# ---------------------------------------------------------------- Doppler
banner("Doppler: receiver moving toward the transmitter at 30 m/s")
sc = iso_scene(load_scene())
v = 30.0
sc.add([Transmitter("tx", position=mi.Point3f(0, 0, 10)),
        Receiver("rx", position=mi.Point3f(d, 0, 10), velocity=mi.Vector3f(-v, 0, 0))])
p, _ = trace(sc)
dop = np.asarray(p.doppler)
print("doppler", dop.ravel()[:4], "expected +v/lambda", v / LAM, "shape", dop.shape)
a_t, tau_t = cir(p, sampling_frequency=1000.0, num_time_steps=4)
print("cir over time shape", a_t.shape)
z = a_t[..., 0, :].ravel()[:4] if a_t.shape[-1] == 4 else a_t.reshape(-1, 4)[0]
print("phase step per 1 ms", np.angle(z[1:] / z[:-1]), "expected", 2 * np.pi * v / LAM / 1000.0)

# ---------------------------------------------------------------- built-in reflector scenes
for name in ("simple_reflector", "floor_wall", "double_reflector"):
    banner(f"scene {name}")
    try:
        sc = iso_scene(load_scene(getattr(rt.scene, name)))
    except Exception as e:  # noqa: BLE001
        print("load failed:", e)
        continue
    for oname, obj in sc.objects.items():
        rm = obj.radio_material
        props = {k: getattr(rm, k) for k in ("name", "itu_type", "thickness",
                                              "relative_permittivity", "conductivity",
                                              "scattering_coefficient")
                 if hasattr(rm, k)}
        bb = getattr(obj.mi_mesh, "bbox", None)
        print(" object", oname, type(rm).__name__, {k: str(v) for k, v in props.items()},
              "bbox", bb() if callable(bb) else bb)
    sc.add([Transmitter("tx", position=mi.Point3f(-10, 0, 10)),
            Receiver("rx", position=mi.Point3f(10, 0, 10))])
    p, dt = trace(sc, max_depth=1)
    a, tau = cir(p)
    nz = np.abs(a) > 0
    order = np.argsort(tau.ravel())
    print(" trace s", round(dt, 3), "a", a.shape, "tau", tau.shape, "paths", int(nz.sum()),
          "tau ns", np.round(tau.ravel()[order] * 1e9, 4),
          "|a|^2 dB", np.round(10 * np.log10(np.abs(a.reshape(-1)[order]) ** 2 + 1e-300), 3))
    try:
        print(" interactions", np.asarray(p.interactions).ravel()[:8])
    except Exception as e:  # noqa: BLE001
        print(" interactions n/a:", e)

# ---------------------------------------------------------------- timing on a street scene
banner("timing: simple_street_canyon, max_depth 3")
sc = iso_scene(load_scene(rt.scene.simple_street_canyon))
sc.add([Transmitter("tx", position=mi.Point3f(-30, 0, 20)),
        Receiver("rx", position=mi.Point3f(40, 5, 1.5))])
p, dt = trace(sc, max_depth=3)
a, tau = cir(p)
print("trace s", round(dt, 2), "paths", int((np.abs(a) > 0).sum()))
p, dt = trace(sc, max_depth=3)
print("second trace s", round(dt, 2))
print(json.dumps({"done": True}))
