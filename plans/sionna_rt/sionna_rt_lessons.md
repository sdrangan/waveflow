# Lessons: the ray-traced RF environment (Sionna RT)

What executing `plans/sionna_rt/sionna_rt.md` taught: surprises, failed approaches, and rules worth
keeping. Newest section last. Every measured number names its tool versions.

## M1: environment and spike (2026-10-12)

- **Sionna RT 2.2.0 picks the CPU variant by itself.** On a machine without CUDA, `import
  sionna.rt` selects `llvm_ad_mono_polarized` with no configuration (sionna-rt 2.2.0, Mitsuba 3.9.1,
  Dr.Jit 1.5.0, LLVM 20 found on the default path). The import takes about 1 s.
- **The extra installs cleanly next to numpy 2.5.3.** `pip install -e ".[sionna]"` adds sionna-rt,
  Mitsuba, Dr.Jit and a few notebook-widget packages; nothing else in `.venv` moves.
