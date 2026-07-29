---
title: Directory layout
parent: Platforms
nav_order: 1
has_children: false
audience: python
api: [PlatformCalib, ModuleStore, platform_fallback_path]
summary: "What is inside a platform directory and where platform directories live. Two content trees under one identity — components/ holds timing residuals keyed by task configuration, modules/ holds resource records keyed by module structure — and three homes: an untracked work tier that sweeps churn freely, a project's own tracked library, and the read-only reference shipped inside the package."
---

# Directory layout

## Inside a platform

```text
<platform>/
    platform.json                          identity — part, clk_freq_hz, [res_types]
    mm_bus.json                            the m_axi bus-transfer law (BusCalib)
    points/*.json                          its distilled corpus

    components/<task-config>/              TIMING residuals
        params.json                            the fitted coefficients
        corpus.csv                             the distilled corpus, re-fittable offline
        rtl/  pysim/                           raw per-run firings — churn, never published

    modules/<module-key>/                  RESOURCE records
        module.json                            the identity this key resolves to
        resource/records.jsonl                 the measurements
        timing/records.jsonl                   (same envelope; unused so far)
```

One identity, two content trees. The split is not historical — the two axes key on different things,
because they answer different questions.

## Two keys, and why

| | `components/` (timing) | `modules/` (resource) |
|---|---|---|
| keyed by | the task **configuration** — function name + template args, e.g. `mem_r_stream_framed_task_32` | the module's **structure digest**, e.g. `mem_r_stream-04919c18` |
| granularity | one entry per synthesized task variant | one entry per structurally distinct module |
| readable? | yes — it is the RTL entity prefix | prefix only; the digest is for correctness |

Both are *configuration-precise*: neither will hand a design at `mem_dwidth=64` a number measured at
32. They differ in how far they go — a structure digest distinguishes anything elaboration
distinguishes, including parameters that never reach a C++ template argument, whereas a task
configuration id stops at what Vitis bakes into the entity name.

{: .note }
> The timing key was **not** always configuration-precise. It used the bare function name, so one
> `mem_r_stream_framed_task` directory served every memory width. Libraries written before the change
> still load — the resolver falls back to the bare name — but a residual found that way is reported
> with `config_specific=False` and says so in its confidence, rather than being presented as a match
> for the width actually being built. The two shipped residuals are currently in exactly that state.

The task-configuration id comes from a single function, `config_id()` in
`waveflow/calib/module_key.py`, used by *both* the timing resolver and the
[resource attributor](../resource/composite.md) — so the two cannot drift on what "the same
configuration" means.

## Where platform directories live

Three homes, searched in this order:

```text
1. platforms_root       the build's own — defaults to  calib/platforms/    (your project's library)
2. $WAVEFLOW_PLATFORM_PATH                                                 (CI, a shared checkout)
3. user data dir        ~/.local/share/waveflow/platforms/                 (a board you calibrated)
4. the package          waveflow/calib/platforms/                          (shipped, read-only)
```

Creation always targets `platforms_root`, never a read-only fallback.

{: .warning }
> **First match wins on the *whole directory*. There is no merging across roots.**
>
> This is the single most surprising thing about platform resolution, and it decides how a project
> should set one up. A platform name resolves to exactly **one** directory — the first root that has
> it — so:
>
> ```text
> before your project has one :  resolves to PACKAGED,  35 module records visible
> after your project publishes:  resolves to PROJECT,    0 module records visible
>                                shipped bus law reachable: False
> ```
>
> The moment your project owns a platform of a given name, the packaged one of that name stops being
> consulted **at all** — you do not inherit its bus law, its residuals, or its records. And giving your
> platform a *different* name does not help either: that is simply a new, empty platform.

## So a project seeds, rather than inheriting

Because there is no merge, a project that publishes its own calibration should **copy** the upstream
platform as its starting point:

```bash
waveflow_calib new calib/platforms/myboard --from zynq7020_bfm_100mhz
```

You then own a complete library — inherited bus law and infra residuals, with your own module records
landing beside them as you calibrate. See [Managing a platform](./workflow.md#seeding-from-an-existing-platform).

The trade is deliberate. You duplicate the upstream data (~156 KB), and an upstream improvement does
not reach you until you re-seed. In exchange, what you inherited is a **reviewable commit** and is
**frozen**: calibration is measurement, and an upstream change cannot move your numbers without you
seeing it. Pinning measurement to a commit is worth more here than resolving it dynamically.

## The tracked / untracked split

```text
calib/work/<name>/            UNTRACKED.  Sweeps, tests and DAG steps write here freely.  It churns.
calib/platforms/<name>/       TRACKED.    Your project's library.  publish_calib is the only writer.
waveflow/calib/platforms/     TRACKED.    The reference library, shipped as package data.
```

A sweep writes the work tier and `publish_calib` promotes the **stable** artifacts — identity, fitted
params, distilled corpora, module records — leaving the raw per-run firing trees behind. That is what
keeps a re-publish churn-free: unchanged files are byte-compared and not rewritten, so re-running a
deterministic fit produces no diff at all.

{: .warning }
> **Give a sweep its own platform name.** Reusing a shipped platform's name makes `Platform.resolve`
> find the *packaged* directory through its fallbacks and write there — bypassing `publish_calib`,
> which is meant to be the single writer of a tracked library.

## What is worth committing

Everything a platform publishes is small and text: the reference library is **156 KB** and holds a bus
law, two timing residuals, and 35 measured module configurations representing about 26 minutes of
Vitis C-synthesis. The point of committing it is that the next project — or the next checkout — gets
those numbers as a *cache hit* rather than a toolchain run.

What is deliberately not committed is the raw material: firing trees, solution directories, generated
RTL. All of it is reproducible from a re-sweep, and all of it is large.

## See also

- [Platform identity](./identity.md) — the manifest and the mismatch gate.
- [Managing a platform](./workflow.md) — creating, inspecting and publishing one.
- [Module keys and the record store](../calib/modules.md) — the `modules/` tier's contract.
