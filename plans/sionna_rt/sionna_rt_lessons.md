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
- **Sionna 2.x reflects off a slab, and matches ITU-R P.2040 because it uses P.2040's equations.**
  `floor_wall`'s concrete floor (ε′ = 5.24, σ = 0.123 S/m, 0.1 m thick) at 45° incidence, vertical
  polarization (TM, in the plane of incidence): Sionna −12.448 dB, the P.2040 slab formula −12.4485
  dB, a single Fresnel interface −11.619 dB. `simple_reflector` is a 1 m metal plate: −0.002 dB.
- **Measure a reflection coefficient against free space on the unfolded path, never against another
  reflection.** The first recording subtracted the metal plate's reflected gain instead of the
  free-space loss over 28.284 m and was 0.002 dB off on both numbers; the reviewer caught it.
- **Traces are fast on the CPU for small scenes**: 0.08–0.8 s for the empty and reflector scenes,
  1.25 s for the first trace of `simple_street_canyon` and 0.14 s for the next (the first call pays
  the LLVM compilation).
- **Keep the spike's evidence in the repo.** The plan cited a scratch file in `/tmp` as evidence;
  the M1 review asked for it, and it now lives in `plans/sionna_rt/spike/`.
- **`SeedSequence(seed, spawn_key=(k,))` is `SeedSequence(seed).spawn()[k]`.** Keying noise chunks
  that way collides with any other stream spawned from the same seed; add a stream tag and the
  channel to the key.
- **Sionna pads invalid paths with τ = −1 and a = 0.** Every minimum, maximum or causality check on
  delays must mask them first.

## M2: the numpy core (2026-10-12)

- **`np.sinc(k)` is not 0 at a nonzero integer** (about 1e-17), so a sinc interpolator at zero
  fractional delay is not an exact pass-through. Special-case `mu == 0` to an impulse.
- **…and `k / f_s * f_s` is not always `k`.** For 87 of k in [1, 2000) it lands 6e-14 below, so the
  fraction is 1 − 6e-14 and the impulse special case never fires; the first test passed only because
  k = 17 happens to round-trip. Snap delays within 1e-9 samples of a whole number (M2 review).
- **Key everything on the absolute sample index.** Giving `process()` the block's absolute start
  index, rather than counting calls, made block continuity (3e-16), run-start independence and
  dropped-block handling one mechanism instead of three.
- **A per-path numpy convolve is about 18 M path-samples/s here** (numpy 2.5.3, i7-7700K): fine for
  test-sized runs, slow for long multi-antenna ones. Paths of one link can share one FIR only when
  their Doppler shifts are equal; with per-path Doppler the rotation stays per path, and a gathered
  einsum over paths is the way to vectorize it (the first version of this lesson was wrong; M2 review).
- **`Path.with_suffix` eats a dotted name.** `sounder_3.5ghz` became `sounder_3.npz`, and `chan.v2`
  and `chan.v3` overwrote each other. Strip only the suffixes you own and append the rest.
- **A blocker should fail the way the real thing fails.** A plain `ImportError` from the test blocker
  was not the `ModuleNotFoundError` of a real missing package; code that caught the narrower one would
  pass in a real install and fail under the blocker, or the reverse.
- **Cache a periodic waveform's period.** Rebuilding a degree-16 m-sequence per block was 25 ms a block,
  13× the channel applier; built once it is 14 µs.

## M3: the Sionna backend, references and cache (2026-10-12)

- **Mitsuba's `Point3f` refuses numpy scalars** ("Item assignment failed"). A specification that
  normalizes its coordinates to plain float triples protects every caller, and JSON wants plain
  numbers anyway.
- **The repo's blanket `*.json` ignore hides committed data.** A channel file's manifest was
  invisible to git until `.gitignore` re-included its directory, the same trap that left the markov
  and cpu_sched JSON files out of every commit. Check `git check-ignore` before relying on a new data
  file being committed.
- **A `-k` selector can quietly skip a parametrized case.** AC2's `-k reflector` matched only
  `simple_reflector`, so the concrete-slab case never ran under the plan's own verify command. Name
  a test after what the selector says, and collect with `--collect-only -k …` to see it.
- **"Never retrace" must not have a path that traces.** A missing half of a committed file fell
  through to tracing and rewriting `tests/rfenv/data/`.
- **Blocking an import is not hiding a package.** `importlib.metadata.version("sionna-rt")` still
  answered under the blocker, so the cache's staleness check behaved as if Sionna were installed.
  The blocker now hides the metadata too; the zero-skip assertion in the AC9 harness then caught a
  test that had been skipping on it.
- **A content hash must not include a path.** The same XML scene by a relative and an absolute path
  gave two cache keys, and its meshes were not hashed at all.
- **`np.savez` stamps the clock into the zip.** Writing entries with a fixed timestamp makes a retrace
  reproduce a channel file byte for byte, so regenerating it is not a spurious diff.
- **Sionna's variant is fixed at `import sionna.rt`.** Switching afterwards fails deep inside
  ("Plugin 'itu-radio-material' not found"); check `sys.modules` and say so up front.
