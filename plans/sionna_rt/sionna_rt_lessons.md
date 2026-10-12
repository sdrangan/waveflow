# Lessons: the ray-traced RF environment (Sionna RT)

What executing `plans/sionna_rt/sionna_rt.md` taught: surprises, failed approaches, and rules worth
keeping. Newest section last. Every measured number names its tool versions.

## M1: environment and spike (2026-10-12)

- **Sionna RT 2.2.0 picks the CPU variant by itself.** On a machine without CUDA, `import
  sionna.rt` selects `llvm_ad_mono_polarized` with no configuration (sionna-rt 2.2.0, Mitsuba 3.9.1,
  Dr.Jit 1.5.0, LLVM 20 found on the default path). The import takes about 1 s.
- **The extra installs cleanly next to numpy 2.5.3.** `pip install -e ".[sionna]"` adds sionna-rt,
  Mitsuba, Dr.Jit and a few notebook-widget packages; nothing else in `.venv` moves.
- **`cir(out_type='numpy')` returns one complex64 array, not a (real, imag) pair**, with a trailing
  time-step axis: `a` is `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths, num_time_steps]`.
  The docs' "tuple of real and imaginary tensors" describes the default Dr.Jit output.
- **`tau` and `doppler` carry antenna axes only without the synthetic array.** With
  `synthetic_array=True` (the default) they are `[num_rx, num_tx, num_paths]` and the per-antenna
  phase sits in `a`; with `False` they are `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths]`,
  and the 1×4 trace takes 0.59 s instead of 0.08 s. A channel file must store both layouts or
  normalize to the full one.
- **Free space is exact except for the float32 carrier phase.** At 100 m and 3.5 GHz: delay equal to
  d/c to the last float32 digit, power gain equal to Friis within 1e-6 dB, but the phase of `a` is
  3.6e-4 rad off e^(−j2πfd/c) (a carrier phase of 7,335 rad in float32). This is what AC5's 1e-3
  bound absorbs.
- **`normalize_delays=True` normalizes the phase too**: the line-of-sight path comes back with τ = 0
  and phase 0. Always trace with `normalize_delays=False` and keep the absolute delay.
- **Doppler is in Hz, positive when approaching**, and `cir(num_time_steps=…)` rotates `a` by
  2π·f_D/f_s per step: 30 m/s toward the transmitter gives +350.2423 Hz (v/λ = 350.2423 Hz).
- **`deterministic=True` makes two traces bit-identical.**
- **Sionna 2.x reflects off a slab, and it matches ITU-R P.2040 to 0.002 dB.** `floor_wall`'s
  concrete floor (ε′ = 5.24, σ = 0.123 S/m, 0.1 m thick) at 45° incidence, vertical polarization
  (TM, in the plane of incidence): Sionna −12.446 dB, the P.2040 slab formula −12.448 dB, a single
  Fresnel interface −11.619 dB. `simple_reflector` is a 1 m metal plate and gives |R|² = 1 exactly.
- **Traces are fast on the CPU for small scenes**: 0.08–0.8 s for the empty and reflector scenes,
  1.25 s for the first trace of `simple_street_canyon` and 0.14 s for the next (the first call pays
  the LLVM compilation).
