# Plan: a ray-traced RF environment (Sionna RT) for Waveflow

**Status:** In progress (M2 started 2026-10-12)
**Complexity:** L3 Complex. A new framework package, an example, docs and tests; open design choices; an unfamiliar library at a new major version, run CPU-only. Scores: scope 3 · clarity 3 · novelty 3 · dependencies 3 · verification 3 · risk 2
**Created:** 2026-10-12 · **Updated:** 2026-10-12 (revised after the independent plan review)
**Plan file:** `plans/sionna_rt/sionna_rt.md` · **Lessons learned:** `plans/sionna_rt/sionna_rt_lessons.md` (created at step 1)
**Branch:** `feat/sionna-prod`

> **For the agent executing this plan:** read *Rules for the doer* first. The
> plan is self-contained. Paths are repo-relative, commands are exact, and
> secrets are named by environment variable only (this task needs none).

## Rules for the doer

1. Before changing anything, re-run the readiness commands in §8. Stop if
   anything required is ❌ blocked.
2. Do one step at a time (§10). A step is done only when its exit condition is
   verified with the stated method. Show the command and its result as evidence.
   Before changing code, read it and its callers. **Check the Sionna RT 2.2.0
   documentation and source (§5) for every Sionna call**: the API is newer than
   the doer's training data, so names, signatures, array shapes and precision
   come from the docs, the installed source and the step 2 spike, never from
   memory. Before building a tool, script or integration the plan didn't
   foresee, search for an existing one and propose it first.
3. After each step, tick it in §10, add a line to the §15 progress log, and
   append anything surprising to `plans/sionna_rt/sionna_rt_lessons.md`.
4. When something fails, find the root cause from the error, logs, tests and
   actual state. No random fixes. Deviation policy: investigate up to **two**
   tested hypotheses, then stop and report what you know.
5. Pause for the user **after each milestone** (§10) and on any surprise. At
   each milestone, run the independent reviewer agent (§13) first, and show its
   findings with the milestone summary.
6. Never perform an action listed in §12 as irreversible without its backup in
   place and fresh approval from the user at that moment. Any single run
   expected to take more than **1 hour of CPU** needs the user's approval at the
   time.
7. Never merge, push, deploy or publish unless the user asks.
8. State assumptions and anything you couldn't verify. Never fill a gap with a
   guess.
9. Commit after each verified step on `feat/sionna-prod`, with a message that
   names the step (for example `rfenv(5): ChannelApplier`), ending with the
   repo's agent attribution line. Stage files by explicit path, never
   `git add -A` or `git add .`. Never stage `.gitignore` or `docs/repo_docs/`
   unless a step says so.
10. **Framework changes are shown first.** Any diff under `waveflow/`,
    `docs/guide/`, `tests/conftest.py`, `pyproject.toml` or `CLAUDE.md` is
    shown to the user before it is committed (the 👁 steps in §10).
11. Run everything inside the repo's virtual environment
    (`source .venv/bin/activate`). Every measured number (timings, tool
    outputs) names its tool versions (sionna-rt, Mitsuba, Dr.Jit; Vitis/Vivado
    2024.1 for the XSI steps).
12. Never change a test's tolerance, seed or reference to make it pass. A
    tolerance in §4 changes only with the user's approval, recorded in §14.
13. **A node's `DynParam`s become C++ assignments on its XSI model.** The
    generator emits every non-default `DynParam` as `<model>.<field> = …;`
    (`waveflow/hw/hw_module.py` around line 140, `waveflow/build/composite_gen.py`
    around line 2292). `RtSignalSource` may therefore expose only `in_bundle` as
    a `DynParam` (the field `RfFileSource` has), and its endpoint must be named
    `rf_ep`, as `RfDataSource`'s is. Every other parameter is a plain field.
14. Finish with §16: check every acceptance criterion, run the regression
    check, and write the completion report.

## 1. Goal

Give Waveflow a **reusable, ray-traced RF environment**. A channel computed by
NVIDIA Sionna RT from a 3D scene (transmitters, receivers, antenna arrays,
materials, motion) drives two pysim blocks on the existing RF sample interface
(`RFSampIF`):

- a **signal source** (`RtSignalSource`) that emits what a receiver would see:
  a built-in or user waveform, sent through the ray-traced channel, at a
  physical received power, with thermal noise;
- a **channel block** (`RtChannel`) that sits between any transmitter's RF
  output and any receiver's RF input (for example an `Rfdc` DAC and an `Rfdc`
  ADC) and applies the ray-traced multipath, delays and Doppler to whatever
  passes through. It is the multipath-fidelity sibling of the existing
  bulk-delay path node `RfSampDelay`.

Any design that already talks to an `Rfdc` can then run end to end against a
realistic propagation environment, in any experiment or example, without
changing its own code. By default the ray tracing is done once and cached as a
**channel file**, so simulation and tests run without Sionna installed and the
XSI testbench can replay the source's output. On request, the blocks trace the
scene inside the simulation instead (**live mode**).

## 2. Requirements and constraints

| Type | Item |
|---|---|
| Must | One channel core used two ways: `RtSignalSource` (an `RFSampIFTx` producer named `rf_ep`, like `RfDataSource`) and `RtChannel` (`rf_in`, an `RFSampIFRx`; `rf_out`, an `RFSampIFTx`; like `RfSampDelay`). Both are pysim `HwModule`s that compose with existing blocks by binding `RFSampIF` edges, with no change to those blocks. |
| Must | Ray tracing is **precomputed and cached** by default: a build step traces a scene specification once and writes a channel file. **Live mode** (tracing inside the simulation, at `pre_sim`) is available on request and gives the same samples on the same install. |
| Must | Static scenes plus **Doppler**: moving transmitters and receivers get per-path Doppler shifts from their velocities (Sionna's first-order model), carried continuously across blocks. What this leaves out is documented: path delays and gains do not drift during a run, and the transmit and receive tiles keep one shared epoch. |
| Must | **Antenna arrays**: the input edge's `n_ch` equals the transmit antenna count, the output edge's `n_ch` the receive antenna count. The first tests are SISO; a 1×4 case is required. |
| Must | **Complex baseband** at the `RFSampIF` (`complex_samp=True`). The carrier frequency is a parameter of the scene; the bandwidth is the edge's sample rate. Any `Rfdc` on such an edge uses an I/Q word, `Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)` (as `examples/rf_loopback/rf_loopback_xsi.py` line 222), because a real word refuses complex samples with a non-zero imaginary part (`waveflow/hw/rfdc.py` around line 534). One `Rfdc` has one word for both directions. |
| Must | The channel's effect is continuous across block boundaries: the output does not depend on `blksize` (sub-sample delays and Doppler phase carry state across blocks). Its causality and latency are declared: the node's added latency (a fixed delay for the filter's look-ahead, or a block, settled in step 3) is a `blk_latency`/delay attribute that loop-latency sums and `assert_clean(startup_blocks=…)` read, as `RfSampDelay.blk_latency` and `rf_shot_loopback.loop_blk_latency` do. |
| Must | Built-in waveforms: tone, Zadoff-Chu, PN, plus any user array or RF bundle. Received power from the transmit power (dBm) and the ray-traced path gains; a `gain_db` maps the result onto the converter's `full_scale`. Thermal noise from kTB plus a noise figure, seeded and reproducible. |
| Must | Scenes: Sionna's built-in scenes and any user-supplied Mitsuba XML scene. |
| Must | Sionna is an **optional extra** (`pip install -e ".[sionna]"`), imported lazily. Tests that ray-trace carry a `sionna` pytest marker; everything else runs without Sionna. |
| Must | The source's output can be rendered as an RF bundle, so the existing C++ `RfFileSource` replays it in XSI (bundle discipline, `waveflow/simulation/rf_tb.py`). |
| Must | A new worked example, `examples/rt_sounder`: a channel sounder whose capture recovers the ray-traced channel's taps. |
| Must not | Change the behavior or interface of `RFSampIF`, `Rfdc`, `RfDataSource`, `RfDataSink`, `RfSampDelay` or any existing example. (Signal processing stays out of the interface: `plans/adc_model.md`, *Signal processing stays out of the interface*.) |
| Must not | Make the base install, or any test outside the `sionna` marker, need Sionna, Mitsuba or Dr.Jit. |
| Must not | Put Sionna-specific code outside `waveflow/rfenv/sionna_backend.py` (the channel file and its application are generator-neutral, so other channel generators can feed them later). |
| Nice to have | OFDM waveform; real-valued (IF) edges with up/down-conversion; re-tracing along a trajectory in live mode (`retrace_period`), which would add delay drift; a figure of the scene's paths. |
| Constraint | CPU only on this machine (no NVIDIA GPU): Sionna RT runs on Dr.Jit's LLVM backend. Sionna's polarized variants compute in **single precision**, which bounds how closely NumPy can match Sionna (AC5). Any run over 1 h of CPU needs approval at the time. |
| Constraint | Python 3.12 and numpy 2.5.3 in `.venv`; `sionna-rt` 2.2.0 resolves against it with no TensorFlow or PyTorch (dry run, 2026-10-12). |
| Constraint | Non-commercial Waveflow Research License for the repo; Sionna RT is Apache-2.0 and Mitsuba 3 is BSD-style, both compatible with use as an optional dependency. |

**Out of scope:** building scenes from OpenStreetMap or Blender; carrier
frequency offset, phase noise and other RF impairments; a C++ (XSI) model of
`RtChannel`; radio maps; GPU support beyond what Sionna does by itself.

## 3. Deliverables

| Deliverable | Format | Location | For whom |
|---|---|---|---|
| The `rfenv` package: channel file (`PathSet`), block-rate application (`ChannelApplier`, `taps`), waveforms, noise, the Sionna backend, the cache, and the two nodes | Python package | `waveflow/rfenv/` | Waveflow users building RF experiments |
| Tests: no-Sionna tests, an import-blocking pytest plugin, `sionna`-marked reference tests, the example's tests and its XSI replay gate | pytest | `tests/rfenv/`, `tests/examples/test_rt_sounder*.py` | maintainers |
| The optional extra and the `sionna` marker | config | `pyproject.toml`, `tests/conftest.py` | maintainers |
| Worked example: channel sounder | Python + build | `examples/rt_sounder/` | users learning the block |
| Committed reference channel files for the tests and the example | `.npz` + `.json` manifest | `tests/rfenv/data/`, `examples/rt_sounder/data/` | tests run without Sionna |
| Guide page | Markdown | `docs/guide/rf/rfenv/index.md` (`parent: RF converters`), linked from `docs/guide/rf/index.md` | users |
| Example page | Markdown | `docs/examples/rt_sounder/index.md` (front matter with `example_dir:`) | users |
| `CLAUDE.md` subsystem bullet for `waveflow/rfenv/`, and the curated-example count updated | Markdown | `CLAUDE.md` | future agent sessions |
| Lessons learned, progress log, completion report | Markdown | `plans/sionna_rt/` | the user |

## 4. Acceptance criteria

The task is done when every box is ticked with the evidence named. The user
accepted these on 2026-10-12; AC5, AC6 and AC7 were revised with the user's
approval after the plan review the same day. A change needs the user's
approval, recorded in §14. "Relative error" means the norm-wise
‖x − y‖₂ / ‖y‖₂ over the compared samples.

- [ ] **AC1 Free space.** An empty scene (`load_scene()` with no file) with one transmitter and one receiver, both arrays a single `iso` element with `V` polarization, at 3.5 GHz and 100 m apart, gives exactly one path. With absolute delays (`normalize_delays=False`), its delay equals d/c within 1 ps and its power gain |a|² equals Friis, (λ/4πd)², within 0.01 dB. Verify: `pytest -m sionna -rs tests/rfenv/test_sionna_reference.py -k free_space`.
- [ ] **AC2 Single reflector.** A scene with one finite plate of documented material and thickness (read from the scene, not assumed), with the specular point inside the plate, gives the line-of-sight path plus the specular reflection. Path count and delays match the image-method geometry within 1 ps. The reflected path's power gain matches the closed-form reference for that material within 0.5 dB, at a stated polarization (TE or TM): a perfect conductor has |R| = 1; a dielectric uses the ITU-R P.2040 slab formula at the plate's thickness. Step 2 picks the scene (built-in `simple_reflector` is metallic) and records it in §14. Verify: `pytest -m sionna -rs tests/rfenv/test_sionna_reference.py -k reflector`.
- [ ] **AC3 Block continuity and accuracy.** Applying a channel block by block equals applying it to the whole signal at once with the same filter, with relative error ≤ 1e-12, for `blksize` ∈ {1, 7, 64, 1024}. With the default filter (length and window fixed in step 3), a signal whose occupied bandwidth is at most 0.8·fs/2, through one path with a sub-sample delay, matches the analytically delayed signal with error power ≤ −60 dB relative to the signal, excluding the filter's edge transients at the start and end. Verify: `pytest tests/rfenv/test_apply.py`.
- [ ] **AC4 Doppler.** Sign convention as in Sionna's docs, f_Δ = (1/λ)(v₀ᵀk̂₀ − v_{n+1}ᵀk̂_n + Σ…): for a receiver moving at speed v, θ is the angle between its velocity and the direction back along the arriving path (−k̂_n), so an approaching receiver sees a positive shift f_D = v·cos θ / λ. A tone through a path with shift f_D comes out shifted by f_D within 0.1 Hz, continuously across blocks, measured by a least-squares fit of the unwrapped phase slope (not an FFT bin). Verify: `pytest tests/rfenv/test_apply.py -k doppler` (synthetic path) and `pytest -m sionna -rs tests/rfenv/test_sionna_reference.py -k doppler` (Sionna's `doppler` for a traced path equals v·cos θ / λ within 0.1 Hz).
- [ ] **AC5 Cross-check with Sionna.** For the same traced paths, with absolute delays on both sides (`normalize_delays=False`), `rfenv.taps()` matches Sionna's `Paths.taps()` (same bandwidth, sampling frequency and lag range) with relative error ≤ 1e-3, a bound set by Sionna's single-precision phase at about 7,000 rad of carrier phase. Run for a SISO case and a 1×4 receive-array case. Verify: `pytest -m sionna -rs tests/rfenv/test_sionna_reference.py -k taps`.
- [ ] **AC6 Source on the interface, and XSI replay.** `RtSignalSource` feeds an `Rfdc` ADC (I/Q word) in a pysim graph for at least 64 blocks with clean counters (`RFSampIF.assert_clean(startup_blocks=…)` with the declared startup). In the XSI run of the same scenario, with the source replaced by `RfFileSource` replaying the rendered bundle, **every data block that reaches the sink is bit-identical to the matching pysim block**. The startup transient and the blocks the pass-through drops at RTL are counted and asserted against stated expectations, the way `tests/examples/test_rf_loopback_xsi.py` gates the loopback. Verify: `pytest tests/rfenv/test_nodes.py -k source` and `pytest -m xsi -rs tests/examples/test_rt_sounder_xsi.py` (0 skipped).
- [ ] **AC7 Channel sounder.** In `examples/rt_sounder`, with noise off and the converter level set by `gain_db` so the strongest tap sits at −10 dBFS, the circular correlation of the capture with the known Zadoff-Chu sequence, after subtracting the declared loop latency (the converter hop, `RtChannel`'s latency and the filter's group delay, summed as `rf_shot_loopback`'s `loop_blk_latency` does), is compared with `rfenv.taps(pathset, bandwidth = sample rate)` at integer lags. For every tap stronger than −30 dB relative to the strongest: the same lag, and |ĥ|² within 0.5 dB. Verify: `pytest tests/examples/test_rt_sounder.py -k sounder`.
- [ ] **AC8 Live mode.** On the same install (same sionna-rt, Mitsuba and Dr.Jit versions and the same CPU variant), running a node in live mode (tracing at `pre_sim`) gives the same output samples as a channel file written by that install for the same scene specification, with relative error ≤ 1e-6. Verify: `pytest -m sionna -rs tests/rfenv/test_live.py`.
- [ ] **AC9 Install without Sionna.** With `sionna`, `mitsuba` and `drjit` imports blocked by a pytest plugin (`tests/rfenv/_block_sionna.py`), in a subprocess: `import waveflow.rfenv` works, committed channel files load, and `RtChannel` and `RtSignalSource` run in cached mode; live mode raises a message naming `pip install -e ".[sionna]"`; and **every non-`sionna` test in `tests/rfenv/` and `tests/examples/test_rt_sounder.py` passes under the blocker**. Verify: `python -m pytest -p tests.rfenv._block_sionna -m "not sionna" tests/rfenv/ tests/examples/test_rt_sounder.py` (and `tests/rfenv/test_no_sionna.py`, which runs that in a subprocess).
- [ ] **AC10 Regression.** The fast suite shows no failures beyond the 6 recorded on 2026-10-11 (§11). `pytest -m sionna -rs` runs with 0 skipped and collects at least `WANT_SIONNA_GATES`. New files pass `ruff check` and `black --check` (black's default line length, 88; the repo has no black config); changed existing files have no more ruff findings than on `main`. Verify: the commands in §11.
- [ ] **AC11 Docs.** The guide page `docs/guide/rf/rfenv/index.md` (`parent: RF converters`, `snippets: run`, so `tests/docs/test_doc_snippets.py` executes its snippet) and the example page `docs/examples/rt_sounder/index.md` exist and are linked. Every number on the two pages has an entry in `tests/docs/test_documented_numbers.py` tying it to a committed file or test. `CLAUDE.md` gains a `waveflow/rfenv/` bullet and its curated-example count goes from twenty to twenty-one, approved by the user. Verify: `pytest tests/docs tests/mcp/test_knowledge_corpus.py` and the user's approval in §15.

## 5. Context and sources of truth

| Source | What it is | Access | Freshness / version |
|---|---|---|---|
| Sionna RT documentation | The authority for every Sionna call (scene, devices, arrays, `PathSolver`, `Paths`, radio materials) | https://nvlabs.github.io/sionna/rt/index.html; API pages under `api/` (`scene.html`, `radio_devices.html`, `antenna_array.html`, `paths_solvers.html`, `paths.html`, `radio_materials.html`, `utils.html`); tutorials `tutorials.html` (*Introduction to Sionna RT*, *Tutorial on Mobility*); developer guide `developer/dev_understanding_paths.html` | Sionna 2.2.0 (checked 2026-10-12) |
| Sionna RT source | Ground truth where the docs are silent (variant selection, shapes, precision, the phase reference) | https://github.com/NVlabs/sionna-rt, and after step 1 the installed package in `.venv/lib/python3.12/site-packages/sionna/rt/` (`__init__.py` for the variant; `path_solvers/paths.py` or equivalent for `cir`/`taps`) | 2.2.0 |
| Mitsuba 3 / Dr.Jit docs | Variant selection (`llvm_ad_mono_polarized`), LLVM backend requirements (`DRJIT_LIBLLVM_PATH`) | https://mitsuba.readthedocs.io, https://drjit.readthedocs.io | Mitsuba 3.9.1, Dr.Jit 1.5.0 |
| ITU-R P.2040 | Reference reflection coefficients of building materials and slabs, for AC2 with a dielectric plate | ITU-R Recommendation P.2040 (the edition Sionna cites in its radio-materials page) | as cited by Sionna 2.2.0 |
| Facts already read from the 2.2.0 docs (2026-10-12) | `load_scene(filename=None, merge_shapes=True, …)` (no file gives an empty scene); built-in scenes `sionna.rt.scene.<name>` incl. `simple_reflector` (metallic), `double_reflector`, `floor_wall`, `simple_street_canyon`, `munich`, `box`; `Transmitter(name, position, orientation=None, look_at=None, velocity=None, power_dbm=44, …)`, `Receiver(name, position, orientation=None, look_at=None, velocity=None, …)`; `PlanarArray(num_rows, num_cols, pattern, polarization, …)`; `PathSolver(deterministic=False)` called as `solver(scene, max_depth=3, …, synthetic_array=True, los=True, specular_reflection=True, diffuse_reflection=False, refraction=True, diffraction=False, seed=42)`, deterministic only with `deterministic=True`; `Paths.cir(*, sampling_frequency=1.0, num_time_steps=1, normalize_delays=True, reverse_direction=False, out_type='drjit')`, `Paths.taps(bandwidth, l_min, l_max, sampling_frequency=None, num_time_steps=1, normalize=False, normalize_delays=True, …)` (`sampling_frequency=None` means the bandwidth); `a` as (real, imag) shaped `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths]`; `tau` that shape or `[num_rx, num_tx, num_paths]`; attributes `doppler`, `valid`, `theta_t/phi_t/theta_r/phi_r`; baseband coefficient a_i·e^(−j2πfτ_i)·e^(j2πf_Δ,i·t); taps h̄_{n,ℓ} = Σ_i a^b_i(n/f_s)·sinc(ℓ − W·τ_i); f_Δ = (1/λ)(v₀ᵀk̂₀ − v_{n+1}ᵀk̂_n + Σ_{i=1}^{n} v_iᵀ(k̂_i − k̂_{i−1})). **From the source, per the plan review:** the `*_mono_polarized` variants compute in single precision, and with `normalize_delays=True` (the default) the phase e^(−j2πfτ) uses the **normalized** delay. **Not documented:** how the CPU variant is chosen and which `tau` shape applies when; step 2 settles both. | docs and source above | 2.2.0 |
| RF sample interface | The edge the blocks bind to: `(n_ch, blksize)` blocks, `complex_samp`, backpressure, loss counters, `assert_clean(startup_blocks)` | `waveflow/hw/rf_sample_if.py` | HEAD of `feat/sionna-prod` |
| Existing RF nodes and bundle format | `RfDataSource` (endpoint `rf_ep`, `in_bundle`, `bfm_model()` → `RfFileSource`), `RfDataSink`, `RfSampDelay` (the `Channel` block at bulk-delay fidelity, PR #202: `rf_in`/`rf_out`, `delay_samp`, `blk_latency`, cross-edge checks in `pre_sim`, `assert_ran`), `write_rf_bundle`, `read_rf_bundle` | `waveflow/simulation/rf_tb.py` (`RfSampDelay` from line 365) | HEAD |
| RF data converter | `Rfdc`, its `full_scale`, `on_rf_bind` (reads the rate, pushes `t0`, checks `n_ch` and `iq_mode`), `rf_samples` (refuses complex samples on a real word); `Rfsoc4x2SampWord` | `waveflow/hw/rfdc.py`, `waveflow/hw/rfdc_samp_word.py`; guide `docs/guide/rf/rfdc/` | HEAD |
| Why a `Channel` block and not the interface | The reserved design: multipath belongs in a pysim-only block with state across blocks | `plans/adc_model.md` (section *`Channel`, `RfDataSource`, `RfDataSink`*); `docs/guide/rf/rfdc/sampling.md` (around line 160); `plans/rfsoc_4x2_bringup.md` (*Components to model*, *inter-block state*) | HEAD |
| DAC → path → ADC wiring | One `Rfdc` owning both tiles' `t0`; the path node between `tx_rf` and `rx_rf`; `loop_blk_latency` and `channel_delay` | `examples/rf_shot_loopback/rf_shot_loopback.py`; page `docs/examples/rf_shot_loopback/` | HEAD |
| ADC → fabric → DAC and its XSI flow | `RfSampPassThrough`; the I/Q gate (`XSI_IQ_WORD`); `rf_dut_build.generate_dut` and the RTL build DAG; what XSI drops and the startup transient | `examples/rf_loopback/` (`rf_loopback.py` line 180, `rf_loopback_xsi.py` from line 206, `rf_dut_build.py`); gate `tests/examples/test_rf_loopback_xsi.py` (its header and the recorded drop counts); a pass-through that never drops: `examples/rf_blk_delay` | HEAD |
| XSI behavioral edges and DynParams | How pysim-only nodes are replaced at the RF boundary; how DynParams become BFM member assignments | `plans/behavioral_edges.md`; `RFSampIF.xsi_model()`; `waveflow/hw/hw_module.py` (around line 140), `waveflow/build/composite_gen.py` (around line 2292) | HEAD |
| Gate-marker pattern | How `xsi` and `gem5` sessions fail on a skip and enforce a collected-count floor | `tests/conftest.py` (`_GATES`; the floor is `WANT_<MARKER>_GATES`) | HEAD (`WANT_XSI_GATES` = 185, `WANT_GEM5_GATES` = 12) |
| Docs tests | Snippet execution and documented-number checks | `tests/docs/test_doc_snippets.py` (`snippets: run` front matter), `tests/docs/test_documented_numbers.py` | HEAD |
| Project rules | Commands, venv activation, tool versions next to numbers, curated examples | `CLAUDE.md` | HEAD |

## 6. Environment

- **OS / shell:** Linux 6.17 (x86-64), bash. 8 cores, 31 GB RAM. No NVIDIA GPU (Intel HD Graphics 630 only).
- **Languages and runtimes:** Python 3.12.3 in `.venv` (activate with `source .venv/bin/activate`; activation also sets up the Vivado XSI environment).
- **Key dependencies:** numpy 2.5.3, scipy 1.18.1, simpy, matplotlib (present). To add through the extra: `sionna-rt` 2.2.0, which brings `mitsuba` 3.9.1 and `drjit` 1.5.0 (dry-run resolution, 2026-10-12). LLVM shared libraries 17–20 are installed (for example `/lib/x86_64-linux-gnu/libLLVM.so.20.1`, `/usr/lib/llvm-18/lib/libLLVM.so.1`) for Dr.Jit's CPU backend; `DRJIT_LIBLLVM_PATH` selects one if the default lookup fails.
- **Toolchain for the XSI steps:** Vitis HLS and Vivado 2024.1 under `/tools/Xilinx/` (`vitis-run`, `vivado`).
- **Build / run / test commands:**
  - Install the extra: `pip install -e ".[sionna]"`
  - Fast suite: `pytest -m "not vitis and not xsi and not gem5 and not sionna"`
  - Ray-tracing tests: `pytest -m sionna -rs`
  - Without Sionna: `python -m pytest -p tests.rfenv._block_sionna -m "not sionna" tests/rfenv/ tests/examples/test_rt_sounder.py`
  - XSI gates for the example: `pytest -m xsi -rs tests/examples/test_rt_sounder_xsi.py`
  - Lint: `ruff check waveflow/rfenv tests/rfenv examples/rt_sounder`, `black --check` on the same paths (default line length 88; read black's exit code, since `black -q` prints nothing either way)
- **Project structure (the parts that matter):** `waveflow/hw/` (interfaces, `Rfdc`), `waveflow/simulation/` (pysim, `rf_tb.py`, `stream_tb.py`), `waveflow/build/` (build steps, codegen), new `waveflow/rfenv/`; `examples/<name>/`; `tests/` mirrors the package; `docs/guide/` and `docs/examples/<name>/index.md`; `plans/`.
- **Limitations:** CPU-only ray tracing in single precision; `PathSolver` results are reproducible only with `deterministic=True`; a Mitsuba variant must be set before `import sionna.rt`, which only picks one if none is set; numbers measured here are Vitis/Vivado 2024.1 (`CLAUDE.md` notes 2024.1 does not reproduce every 2025.1 number).

## 7. Existing system and reuse

- **Current implementation:** `RFSampIF` is a transport-only, block-rate edge with a metronome, a producer-side buffer, and the underrun/overrun counters that form its contract; it deliberately has no signal processing. `RfDataSource` plays blocks from an RF bundle loaded in `pre_sim`, reads `n_ch`/`blksize`/`complex_samp`/`samp_rate` off the bound interface, and in XSI becomes `RfFileSource` via `bfm_model()`. `RfDataSink` records blocks and their grid indices. **`RfSampDelay` (PR #202) is the reserved `Channel` block at bulk-delay fidelity**: one block in, one block out per period, a whole-sample `delay_samp` held in a tail buffer, a declared `blk_latency` of zero, cross-edge checks in `pre_sim`, and `assert_ran`. `examples/rf_shot_loopback` wires `Rfdc.tx_rf → RfSampDelay → Rfdc.rx_rf` with one `Rfdc` owning both tiles' `t0`, and recovers the path delay after subtracting `loop_blk_latency`. `Rfdc` binds to the RF edges in `on_rf_bind` and quantizes against `full_scale`; a real word refuses complex samples.
- **Reuse:**
  - `RFSampIFTx` / `RFSampIFRx` and `RFSampIF` exactly as they are.
  - `RtChannel` follows `RfSampDelay`'s shape and conventions: endpoint names `rf_in`/`rf_out`, cross-edge checks in `pre_sim` (with `n_ch` allowed to differ between the edges, since a MIMO channel maps transmit to receive antennas; equal `blksize`, sample rate and `complex_samp`), a declared latency, `assert_ran`. The multipath generalization lives in the new class; `RfSampDelay` is not changed.
  - `RtSignalSource` follows `RfDataSource`: endpoint `rf_ep`, `in_bundle` as its only `DynParam` (Rule 13), `bfm_model()` returning the same `RfFileSource` model over the rendered bundle.
  - `write_rf_bundle` / `read_rf_bundle` for rendering and replay.
  - `examples/rf_shot_loopback`'s wiring and latency accounting (`loop_blk_latency`, `channel_delay`) for the sounder's channel-mode graph.
  - `examples/rf_loopback`'s I/Q pass-through and its XSI flow for the source-mode graph: `RfSampPassThrough`, `XSI_IQ_WORD`, and `rf_dut_build.generate_dut` / its build DAG to produce the RTL (reuse the I/Q gate's RTL rather than re-synthesizing a separate copy). Importing across examples has precedent (`examples/cpu_sched` imports `examples.regmap`).
  - `RfDataSource` / `RfDataSink` and `StreamDriver` / `StreamSink` (`waveflow/simulation/stream_tb.py`) in tests and the example.
  - Sionna's own `Paths.taps()` as the reference for `rfenv.taps()` (AC5). The runtime application stays NumPy so cached mode needs no Sionna.
  - The `gem5` marker pattern in `tests/conftest.py` (`_GATES`) for the `sionna` marker.
  - The build-step pattern (`waveflow/build/build.py`, `BuildStep`, `BuildDag`, examples' `*_build.py` DAGs) for the trace-and-cache step and the example's build.
- **Conventions to follow:** dataclass `HwModule`s with endpoints added in `__post_init__`; structural facts read off the bound interface, never restated; `DynParam` only for run-time paths with a `root` anchor; module docstrings that explain the *why*; numbers with their tool version; black (default line length 88) and ruff.
- **Docs to keep in sync:** `docs/guide/rf/index.md` (link the new page); `docs/guide/rf/rfdc/sampling.md` (one sentence pointing to the new channel block where it says multipath belongs in a channel block); `docs/examples/index.md` is generated from front matter (no edit needed); `tests/mcp/test_knowledge_corpus.py` (`TOC_EXAMPLES` gains `rt_sounder`); `CLAUDE.md` subsystem list and its curated-example count.

## 8. Tools and access (readiness)

Checked 2026-10-12. Re-run before starting:

```bash
# guided-task plugin's readiness script, run via marketplace_env (see its SKILL.md):
export MARKETPLACE_ROOT=/home/wirelesslab914/ali/claude-marketplace
. "$MARKETPLACE_ROOT/common/scripts/skill_env.sh"
READY="$MARKETPLACE_ROOT/common/plugins/guided-task/skills/guided-task/scripts/check_readiness.py"
"$PY" "$READY" --tool git --path .venv/bin/python --path /lib/x86_64-linux-gnu/libLLVM.so.20.1 \
  --path /tools/Xilinx/Vitis/2024.1/bin/vitis-run --path /tools/Xilinx/Vivado/2024.1/bin/vivado \
  --path waveflow/hw/rf_sample_if.py --path waveflow/simulation/rf_tb.py \
  --path examples/rf_loopback/rf_loopback.py --writable plans/sionna_rt --writable waveflow/rfenv \
  --writable examples/rt_sounder --url https://pypi.org/simple/sionna-rt/ \
  --url https://nvlabs.github.io/sionna/rt/index.html --url https://github.com/NVlabs/sionna-rt --git .
# Dependency resolution (read-only):
source .venv/bin/activate && pip install --dry-run sionna-rt
# No credentials are needed, so there are no credential probes.
```

| Check | Target | Status | Detail |
|---|---|---|---|
| tool | `git` | ✅ ready | git version 2.43.0 (/usr/bin/git) |
| path | `.venv/bin/python` | ✅ ready | file, readable |
| path | `/lib/x86_64-linux-gnu/libLLVM.so.20.1` | ✅ ready | file, readable |
| path | `/tools/Xilinx/Vitis/2024.1/bin/vitis-run` | ✅ ready | file, readable |
| path | `/tools/Xilinx/Vivado/2024.1/bin/vivado` | ✅ ready | file, readable |
| path | `waveflow/hw/rf_sample_if.py` | ✅ ready | file, readable |
| path | `waveflow/simulation/rf_tb.py` | ✅ ready | file, readable |
| path | `examples/rf_loopback/rf_loopback.py` | ✅ ready | file, readable |
| writable | `plans/sionna_rt` | ✅ ready | does not exist yet — will be created (parent is writable) |
| writable | `waveflow/rfenv` | ✅ ready | does not exist yet — will be created (parent is writable) |
| writable | `examples/rt_sounder` | ✅ ready | does not exist yet — will be created (parent is writable) |
| url | `https://pypi.org/simple/sionna-rt/` | ✅ ready | HTTP 200 |
| url | `https://nvlabs.github.io/sionna/rt/index.html` | ✅ ready | HTTP 200 |
| url | `https://github.com/NVlabs/sionna-rt` | ✅ ready | HTTP 200 |
| git | `.` | ✅ ready | branch `feat/sionna-prod`, working tree clean |

**Readiness: READY** — 15 ready · 0 to check · 0 blocked

Dependency dry run (2026-10-12): would install `sionna-rt 2.2.0`, `drjit 1.5.0`, `mitsuba 3.9.1`, `importlib_resources 7.1.0`, `ipywidgets 8.1.9`, `jupyterlab_widgets 3.0.17`, `widgetsnbextension 4.0.16`, `pythreejs 2.4.2`, `ipydatawidgets 4.3.5`, `traittypes 0.2.3`; no conflicts.

| Capability | Kind | Purpose | Available? |
|---|---|---|---|
| Web access to the Sionna, Mitsuba and Dr.Jit docs | tool (WebFetch) | Authoritative API reference (A1) | ✅ |
| Reviewer agent | agent (`general-purpose` subagent, read-only) | Independent review at each milestone | ✅ |
| Vitis HLS / Vivado xsim 2024.1 | local toolchain | RTL of the I/Q pass-through and the example's XSI replay gate (steps 17–18) | ✅ |
| NVIDIA GPU | device | Faster ray tracing | ❌ absent — CPU (LLVM) chosen instead |
| The user | person | Approves 👁 diffs, the design note, milestones, any run over 1 h, tolerance changes | ✅ |

**Gaps the user accepted:** no NVIDIA GPU; ray tracing runs on the CPU through Dr.Jit's LLVM backend (accepted 2026-10-12 with the environment confirmation). Risk in §14.

## 9. Approach

**Chosen approach.** A generator-neutral channel core in NumPy, with Sionna RT
as one backend that fills it, and two thin pysim nodes on `RFSampIF`:

1. **The channel file (`PathSet`).** For every link (receiver antenna × transmitter antenna), the list of paths: complex amplitude `a`, absolute delay `tau`, Doppler `f_D`, plus a JSON manifest (carrier, scene and solver specification, its hash, positions and velocities, tool versions and variant, creation date). This is the smallest representation that loses nothing, applies at any sample rate, and is what Sionna's `Paths` already holds. It is written by the backend with `normalize_delays=False` and read by NumPy alone.
2. **Block-rate application (`ChannelApplier`).** For each output sample, y_r(t) = Σ_t Σ_p a_p·e^(j2π f_D,p t)·x_t(t − τ_p), with each delay realized as a band-limited fractional-delay filter (windowed sinc, length and window fixed in step 3) over a history buffer kept across blocks, and the Doppler phase taken from the absolute sample index. The output is therefore independent of `blksize` (AC3). The filter's look-ahead makes the node non-causal by half its length; step 3 settles whether that is absorbed by a declared fixed delay or by one block of latency, and the node declares it so loop latencies and `assert_clean` account for it. Bulk delay is explicit (`delay_mode`: absolute, or relative to the first path).
3. **The Sionna backend.** A JSON-serializable `SceneSpec` (scene, carrier, devices with positions, orientations and velocities, arrays, solver settings, `deterministic=True`) is traced by `PathSolver` into a `PathSet`. All Sionna imports live in `sionna_backend.py` and happen inside functions, after the Mitsuba variant is set.
4. **Cache.** `load_or_trace(spec, cache_dir)` reuses a channel file whose manifest matches the spec hash; it traces only when needed and Sionna is installed. A build step wraps it for examples.
5. **Nodes.** `RtChannel` (`rf_in` → `rf_out`) and `RtSignalSource` (`rf_ep`) wrap a `ChannelApplier`, with waveform generation, power scaling (`gain_db` onto the converter's `full_scale`) and thermal noise. Cached mode loads a channel file; live mode traces a `SceneSpec` at `pre_sim` (no `PathSet` exists at bind time, so all channel-dependent checks run in `pre_sim`). `RtSignalSource.render_bundle()` writes the RF bundle its XSI twin (`RfFileSource`) replays.

**Search before building.**

- **Sionna's own link-level tools** (the full `sionna` PHY package: `cir_to_time_channel`, `ApplyTimeChannel`, AWGN) do this application, but they pull in a deep-learning framework and would make cached mode depend on it. They are rejected for the runtime; the RT package's own `Paths.taps()` is the reference (AC5).
- **Applying Sionna's `taps()` directly** per block would tie the runtime to Sionna and to one bandwidth and lag window per trace; rejected for the same reason.
- **`RfSampDelay`** is the existing `Channel` block at bulk-delay fidelity. `RtChannel` adopts its shape and conventions and adds multipath, sub-sample delay and Doppler with the overlap-state discipline of `plans/rfsoc_4x2_bringup.md`. Extending `RfSampDelay` itself is rejected because it must not change.
- **`RfDataSource`** already provides XSI replay. `RtSignalSource` renders to its bundle format and reuses its `RfFileSource` BFM rather than adding C++.
- **No MCP server or plugin** offers ray-traced channels. The `dataviz` skill is used for any figure.

Alternatives considered:

| Option | Pros | Cons | Fits the requirements? |
|---|---|---|---|
| **A. NumPy core + Sionna backend + cache (chosen)** | Runs without Sionna; XSI replay for free; block-size independent; other generators can feed it | Own fractional-delay code to verify (AC3, AC5) | Yes, all musts |
| B. Call Sionna at run time for everything | Least code; Sionna's own filters | Every user and test needs Sionna; slow on CPU; no XSI replay; breaks "must not need Sionna" | No |
| C. Precompute only sampled taps (`Paths.taps()`) per sample rate | Simple runtime (an FIR) | A cache per sample rate and bandwidth; Doppler needs time steps per block; loses the physical paths | Partly |
| D. Put the channel inside `RFSampIF` | No new node | Violates the interface's transport-only rule and its C++ equivalence obligation (`plans/adc_model.md`) | No |

## 10. Steps

Every step ends with a commit on `feat/sionna-prod` that names it (the rules in
*Rules for the doer*). Flags: 👁 the user reviews the diff before it is
committed · 💲 cost (Vitis/Vivado CPU time; anything expected over 1 h needs
approval at the time). No step is irreversible (§12).

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 1 👁 | **Extra, marker, lessons file.** Add `[project.optional-dependencies] sionna = ["sionna-rt>=2.2,<3"]` and a `sionna` marker to `pyproject.toml`; add `"sionna"` to `_GATES` in `tests/conftest.py` with `WANT_SIONNA_GATES = 0` (raised as gates land) and a comment in the existing style; install the extra into `.venv`; create `plans/sionna_rt/sionna_rt_lessons.md`. | §2, §7 (`gem5` pattern) | `pip show sionna-rt` reports 2.2.x; `python -c "import sionna.rt"` succeeds; `pytest -m sionna --collect-only -q` runs; the fast suite still shows only the 6 baseline failures | the commands named, `pytest -m "not vitis and not xsi and not gem5 and not sionna"` | commit | ☑ |
| 2 | **CPU spike** (scratch script in the session scratchpad, not committed). Answer, with printed evidence: which Mitsuba variant Sionna picks without CUDA, and how to force `llvm_ad_mono_polarized` (`mi.set_variant` before `import sionna.rt`; `DRJIT_LIBLLVM_PATH` if needed); free space in an empty scene; the shapes of `a`, `tau`, `doppler` from `cir(out_type='numpy')` with `synthetic_array` True and False; the units and sign of `doppler` for a moving receiver; whether `PathSolver(deterministic=True)` twice gives identical results; the float32 phase error on a 100 m path at 3.5 GHz; which scene serves AC2 (material, thickness, size of the plate; `simple_reflector` is metallic); wall time per trace. | §5 docs and source | Each question has a recorded answer with the script's output | the spike's output pasted in §15; findings in the lessons file; AC2's scene in §14 | commit (plan + lessons only) | ☑ |
| 3 👁 | **Design note**, recorded in §14: sample units (recommended: complex envelope in √W, so \|x\|² is power in watts; `gain_db` maps to the `Rfdc`'s `full_scale`); the channel-file schema and manifest fields; the `delay_mode` default; sign conventions (Sionna's baseband coefficient and Doppler formula); how `tau` per antenna is stored; the fractional-delay filter's default length and window (sized to meet AC3's −60 dB); **causality and latency** (a declared fixed delay or one block, and the resulting `startup_blocks` for `assert_clean` and the loop latency in AC7); the noise seeding scheme. | step 2 | The user approves the note | the user's reply, recorded in §14 and §15 | commit (plan) | ☑ |
| | **Milestone M1** (steps 1–3): reviewer agent on the diff and the design note; pause for the user. | | | | | |
| 4 | **`PathSet` and the import blocker**: `waveflow/rfenv/__init__.py`, `waveflow/rfenv/paths.py` (dataclass, validation, `save`/`load` to `.npz` + `.json` manifest, `spec_hash`), `tests/rfenv/_block_sionna.py` (a pytest plugin that makes `import sionna`, `mitsuba`, `drjit` raise `ImportError`), `tests/rfenv/test_paths.py`. NumPy only. | step 3 note | Round trip is bit-identical; invalid shapes and missing manifest fields raise with clear messages; the tests pass under the blocker | `pytest tests/rfenv/test_paths.py` and `python -m pytest -p tests.rfenv._block_sionna tests/rfenv/test_paths.py` | commit | ☑ |
| 5 | **`ChannelApplier` and `rfenv.taps()`**: `waveflow/rfenv/apply.py` (stateful block application with the step 3 filter and declared latency; `reference_apply()` that evaluates the whole signal with the same filter; `taps(pathset, bandwidth, l_min, l_max, sampling_frequency, num_time_steps)` implementing Sionna's documented formula with absolute delays), `tests/rfenv/test_apply.py` on synthetic `PathSet`s, including a 2×4 MIMO case. | step 4 | AC3 and AC4 (synthetic) pass | `pytest tests/rfenv/test_apply.py` | commit | ☐ |
| 6 | **Waveforms, power and noise**: `waveflow/rfenv/waveforms.py` (tone, Zadoff-Chu, PN, user array, RF bundle), `waveflow/rfenv/noise.py` (kTB·NF, seeded, block-size invariant), `tests/rfenv/test_waveforms.py`. | step 3 units | ZC has its ideal periodic autocorrelation; noise power within 2% of kTB·NF over 10⁶ samples; noise identical for `blksize` 1 and 1024 | `pytest tests/rfenv/test_waveforms.py` | commit | ☐ |
| | **Milestone M2** (steps 4–6): reviewer agent; pause for the user. | | | | | |
| 7 👁 | **Sionna backend**: `waveflow/rfenv/sionna_backend.py` (`SceneSpec`, `trace(spec) -> PathSet` with `normalize_delays=False`, variant selection before import with the override `WAVEFLOW_SIONNA_VARIANT`, lazy imports, an install hint when Sionna is missing), `tests/rfenv/test_sionna_backend.py` (`sionna` marker; `WANT_SIONNA_GATES` updated), and a count test that `pytest -m sionna --collect-only` collects exactly `WANT_SIONNA_GATES`, like `tests/build/test_xsi_session_gate.py`'s (the sionna test modules import Sionna lazily so they collect without it). | steps 2–4 | A built-in scene traces into a valid `PathSet`; two traces of one spec are identical; under the blocker, `trace()` raises the install hint | `pytest -m sionna -rs tests/rfenv/test_sionna_backend.py` (0 skipped); `python -m pytest -p tests.rfenv._block_sionna tests/rfenv/test_sionna_backend.py -m "not sionna"` | commit | ☐ |
| 8 | **Reference checks** against closed forms and Sionna: `tests/rfenv/test_sionna_reference.py` (free space, single reflector, Doppler, taps SISO and 1×4). | step 7 | AC1, AC2, AC4 (traced) and AC5 pass | `pytest -m sionna -rs tests/rfenv/test_sionna_reference.py` (0 skipped) | commit | ☐ |
| 9 | **Cache and build step**: `waveflow/rfenv/cache.py` (`load_or_trace`), a `TraceChannelStep` build step, `tests/rfenv/test_cache.py`. | steps 4, 7 | A matching cache file is reused without tracing; a changed spec retraces (a `sionna`-marked test) | `pytest tests/rfenv/test_cache.py` and `pytest -m sionna -rs tests/rfenv/test_cache.py` | commit | ☐ |
| 10 | **Reference channel files and the no-Sionna check**: trace and commit the small channel files the tests use (`tests/rfenv/data/`); `tests/rfenv/test_no_sionna.py`, which runs the non-`sionna` tests in a subprocess under the blocker. | steps 4–9 | The committed files load under the blocker; AC9's run passes for what exists so far | `pytest tests/rfenv/test_no_sionna.py` | commit | ☐ |
| | **Milestone M3** (steps 7–10): reviewer agent; pause for the user. | | | | | |
| 11 👁 | **`RtChannel`**: `waveflow/rfenv/nodes.py` (`rf_in`, `rf_out`; parameters: channel file or `SceneSpec` and `mode`, filter, `delay_mode`, `gain_db`, noise figure and seed; the declared latency; `pre_sim` checks: both edges complex, equal sample rate and `blksize`, `rf_in.n_ch` = transmit antennas and `rf_out.n_ch` = receive antennas; `assert_ran`), `tests/rfenv/test_nodes.py`. | steps 5, 6, 10 | Graph `RfDataSource → RFSampIF → RtChannel → RFSampIF → RfDataSink`: output equals `reference_apply` on the whole signal shifted by the declared latency (relative error ≤ 1e-12, noise off); counters clean after the declared startup; 2×4 case; mismatched `n_ch` or `blksize` raises in `pre_sim` | `pytest tests/rfenv/test_nodes.py -k channel` | commit | ☐ |
| 12 👁 | **`RtSignalSource`**: in `nodes.py` (`rf_ep`; waveform, channel, power, noise as plain fields; `in_bundle` the only `DynParam`; `render_bundle()`; `bfm_model()` returning the `RfFileSource` model like `RfDataSource`), tests in `tests/rfenv/test_nodes.py`. | step 11 | The pysim output equals the rendered bundle; feeding an `Rfdc` ADC with the I/Q word for ≥ 64 blocks leaves the counters clean (AC6, pysim part) | `pytest tests/rfenv/test_nodes.py -k source` | commit | ☐ |
| 13 | **Live mode**: both nodes accept `mode="live"` with a `SceneSpec` and trace at `pre_sim`; `tests/rfenv/test_live.py` (`sionna` marker), plus a blocker test that live mode raises the install hint. | steps 7, 11, 12 | AC8 passes; the install hint is raised under the blocker | `pytest -m sionna -rs tests/rfenv/test_live.py`; `pytest tests/rfenv/test_no_sionna.py` | commit | ☐ |
| | **Milestone M4** (steps 11–13): reviewer agent; pause for the user. | | | | | |
| 14 | **Example scene and channel file**: `examples/rt_sounder/` with its `SceneSpec` (scene and positions chosen so that several taps above −30 dB are separated by at least one sample at the default 3.5 GHz and 122.88 MSPS), the traced channel file committed in `examples/rt_sounder/data/`, and `rt_sounder_build.py` with the trace step. | steps 9, 10 | The committed file loads under the blocker; its taps at integer lags show the intended structure (printed) | `python -m examples.rt_sounder.rt_sounder_build --list-steps`; a load test in `tests/examples/test_rt_sounder.py` | commit | ☐ |
| 15 | **Sounder, channel mode (pysim)**: a `StreamDriver` plays Zadoff-Chu words into `Rfdc.tx_streams[0]` (I/Q word) → DAC → `RFSampIF` → `RtChannel` → `RFSampIF` → ADC → `StreamSink`, wired like `rf_shot_loopback` (one `Rfdc` owning both `t0`s); the loop latency declared; offline circular correlation and the tap comparison. | step 14 | AC7 passes | `pytest tests/examples/test_rt_sounder.py -k sounder` | commit | ☐ |
| 16 | **Sounder, source mode (pysim)**: `RtSignalSource → RFSampIF → Rfdc ADC → RfSampPassThrough → Rfdc DAC → RFSampIF → RfDataSink` with `XSI_IQ_WORD`; the rendered bundle written for XSI. | steps 12, 14 | The pysim run is clean; the bundle is written and reads back equal to the source's blocks | `pytest tests/examples/test_rt_sounder.py -k source` | commit | ☐ |
| 17 💲 | **RTL for the source-mode graph**: produce the I/Q pass-through's RTL through `examples/rf_loopback/rf_dut_build.py`'s DAG (`generate_dut` with the I/Q word), reusing the RTL the loopback's I/Q gate uses; record the csynth time (Vitis HLS 2024.1). | step 16 | The RTL exists and its stamp matches the current generators | `pytest -m vitis -rs` on the relevant build test, or the DAG's own check | commit (no generated files) | ☐ |
| 18 👁 | **XSI replay gate**: `tests/examples/test_rt_sounder_xsi.py` (`xsi` marker): `RfFileSource` replays the rendered bundle through the RTL; per-block comparison with the startup transient and RTL drops asserted against stated expectations; raise `WANT_XSI_GATES` in `tests/conftest.py` by the number of new gates. | step 17 | AC6 passes; `pytest -m xsi --collect-only -q` counts the new floor | `pytest -m xsi -rs tests/examples/test_rt_sounder_xsi.py` (0 skipped) | commit | ☐ |
| | **Milestone M5** (steps 14–18): reviewer agent; pause for the user. Decide here whether the cross-example import of `RfSampPassThrough` stays. | | | | | |
| 19 👁 | **Guide page**: `docs/guide/rf/rfenv/index.md` (`parent: RF converters`, `snippets: run`): concepts, the channel file, cached and live modes, units, the two nodes, wiring to an `Rfdc` (the I/Q word), a short runnable snippet, verification evidence, limits (static scenes, single precision, what Doppler-only leaves out); link from `docs/guide/rf/index.md`; one sentence in `docs/guide/rf/rfdc/sampling.md`; documented-number entries. | steps 1–18 | The page renders, its snippet runs, its numbers are tied to sources | `pytest tests/docs` | commit | ☐ |
| 20 👁 | **Example page and registry**: `docs/examples/rt_sounder/index.md` (front matter: `title`, `parent: Examples`, `nav_order`, `example_dir: examples/rt_sounder`, `summary`); documented-number entries; `rt_sounder` in `TOC_EXAMPLES` (`tests/mcp/test_knowledge_corpus.py`); the `CLAUDE.md` bullet and the curated-example count (twenty → twenty-one). | step 19 | AC11 passes | `pytest tests/docs tests/mcp/test_knowledge_corpus.py` | commit | ☐ |
| 21 | **Regression and close-out**: §11's regression check; review every AC with evidence. | all | AC9 and AC10 pass; every AC has evidence for §16 | §11 commands | commit | ☐ |
| | **Milestone M6** (steps 19–21): reviewer agent on the whole branch; pause for the user. | | | | | |
| 22 | **Completion report** (§16) and the final lessons entry; status set to *Done*, *Partially done* or *Blocked*. | all | §16 filled in | the user reads it | commit | ☐ |

Milestones: M1 = steps 1–3 (environment, spike, design note) · M2 = 4–6 (NumPy core) · M3 = 7–10 (Sionna backend, references, cache) · M4 = 11–13 (nodes and live mode) · M5 = 14–18 (example and XSI) · M6 = 19–21 (docs and regression), then 22.

## 11. Verification strategy

- **Per step:** the step's *Verify with*. New code gets new tests in `tests/rfenv/` written with it. Physical correctness is checked against closed forms (Friis, image method, a perfect conductor or ITU-R P.2040, Doppler) and against Sionna's own `Paths.taps()`; signal processing against a direct whole-signal reference (`reference_apply`); end to end by the channel sounder recovering the traced taps; XSI by per-block equality with pysim.
- **Regression check (step 21):**
  - Fast suite: `pytest -m "not vitis and not xsi and not gem5 and not sionna" -rs`. Baseline (2026-10-11, `feat/cpu-model` after the merge): 4,789 passed, 6 failed, 47 skipped, 8 xfailed, 1 xpassed. The 6 baseline failures, all environment- or data-related: `tests/docs/test_documented_numbers.py::test_cpu_replay_figures_match_the_recorded_replay`; `tests/examples/test_markov_figures.py::test_every_manifest_figure_is_committed_and_recorded` and `::test_the_committed_figure_is_what_the_build_renders`; `tests/examples/test_vitis_fft.py::test_generate_writes_the_build_tree`; `tests/mcp/test_retrieval_eval.py::test_generated_code_is_excluded_by_default`; `tests/mcp/test_usage_index.py::test_generated_files_are_excluded_from_usage_by_default`. No other failure is acceptable.
  - Without Sionna: `python -m pytest -p tests.rfenv._block_sionna -m "not sionna" tests/rfenv/ tests/examples/test_rt_sounder.py`.
  - `pytest -m sionna -rs`: 0 skipped, at least `WANT_SIONNA_GATES` collected.
  - `pytest -m xsi -rs tests/examples/test_rt_sounder_xsi.py` and the step 17 build check: 0 skipped.
  - `pytest tests/docs tests/mcp`.
  - Lint: `ruff check waveflow/rfenv tests/rfenv examples/rt_sounder` and `black --check` on the same; every changed existing file has no more ruff findings than on `main`.
  - By hand: the `rf_loopback` and `rf_shot_loopback` tests still pass (`pytest -m "not vitis and not xsi" tests/examples -k "rf_loopback or rf_shot_loopback"`), since their pieces are reused.
- **Independent review:** a fresh read-only `general-purpose` reviewer agent at each milestone (M1–M6), given this plan, the milestone's diff and the acceptance criteria; it reports blocking issues, wrong numbers and weak tests. Findings and their dispositions go in §14.

## 12. Safety

- **Version control:** branch `feat/sionna-prod`; a commit per verified step; never pushed or merged unless the user asks.
- **Diff review by the user:** anything under `waveflow/`, `docs/guide/`, `tests/conftest.py`, `pyproject.toml`, `CLAUDE.md` (the 👁 steps), and dependency changes.
- **Irreversible or destructive actions:** none expected. Installing the extra into `.venv` is reversible (`pip uninstall` the packages listed in §8). Generated build outputs stay git-ignored.

  | Action | Step | Backup / recovery | Dry run | Approval |
  |---|---|---|---|---|
  | Install `sionna-rt` and its dependencies into `.venv` | 1 | `pip uninstall` the packages listed in §8; the dry run shows no conflicts | done 2026-10-12 | the user approves step 1's diff |
  | csynth of the I/Q pass-through (CPU time) | 17 | git-ignored outputs; delete to undo | — | at the time, if expected over 1 h |

- **Permissions:** write only inside the repo and its `.venv`; read the web for documentation only; no network writes; no credentials.
- **Secrets:** none.
- **Deviation policy:** at most two tested hypotheses, then stop and report. If the plan turns out wrong (for example, the CPU backend cannot trace a built-in scene in reasonable time), stop, propose the change, and update §10 and §14 only after the user approves.

## 13. Agents and models

| Role | Agent / model | Responsibility |
|---|---|---|
| Doer | the Claude Code session executing this plan | steps 1–22 |
| Reviewer | a fresh `general-purpose` subagent, read-only, at each milestone | check the diff against this plan and §4; confirm or refute numbers; list blocking and should-fix findings |
| User | the user | approve 👁 diffs, the design note, each milestone, long runs, tolerance changes |

## 14. Decisions, assumptions, risks, open questions

| Kind | Item | Resolution / owner |
|---|---|---|
| Decision | Level L3 Complex (scores in the header). | user, 2026-10-12 |
| Decision | One channel core used as a **signal source** and a **channel block**, both on `RFSampIF`. | user, 2026-10-12 |
| Decision | Ray tracing **precomputed and cached** by default, with a **live mode** that traces inside the simulation on request. | user, 2026-10-12 |
| Decision | **Static scenes plus Doppler**; re-tracing along trajectories is a nice-to-have. | user, 2026-10-12 |
| Decision | **Antenna arrays** supported; SISO tests first, a 1×4 case required. | user, 2026-10-12 |
| Decision | New example **`examples/rt_sounder`** (channel sounder), which is also the end-to-end check. | user, 2026-10-12 |
| Decision | Sionna as an **optional extra** with a `sionna` pytest marker (a skip fails a `-m sionna` session, like `gem5`). | user, 2026-10-12 |
| Decision | XSI: the **source** is replayed from its rendered bundle by the existing `RfFileSource`; `RtChannel` stays pysim-only. | user, 2026-10-12 |
| Decision | Plan and lessons in `plans/sionna_rt/`; reviewer agent at each milestone; two hypotheses then stop; a commit per step; pause after each milestone (L3 default). | user, 2026-10-12 |
| Decision | Package name `waveflow/rfenv/`, complex baseband at the edge, default test case 3.5 GHz and 122.88 MSPS, the waveform and noise set of §2, the acceptance criteria of §4, the 1 h CPU approval limit. | user, 2026-10-12 (accepted the proposals) |
| Decision | Approach A of §9 (NumPy core, Sionna backend, cache); B–D rejected for the reasons given. | planning, 2026-10-12 |
| Decision | **Plan review** (independent reviewer agent, 2026-10-12): not ready as first written; 3 blocking findings, 10 should-fix, 11 nits. The user chose: **AC6** judged per surviving block, with the transient and RTL drops asserted (the pass-through drops ADC words at RTL by design); **AC5** relative error ≤ 1e-3 with absolute delays (Sionna computes in single precision, and phases from normalized delays by default); **AC7** compared as taps at integer lags against `rfenv.taps()` after subtracting the declared loop latency, strongest tap at −10 dBFS, noise off (a half-sample path correlates about 3.9 dB low at the nearest lag, and paths within a sample add coherently). The user had every other finding applied: reuse `RfSampDelay` and `rf_shot_loopback`; the I/Q `Rfdc` word; Rule 13 on `DynParam`s; filter causality and latency in step 3; precise conditions on AC1–AC4, AC8, AC9, AC11; steps split (old 9, 13, 14, 16); the import blocker moved to step 4; a cost flag instead of ⚠️; the guide's parent page; the curated-example count; black's default line length; reuse of the I/Q gate's RTL; the limits of a static scene with Doppler. | user, 2026-10-12 |
| Assumption | Sionna RT 2.2.0 runs on this machine's CPU through Dr.Jit's LLVM backend with the installed LLVM 17–20. Step 2 confirms or refutes it before any code depends on it. | accepted 2026-10-12 |
| Assumption | Sionna's per-path `doppler` follows its documented first-order formula, and `cir()`/`taps()` follow the documented formulas with absolute delays. AC4 and AC5 test this. | accepted 2026-10-12 |
| Risk | **CPU ray tracing too slow** for large scenes (for example `munich`). Mitigation: tests use small scenes and committed channel files; any run over 1 h needs approval; the example's channel file is committed. | doer |
| Risk | **API drift**: 2.x is newer than the doer's training data. Mitigation: Rule 2 (docs and source first), the step 2 spike, and the `<3` version bound. | doer |
| Risk | **Non-determinism** of `PathSolver`. Mitigation: `deterministic=True` everywhere, checked in step 7. | doer |
| Risk | **Single precision** in Sionna limits agreement with NumPy (AC5 at 1e-3) and between installs (AC8 holds only on one install). Mitigation: absolute delays; channel files record the versions and variant; the cache retraces on a version change when Sionna is present. | doer |
| Risk | **Fractional-delay accuracy versus cost**: a long windowed sinc is accurate but slow per block. Mitigation: the filter is a parameter; AC3's −60 dB bound sets the default; the speed is measured and reported (not gated). | doer |
| Risk | **RTL drops in the pass-through** make the source-mode XSI capture lossy. Mitigation: AC6 asserts the drops; if they hide a mismatch, the `rf_blk_delay` pattern (never drops) is the fallback, with the user's approval. | doer |
| Risk | **Cross-example import** of `RfSampPassThrough` may be judged against the repo's convention. Mitigation: decided at M5; the alternative is moving it into the framework as a separate approved change. | user decides at M5 |
| Decision | **Design note (step 3)**, approved by the user. (1) **Units:** samples are the complex envelope in √W (\|x\|² = power in W at the antenna port); `RtSignalSource` scales a unit-power waveform by √P_tx (`tx_power_dbm`); `RtChannel` treats its input as √W at the transmit antenna; both take `gain_db` for the receive chain, applied after noise, to map onto the `Rfdc`'s `full_scale`; a helper gives the `gain_db` that puts the strongest tap at a target dBFS (AC7: −10 dBFS). (2) **`PathSet`:** float64/complex128 arrays normalized to the full per-antenna layout `[n_rx_ch, n_tx_ch, n_paths]`: `a` (Sionna's coefficient, carrier phase included), `tau` (absolute, s), `doppler` (Hz), `valid`; with the synthetic array, `tau` and `doppler` are broadcast across antennas and the per-antenna phase stays in `a`. A channel is one (device, antenna) pair, so several Sionna receivers (or transmitters) flatten into one edge's channels; the manifest's channel map names each. JSON manifest: schema version, carrier, channel map, scene specification and its SHA-256, solver settings, positions and velocities, generator and versions (sionna-rt, Mitsuba, Dr.Jit, variant), creation date, "absolute delays, normalize_delays=False". Files `<name>.npz` + `<name>.json`. (3) **`delay_mode`:** default `absolute`; `relative` subtracts the earliest path's delay from the envelope delays only (coefficients keep their phase). (4) **Convention:** y_r(n) = Σ_t Σ_p a_p·e^(j2π f_D,p·n/f_s)·x_t(n/f_s − τ_p), Sionna's `a` unchanged, time zero at the run's first sample (matches `cir()` time steps). (5) **Filter:** Kaiser-windowed sinc, `filt_len = 64`, β = 6 (Kaiser estimate for 60 dB to 0.4·f_s: about 36 taps); AC3's test confirms it, and only the default length may change, not the tolerance. (6) **Causality and latency:** each node adds a declared fixed delay of `filt_len/2` samples to every path, which makes it causal; one block in, one block out (`blk_latency = 0`, like `RfSampDelay`), so `assert_clean`'s startup is unaffected; `latency_samp` (group delay, plus the bulk delay in absolute mode) is what loop latencies and AC7 subtract; the source uses the same filter, so its output equals `RtChannel` on the same waveform. (7) **Noise:** complex Gaussian, power kTB·NF with B = the sample rate, generated in fixed 4,096-sample chunks keyed by absolute sample index (`SeedSequence(seed, spawn_key=(chunk,))`), independent of `blksize`. (8) **Live mode:** traces at `pre_sim` into an in-memory `PathSet` of the same schema, then the cached path. | user, 2026-10-12 (step 3) |
| Decision | **Design note amendments (M1 review)**, approved by the user. (A1) **Latency:** D = `filt_len`/2 is the declared group delay (`group_delay_samp`); the node's output is y_node(n) = y_ref(n − D), with y_ref the convention of (4), Doppler included, so the Doppler phase is taken at n − D. The **bulk delay** is the earliest valid path delay and is used only by `delay_mode = relative`. `latency_samp` = D in both modes; AC7 subtracts the loop latency (converter hop, `blk_latency`) plus D and never the bulk delay, because its reference taps use absolute delays. (A2) **Invalid paths:** Sionna pads unused paths with τ = −1 and a = 0; they are masked out of the earliest-delay (relative mode), the maximum delay (buffer sizing) and the τ ≥ 0 causality check. Relative mode takes the earliest valid path **across the whole edge**, not per device pair, so flattened receivers keep their delay differences. (A3) **Sample index:** sample n = (`blk.idx` − 1)·`blksize` + j, from the edge's 1-based absolute block index (`RfBlock.idx`); the applier, the Doppler phase and the noise all key on it. A dropped input block (a gap in `idx`) is zero-filled in the history and counted, so the grid, the Doppler phase and the noise stay aligned. (A4) **Units:** `RtChannel` gains `tx_gain_db`, which maps its input's amplitude (for example a DAC at `full_scale`) to √W at the transmit antenna (default 0 dB); `gain_db` and `tx_gain_db` scale amplitude by 10^(dB/20); dBFS for complex samples is 10·log10(\|x\|²/`full_scale`²), with I and Q each quantized against ±`full_scale` by the `Rfdc`. (A5) **Noise keying:** `SeedSequence(seed, spawn_key=(STREAM_TAG, ch, chunk))` with a per-node stream tag and the current chunk cached (the first keying collided with `SeedSequence(seed).spawn()` and had no stream per channel). (A6) **Smaller points:** the history buffer holds ceil(max valid τ·f_s) + `filt_len` samples; the manifest records the variant, and a variant set before `import sionna.rt` is kept (CUDA preferred when present), so a file traced on a GPU host differs in variant; step 7 adds a `WANT_SIONNA_GATES` count test like `tests/build/test_xsi_session_gate.py`'s; the guide notes that running a Sionna-only test file without `-m` and without Sionna fails the session, as XSI gate files do. | user, 2026-10-12 (M1 review) |
| Decision | **Review of new framework files in M2** (steps 4–6): new files under `waveflow/rfenv/` are committed per step once their tests pass and reviewed by the user at the milestone, with the reviewer agent's findings; changes to existing framework files still wait for the user's approval before commit (Rule 10). | user, 2026-10-12 |
| Decision | **M1 review: dispositions** (independent reviewer agent, 2026-10-12: 0 blocking, 6 should-fix, 7 nits). **Fixed:** the reflector numbers (measured against free space; §15 step 2 and the lessons corrected); the spike's script and output, and the P.2040 comparison, saved in `plans/sionna_rt/spike/` as the evidence §15 cites; the design note's open points (amendments A1–A6); step 7 gains the count test. **Recorded:** the fast suite after step 1 collected 4,853 tests against the baseline's 4,851; the extra 2 are not explained (most likely tests parametrized over build outputs that the 2026-10-11 Vitis run created), and no failure came with them. | user, 2026-10-12 (M1) |
| Decision | **AC2's scene** (step 2): `floor_wall`'s concrete floor (ITU concrete, ε′ = 5.24, σ = 0.123 S/m at 3.5 GHz, 0.1 m thick), Tx (−10, 0, 10) and Rx (10, 0, 10), vertical polarization = TM in the plane of incidence, 45° incidence; reference: the ITU-R P.2040 slab formula, with \|R\|² measured against free space on the unfolded path, traced with `max_depth=1` (spike: Sionna −12.448 dB, formula −12.4485 dB). A second check with `simple_reflector` (1 m metal plate, σ = 1e7 S/m): Sionna −0.002 dB, P.2040 −0.0024 dB. Sionna computes slab reflection with P.2040-4's own equations (`sionna/rt/utils/electromagnetics.py`), so AC2 checks the pipeline (geometry, polarization, path-loss reference), not the formula independently. AC2's 0.5 dB bound is unchanged. | doer, 2026-10-12 (step 2 evidence) |

## 15. Progress log

| Date | Step | What happened | Evidence | Deviation |
|---|---|---|---|---|
| 2026-10-12 | plan | Plan written through the guided-task interview (L3; 3 rounds); readiness READY 15/15 | §8 | — |
| 2026-10-12 | plan review | Independent reviewer agent: 3 blocking, 10 should-fix, 11 nits; the user revised AC5, AC6, AC7 and had every other finding applied; the plan was revised (22 steps) | §14 *Plan review* | — |
| 2026-10-12 | plan | The user approved the revised plan; status Ready | the user's reply | — |
| 2026-10-12 | 1 | `pyproject.toml`: extra `sionna = ["sionna-rt>=2.2,<3"]` and the `sionna` marker; `tests/conftest.py`: `WANT_SIONNA_GATES = 0` and `"sionna"` in `_GATES`; `pip install -e ".[sionna]"` installed sionna-rt 2.2.0, Mitsuba 3.9.1, Dr.Jit 1.5.0 (numpy stays 2.5.3); `import sionna.rt` takes 1.1 s and selects `llvm_ad_mono_polarized` by itself; lessons file created. Fast suite `pytest -m "not vitis and not xsi and not gem5 and not sionna" -rs`: 4,813 passed, 5 failed, 26 skipped, 8 xfailed, 1 xpassed (7 min 28 s). The 5 failures are among the 6 baseline ones; the sixth (`test_usage_index.py::test_generated_files_are_excluded_from_usage_by_default`) and 21 skips now pass because the full Vitis/XSI runs of 2026-10-11 left csynth reports and untracked `gen/` files behind (the skip reasons differ only in `csynth report dir` / `not built here`), not because of this step. `tests/build/test_xsi_session_gate.py`: 20 passed. `pytest -m sionna -rs`: 0 selected (no gates yet). `tests/conftest.py` keeps its one ruff finding of `main` (FURB188) | the commands named | — |
| 2026-10-12 | 2 | CPU spike (scratch script, sionna-rt 2.2.0, Mitsuba 3.9.1, Dr.Jit 1.5.0, `llvm_ad_mono_polarized`). **Variant:** selected automatically without CUDA; nothing to configure. **Free space** (empty scene, iso V, 3.5 GHz, 100 m): 1 path; τ = 3.335641e-07 s = d/c (Δ 0 ps); |a|² = −83.32915 dB vs Friis −83.32914 dB (Δ 1.0e-6 dB); phase of a vs e^(−j2πfd/c): 3.6e-4 rad (float32 at 7,335 rad). **Shapes:** `cir(out_type='numpy')` gives complex64 `a` [rx, rx_ant, tx, tx_ant, paths, time] and float32 `tau`; synthetic array: τ, doppler [rx, tx, paths]; non-synthetic: [rx, rx_ant, tx, tx_ant, paths]. **normalize_delays=True** gives τ = 0 and phase 0 for the LoS path. **Determinism:** two `deterministic=True` traces identical. **Doppler:** receiver approaching at 30 m/s: +350.24234 Hz (v/λ = 350.24230 Hz); `cir(num_time_steps=4, sampling_frequency=1 kHz)` phase step 2.2006378 rad per ms (expected 2.2006373). **Reflector scenes** (Tx (−10,0,10), Rx (10,0,10), max_depth 1): `simple_reflector` = 1 m metal plate (ITU metal, 0.01 m): LoS 66.7128 ns −69.350 dB, reflection 94.3462 ns −72.362 dB; `floor_wall` = concrete floor (ε′ 5.24, σ 0.123 S/m, 0.1 m) + brick wall: reflection −84.808 dB. Against free space on the unfolded 28.284 m path (−72.360 dB): metal \|R\|² = −0.002 dB (P.2040: −0.0024 dB); concrete \|R\|² = −12.448 dB (P.2040 slab, TM at 45°: −12.4485 dB; a single Fresnel interface: −11.619 dB). *(Corrected at the M1 review: first recorded as 0.000 and −12.446 dB, measured against the metal reflection instead of free space.)* **Timing:** 0.08–0.82 s per trace for small scenes; `simple_street_canyon` max_depth 3: 1.25 s first, 0.14 s next (5 paths) | `plans/sionna_rt/spike/` (`rt_spike.py`, `rt_spike.out`, `p2040_slab.py`, `p2040_slab.out`); lessons file | — |
| 2026-10-12 | 3 | Design note proposed from the spike and approved by the user; recorded in §14 (*Design note (step 3)*) | the user's reply | — |
| 2026-10-12 | M1 review | Independent reviewer agent: accept with corrections, 0 blocking, 6 should-fix, 7 nits. Verified the step 1 gate behaviour with fake items (xsi, gem5 unchanged; a plain run is not a sionna session), the spike numbers against `rt_spike.out`, Doppler signs, `cir` time-step phase reference, 1×4 taps vs Sionna to 2e-7, P.2040 TM = V, the filter (64 taps, β 6: −67 dB worst tone, −76 dB flat band), causality for every `blksize`. The user approved every fix and amendment; recorded in §14 | §14 *M1 review*; `plans/sionna_rt/spike/p2040_slab.out` | — |
| 2026-10-12 | 4 | `waveflow/rfenv/__init__.py`, `waveflow/rfenv/paths.py` (`PathSet`: canonical read-only arrays `[n_rx_ch, n_tx_ch, n_paths]`, validation of valid paths only, `min_delay`/`max_delay`/`power_gain` over valid paths, `save`/`load` as `.npz` + `.json`, `spec_hash`, `channel_map`, `REQUIRED_KEYS`, schema version 1); `tests/rfenv/_block_sionna.py` (the import-blocking plugin), `tests/rfenv/_pathsets.py` (synthetic channel files), `tests/rfenv/test_paths.py`. `pytest tests/rfenv/test_paths.py`: 20 passed; under the blocker (`python -m pytest -p tests.rfenv._block_sionna tests/rfenv/test_paths.py`): 20 passed; a subprocess test checks `import sionna.rt` fails under the blocker while `waveflow.rfenv` works. ruff and black clean. The user chose to review the new `waveflow/rfenv/` files of steps 4–6 at M2, not before each commit (changes to existing framework files still wait for approval) | the commands named | — |

## 16. Completion report

*Filled in when the work ends.*

- **Changed:**
- **Tested (commands and results):**
- **Acceptance criteria:** AC1 … AC11 ✅/❌/⚠️ with evidence
- **Decisions made:**
- **Assumptions and not verified:**
- **Remaining risks and issues:**
- **Next steps:**
- **Reusable artifacts saved / tools that would have helped:**

## Checklist coverage

| Group | Addressed (where) | Not applicable (why) |
|---|---|---|
| T: task definition, planning | T1 §1–§4; T2 each step's *Verify with*; T3 header paths, §15, Rules 3 and 5; T4 §10 steps and milestones; T5 §9 alternatives, §14 | — |
| C: context and environment | C1 §5, §8; C2 §5 (every source read directly, with its access method); C3 §6; C5 §7; C6 §7 reuse and conventions | C4: the repo is familiar and well documented (`CLAUDE.md`, `docs/guide/`); no overview step needed |
| V: verification | V1 §4, §10, §11; V2 §11 regression with the recorded baseline; V3 §16, Rule 14; V4 Rule 8, §14; V5 Rule 4, §12 | — |
| D: documentation and continuity | D1 `plans/sionna_rt/sionna_rt_lessons.md`, Rule 3; D2 step 20 (`CLAUDE.md` bullet); D3 §7 docs to sync, steps 19–20; D4 §16; D5 §16 reusable artifacts (the `rfenv` package, the channel files, the sounder) | — |
| R: version control and safety | R1 §12, Rule 9; R2 §12 review areas, 👁 steps; R3 §12 table | — |
| S: security and permissions | S2 §12 permissions | S1: no credentials are needed |
| A: authoritative sources | A1 §5 (Sionna RT 2.2.0 docs and source, Mitsuba 3.9.1, Dr.Jit 1.5.0, ITU-R P.2040), Rule 2 | — |
| P: plugins, MCP, tools | P1 §9 search before building; P2 §8 capabilities (`dataviz` for figures); P4 the doer runs Sionna, pysim and XSI itself (§8, §11); P5 §16 | P3: no MCP server gives ray-traced channels or is needed; no `.mcp.json` change |
| M: multi-agent | M1 §11, §13 | — |
