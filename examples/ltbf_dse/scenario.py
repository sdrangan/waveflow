"""scenario.py -- the Denver drops of the LTBF paper, re-traced with waveflow.rfenv.

``plans/mimo_cg/ltbf_cg_dse.md`` step 1.1.  The geometry is the paper's code, read for its
semantics (``salsa-ltbf/salsa/salsa/ray_tracing.py``, ``miutils.py``, ``salsa_main.py``):

* one BS, three sectors at yaws 0, 120 and 240 degrees, 40 m above the coverage-grid point nearest
  the origin; each sector a planar array, tr38901 pattern, V polarization, half-wavelength spacing;
* ten UEs per sector, uniformly on the 10 m coverage grid within 100 m - 2 km, outside buildings and
  inside the sector's 120-degree wedge, 1.5 m above the grid surface; a dipole each, pointed at the
  BS; speed uniform in 0 - 100 km/h, heading uniform;
* the UEs transmit; ``PathSolver`` with ``max_depth`` 3, ``samples_per_src`` 1e5,
  ``max_num_paths_per_src`` 100, the synthetic array, no diffraction or diffuse reflection;
* two snapshots per drop: the long-term statistics at the drop, the SINR one ``T_LT`` later with
  every UE moved by ``velocity * T_LT``.

**One element per sector, the array applied afterwards.**  Sionna's synthetic array traces once from
the array centre and multiplies each path's coefficient by ``exp(j 2 pi / lambda * r_ant . k_rx)``
(``sionna/rt/path_solvers/paths.py``, ``_apply_synthetic_array``).  Tracing one tr38901 element per
sector and keeping each path's arrival direction lets :mod:`examples.ltbf_dse.ltbf_link` apply any
array size to the same trace: the 32x32, 16x16 and 8x8 arrays are one trace, a drop is kilobytes
instead of hundreds of megabytes, and the tracing cost does not grow with the array.  The test
``test_synthetic_expansion_matches_sionna`` checks the expansion against Sionna's own synthetic array.

Everything this module writes is local (``examples/ltbf_dse/.gitignore``).

**Provenance.** Copied from ``paper/cg_dse`` (``examples/ltbf_cg/``, ``ba757884``) by
``plans/ltbf_dse/ltbf_dse.md`` step 0.5; the ``plans/mimo_cg/ltbf_cg_dse.md`` it cites lives on that
branch (``git show paper/cg_dse:plans/mimo_cg/ltbf_cg_dse.md``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.rfenv.sionna_backend import (
    ArraySpec,
    DeviceSpec,
    SceneSpec,
    SolverSpec,
    import_sionna,
    trace_with_paths,
    versions,
)

HERE = Path(__file__).resolve().parent
#: The repo-local copy of the paper's Denver scene (step 0.3).
SCENE_XML = HERE / "data" / "denver" / "denver.xml"
#: Where the traced drops go.
DROPS_DIR = HERE / "data" / "drops"

FC = 3.5e9
WAVELENGTH = 299_792_458.0 / FC
N_SECT = 3
#: UEs per sector.
N_UE = 10
GNB_HEIGHT = 40.0
UE_HEIGHT = 1.5
DIST_RANGE = (100.0, 2000.0)
SPEED_RANGE = (0.0, 100.0 * 5.0 / 18.0)
RX_BEAMWIDTH = 2.0 * np.pi / 3.0
GRID_SIZE = 10.0
BLDG_TOL = 1.0
T_LT = 10e-3
#: The paper's solver settings (``salsa_main.py:154-161``; ``ray_tracing.py:582-590``).
SOLVER = SolverSpec(
    max_depth=3,
    samples_per_src=100_000,
    max_num_paths_per_src=100,
    synthetic_array=True,
    diffuse_reflection=False,
    diffraction=False,
)
#: One tr38901 element per sector; the array is applied in ``ltbf_link``.
RX_ELEMENT = ArraySpec(num_rows=1, num_cols=1, pattern="tr38901", polarization="V")
UE_ARRAY = ArraySpec(num_rows=1, num_cols=1, pattern="dipole", polarization="V")


@dataclass(frozen=True)
class Grid:
    """The paper's coverage grid (``miutils.CoverageMapPlanner``): the surface height under each
    point, the height of the lowest surface, and whether a building stands there."""

    xgrid: np.ndarray
    ygrid: np.ndarray
    zmin: np.ndarray
    zmax: np.ndarray
    bldg: np.ndarray


def coverage_grid(scene_xml: Path = SCENE_XML, grid_size: float = GRID_SIZE) -> Grid:
    """Cast vertical rays through the scene from below and above, on a ``grid_size`` grid."""
    rt, mi = import_sionna()
    scene = rt.load_scene(str(scene_xml))
    m = scene.mi_scene
    b = m.bbox()
    x = np.arange(b.min.x, b.max.x, grid_size)
    y = np.arange(b.min.y, b.max.y, grid_size)
    xgrid, ygrid = np.meshgrid(x, y)
    xv, yv = xgrid.ravel(), ygrid.ravel()
    n = xv.size

    def cast(z0: float, dz: float) -> np.ndarray:
        p0 = mi.Point3f(xv, yv, np.full(n, z0))
        d = np.zeros((3, n))
        d[2] = dz
        si = m.ray_intersect(mi.Ray3f(p0, mi.Vector3f(d)))
        return np.array(si.p)[2].reshape(xgrid.shape)

    zmin = cast(b.min.z - 1.0, 1.0)
    zmax = cast(b.max.z + 1.0, -1.0)
    return Grid(xgrid, ygrid, zmin, zmax, (zmax - zmin) > BLDG_TOL)


@dataclass(frozen=True)
class Drop:
    """One Monte Carlo drop: the BS, the sector yaws, the UEs, their velocities and sectors."""

    seed: int
    gnb_pos: np.ndarray  # (3,)
    yaws: np.ndarray  # (N_SECT,)
    ue_pos: np.ndarray  # (N_SECT * n_ue, 3)
    ue_vel: np.ndarray  # (N_SECT * n_ue, 3)
    ue_sector: np.ndarray  # (N_SECT * n_ue,), the sector the UE was dropped in

    def positions(self, snapshot: int) -> np.ndarray:
        """UE positions at snapshot 0 (the drop) or 1 (one ``T_LT`` later)."""
        return self.ue_pos + self.ue_vel * (T_LT * int(snapshot))


def make_drop(seed: int, grid: Grid, n_ue: int = N_UE) -> Drop:
    """Place the BS and the UEs as ``ray_tracing.py`` (``load_scene``, ``drop_users``) does."""
    rng = np.random.default_rng(seed)
    d0 = grid.xgrid**2 + grid.ygrid**2
    i, j = np.unravel_index(np.argmin(d0), grid.xgrid.shape)
    gnb = np.array([grid.xgrid[i, j], grid.ygrid[i, j], grid.zmax[i, j] + GNB_HEIGHT])
    dist = np.sqrt(
        (grid.xgrid - gnb[0]) ** 2
        + (grid.ygrid - gnb[1]) ** 2
        + (grid.zmax - gnb[2]) ** 2
        + grid.zmin**2
    )
    angles = np.arctan2(grid.ygrid - gnb[1], grid.xgrid - gnb[0])
    yaws = 2.0 * np.pi * np.arange(N_SECT) / N_SECT
    pos, vel, sect = [], [], []
    for s, yaw in enumerate(yaws):
        dev = min(RX_BEAMWIDTH, 2.0 * np.pi / N_SECT)
        lo, hi = yaw - dev / 2.0, yaw + dev / 2.0
        if lo < -np.pi:
            lo, hi = lo + 2 * np.pi, hi + 2 * np.pi
        if hi > np.pi:
            lo, hi = lo - 2 * np.pi, hi - 2 * np.pi
        ok = np.where(
            (dist > DIST_RANGE[0])
            & (dist < DIST_RANGE[1])
            & ~grid.bldg
            & (angles > lo)
            & (angles < hi)
        )
        if ok[0].size < n_ue:
            raise ValueError(f"sector {s}: only {ok[0].size} grid points are eligible")
        pick = rng.choice(ok[0].size, n_ue, replace=False)
        r, c = ok[0][pick], ok[1][pick]
        p = np.column_stack(
            (grid.xgrid[r, c], grid.ygrid[r, c], grid.zmax[r, c] + UE_HEIGHT)
        )
        speed = rng.uniform(*SPEED_RANGE, size=n_ue)
        heading = rng.uniform(0.0, 2.0 * np.pi, size=n_ue)
        v = np.column_stack(
            (speed * np.cos(heading), speed * np.sin(heading), np.zeros(n_ue))
        )
        pos.append(p)
        vel.append(v)
        sect.append(np.full(n_ue, s))
    return Drop(
        seed=int(seed),
        gnb_pos=gnb,
        yaws=yaws,
        ue_pos=np.vstack(pos),
        ue_vel=np.vstack(vel),
        ue_sector=np.concatenate(sect),
    )


def scene_spec(
    drop: Drop,
    snapshot: int,
    scene_xml: Path = SCENE_XML,
    rx_array: ArraySpec = RX_ELEMENT,
) -> SceneSpec:
    """The ``SceneSpec`` of one snapshot: the UEs transmit, the three sectors receive."""
    pos = drop.positions(snapshot)
    tx = tuple(
        DeviceSpec(
            f"ue-{k}",
            tuple(pos[k]),
            look_at=tuple(drop.gnb_pos),
            velocity=tuple(drop.ue_vel[k]),
        )
        for k in range(pos.shape[0])
    )
    rx = tuple(
        DeviceSpec(f"gnb-{s}", tuple(drop.gnb_pos), orientation=(float(yaw), 0.0, 0.0))
        for s, yaw in enumerate(drop.yaws)
    )
    return SceneSpec(
        scene=str(scene_xml),
        frequency_hz=FC,
        transmitters=tx,
        receivers=rx,
        tx_array=UE_ARRAY,
        rx_array=rx_array,
        solver=SOLVER,
    )


def trace_snapshot(drop: Drop, snapshot: int, scene_xml: Path = SCENE_XML) -> dict:
    """Trace one snapshot with one element per sector; return the compact path record.

    ``a`` (n_sect, n_ue_tot, P) complex, ``tau`` (n_sect, n_ue_tot, P) seconds, ``valid`` and
    ``k_rx`` (n_sect, n_ue_tot, P, 3), Sionna's stored arrival direction of each path.
    """
    spec = scene_spec(drop, snapshot, scene_xml)
    ps, paths = trace_with_paths(spec)
    k = (
        paths._k_rx
    )  # the vectors _apply_synthetic_array uses, not the ill-conditioned angles
    kx, ky, kz = (np.array(paths._k_component(k, c)) for c in range(3))
    shape = ps.a.shape  # (n_sect, n_ue_tot, P) with one element at each end
    k_rx = np.stack([kx.reshape(shape), ky.reshape(shape), kz.reshape(shape)], axis=-1)
    a, tau, valid = np.asarray(ps.a), np.asarray(ps.tau), np.asarray(ps.valid)
    # Sionna's path order within a link can change from one run to the next (the same paths,
    # permuted).  Sort each link's paths by delay, invalid ones last, so a retrace of the same seed
    # writes the same bytes.
    key = np.where(valid, tau, np.inf)
    order = np.argsort(key, axis=-1, kind="stable")
    take = lambda x: np.take_along_axis(x, order, axis=-1)
    return {
        "a": take(a),
        "tau": take(tau),
        "valid": take(valid),
        "k_rx": np.take_along_axis(k_rx, order[..., None], axis=-2),
        "spec_hash": ps.manifest["spec_hash"],
    }


def drop_files(seed: int, out_dir: Path = DROPS_DIR) -> tuple[Path, Path]:
    return out_dir / f"drop_{seed:05d}.npz", out_dir / f"drop_{seed:05d}.json"


def trace_drop(
    seed: int, grid: Grid, out_dir: Path = DROPS_DIR, scene_xml: Path = SCENE_XML
) -> Path:
    """Trace both snapshots of drop ``seed`` into ``drop_<seed>.npz`` + ``.json`` (skipped if done)."""
    npz, js = drop_files(seed, out_dir)
    if npz.is_file() and js.is_file():
        return npz
    out_dir.mkdir(parents=True, exist_ok=True)
    drop = make_drop(seed, grid)
    snaps = [trace_snapshot(drop, s, scene_xml) for s in (0, 1)]
    arrays = {
        "gnb_pos": drop.gnb_pos,
        "yaws": drop.yaws,
        "ue_pos": drop.ue_pos,
        "ue_vel": drop.ue_vel,
        "ue_sector": drop.ue_sector,
    }
    for s, rec in enumerate(snaps):
        for name in ("a", "tau", "valid", "k_rx"):
            arrays[f"{name}{s}"] = rec[name]
    tmp = npz.with_name(npz.name + ".tmp.npz")
    np.savez(tmp, **arrays)
    tmp.replace(npz)
    js.write_text(
        json.dumps(
            {
                "seed": int(seed),
                "spec_hash": [rec["spec_hash"] for rec in snaps],
                "versions": versions(),
                "scene_xml": str(scene_xml.relative_to(HERE)),
            },
            indent=1,
        )
    )
    return npz


def load_drop(seed: int, out_dir: Path = DROPS_DIR) -> dict:
    with np.load(drop_files(seed, out_dir)[0]) as f:
        return {k: f[k] for k in f.files}


def main(argv: list[str] | None = None) -> None:
    """Trace drops ``first .. first+count-1`` (resumable: finished drops are skipped)."""
    import argparse
    import time

    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--threads", type=int, default=4, help="Dr.Jit worker threads")
    a = ap.parse_args(argv)
    import drjit as dr

    import_sionna()
    dr.set_thread_count(a.threads)
    grid = coverage_grid()
    t0 = time.time()
    for seed in range(a.first, a.first + a.count):
        trace_drop(seed, grid)
        print(f"drop {seed} done at {time.time() - t0:.1f} s", flush=True)
    (DROPS_DIR / f"done_{a.first}_{a.count}").write_text(f"{time.time() - t0:.1f}\n")


if __name__ == "__main__":
    main()
