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
  reproduce a channel file's `.npz` byte for byte, so regenerating it is not a spurious diff. (The
  `.json` manifest records the trace's date, so a retrace on another day changes that one line;
  *corrected at the M6 review*.)
- **Sionna's variant is fixed at `import sionna.rt`.** Switching afterwards fails deep inside
  ("Plugin 'itu-radio-material' not found"); check `sys.modules` and say so up front.

## M4: the nodes and live mode (2026-10-12)

- **The `-k` trap again**: steps 11 and 12 verified with `-k channel` and `-k source`, which missed
  five tests. A prefix in every test name (`test_channel_*`, `test_source_*`) and a selector on the
  prefix is the fix that holds; the lesson from M3 was written but not applied.
- **One field name, one meaning.** `root` anchored the channel file on the new nodes and the bundle on
  `RfDataSource`; a harness copying the loopback's `tb.source.root = root` would have resolved the
  channel inside the scenario directory. `channel_root` says which.
- **Render the geometry you will replay.** A bundle rendered at one rate and replayed on another edge
  carries wrong delays and Doppler, and the C++ replay checks only the size. Take the geometry from
  the bound edge and refuse a mismatch.
- **"Same samples on the same install" needs a file from that install.** Comparing live mode with the
  committed reference fails wherever the reference was traced elsewhere; tracing the comparison file
  into a temporary directory is AC8's literal wording and works anywhere.
- **A path node that skips a dropped block shifts its output grid.** Emitting the channel's response
  to zero input for the missing block keeps the output edge on time and is what the air would do.
- **Exact beats tolerant when exact is available.** The ADC check went from relative error ≤ 1e-3 to
  word-for-word equality with the converter's own quantizer.

## M5: the channel sounder (2026-10-12)

- **An accuracy bound chooses the filter, and the default is not the example's.** AC7's 0.5 dB on
  every tap within 30 dB of the strongest reaches the sinc sidelobes far from the paths, where the
  node's Kaiser window departs from Sionna's exact sinc: 1.49 dB at the default 64 taps, 0.125 dB at
  256. Measuring the windowed-against-exact difference before the sounder existed made it a parameter
  choice recorded in §14, not a tolerance argument after a failure.
- **Seeded by name means the name is part of the scenario.** `RtSignalSource` keys its noise by its
  node's name, so the XSI graph (`xsi_tb_rt`) rendered a different bundle from the pysim one
  (`src_tb`). One constant names the testbench in both backends; "the same bytes in both backends" is
  checked, not assumed.
- **A loss-tolerant DUT changes the grain of bit-exactness, not its strength.** `rf_pass_through`
  drops ADC words while it writes, so only the first block reaches the sink whole. Distinct source
  words (noise makes all 1024 unique) let every sink word be placed in the source stream, in order.
  That is stronger than a first-block check and true, where the block-for-block wording was neither.
- **A bound that the measurement does not reach is slack, not a check.** "Skipped ≤ the ADC's drops"
  left 30 words of room (drops after the last delivered word), so an RTL losing words of its own would
  have passed. The gaps do not depend on the data, so the gate pins them exactly (M5 review, S1).
- **Check a description against the geometry, not the intent.** "A rooftop transmitter" was what the
  position was meant to be; ray casts showed open floor below it and no wall beside it (M5 review).
- **A hand-written main that names generated members fails only under `-m xsi`.** Renaming the
  testbench broke the counters main at the C++ compile. A toolchain-free test now checks every
  `h.<member>` the main reads against the generated harness.
- **"UP-TO-DATE" and "fresh" are different questions.** The DAG called csynth up to date while
  `rtl_staleness` (content stamps) showed the RTL was built from other bytes of a header. The stamp is
  the authority; `--force-step csynth` (15.8 s) settled it before any number was read off the RTL.
- **`-k <name>` also matches the module name.** `-k sounder` selects every test in
  `test_rt_sounder.py`. Harmless when it over-selects; the M3/M4 trap was under-selection.

## M6: the docs and the regression (2026-10-12)

- **Copy the newest convention, not the nearest example.** Step 18 committed the sounder's `xsi/`
  workspace because `rf_loopback` and `rf_blk_delay` do, and the M5 reviewer agreed; both are on
  `test_source_layout.py`'s not-yet-migrated list, and only the full regression saw it. Before copying
  a layout, check which examples were born under the current plan (`vitis_fft`, `ssr_fft`) and run the
  repo-wide lints (`tests/build/`) at the step, not at the close-out.
- **A gate that regenerates must not skip on what it generates.** The replay gate required `xsi/run.sh`
  before calling `generate_tb`, so untracking the workspace turned it into a skip. Skip only for what
  the test cannot make (Vivado, the RTL); generate, then assert the rest.
- **"Every number is tied" is a two-way check.** One direction rebuilds each figure from its source and
  looks for the phrase; the other refuses any prose number no phrase accounts for, so a number added
  later fails until it is tied. Output fences are left to the snippet harness, which runs them.
- **An example page's table can be the example's own output.** Running `main()` under `snippets: run`
  makes AC7's table on the page a checked artifact instead of a pasted one.
- **Never run `git stash` as a reflex.** One slipped into a check command and shelved the working tree;
  `git stash pop` restored it at once. Commands that touch the tree need the same care as commits.
- **A version floor is a test target.** `datetime.UTC` (3.11) slipped into a repo that declares 3.10,
  in the one function no test runs without Sionna; `ruff check --target-version py310` and a grep for
  3.11-only names are cheap at every milestone.
- **"Set membership" is not a tie.** Checking that each number on a page is *some* sourced number let
  a swap between two of them through (the review swapped the filter default 64 for 256 and passed).
  Rebuild each phrase from its source, as the example page already did.
- **A Must in the requirements needs its own test, even when it obviously works.** Tracing a user's
  XML scene was only ever hashed, never traced; the gate costs a second.

## Closing (2026-10-12)

- **What made an unfamiliar library at a new major version tractable:** a CPU spike before any code
  (variant, units, the amplitude convention, `normalize_delays`), a design note the user approved,
  and closed-form references (Friis, the image method, P.2040, v·cos θ/λ) rather than Sionna checked
  against itself.
- **What the milestone reviews were worth:** every one found real defects (wrong numbers in the log,
  a weak test, a false description, an unsupported Python), and two reviewers once agreed on a wrong
  convention. Independent review catches what the doer cannot see; the repo's own lints catch what
  a reviewer assumes.
- **Optional dependencies want a blocker, not a skip.** Running every non-Sionna test with the imports
  blocked, in a subprocess that refuses skips, kept "Sionna is optional" true across 22 steps.
- **Declare latency, never fit it.** The sounder's 384 samples is a sum of what the nodes declare;
  the test that only the declared latency passes is what makes the tap comparison mean something.
