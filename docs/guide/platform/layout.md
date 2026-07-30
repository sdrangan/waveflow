---
title: Directory layout
parent: Platforms
nav_order: 3
has_children: false
audience: python
api: [PlatformCalib, ModuleStore, platform_fallback_path]
summary: "What is inside a platform directory and where platform directories live. Two content trees under one identity — components/ holds timing residuals keyed by task configuration, modules/ holds resource records keyed by module structure — and three homes: an untracked work tier that sweeps churn freely, a project's own tracked library, and the read-only reference shipped inside the package."
---

# Directory layout

## Inside a platform

Each platform is stored in a directory with structure:

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

One identity and two separate content trees for **components** and **modules** described below.

### The identity manifest

The identity file `platform.json` stores the [identity](./identity.md) in a JSON format.

```json
{ "part": "xc7z020clg484-1", "clk_freq_hz": 100000000.0 }
```

This is the **one** source both synthesis and calibration read, so the part a design is synthesized for
cannot drift from the part its numbers were measured on.   The `platform.json` can optionally also describe the *resource types*
with a `"res_types"` field.  As described in [platform identity page](./identity.md), the default uses the Xilinx/AMD resources:

```json
{ "res_types": ["lut", "ff", "dsp", "bram", "uram", "srl"] }
```

However, future ASIC flows could define other resource types.  For example, we could theoretically add a field such as:

```json
{ "part": "tsmc45", "clk_freq_hz": 1e9, "res_types": ["cell_area", "macros", "regs"] }
```

## Two keys, and why

Timing and resource data are stored in two separate trees:

- **`components/`** — the *timing* residuals, one entry per task **configuration** (a task's function
  name plus its template arguments).
- **`modules/`** — the *resource* records, one entry per structurally distinct **module**.

They key differently because they are answering different questions:

|             | `components/` (timing)                                                                               | `modules/` (resource)                                                |
| ----------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| keyed by    | the task **configuration** — function name + template args, e.g. `mem_r_stream_framed_task_32` | the module's **structure digest**, e.g. `mem_r_stream-04919c18` |
| granularity | one entry per synthesized task variant                                                                 | one entry per structurally distinct module                             |
| readable?   | yes — it is the RTL entity prefix                                                                     | prefix only; the digest is for correctness                             |

What they share is that both are *configuration-precise*: neither will hand a design at
`mem_dwidth=64` a number measured at 32. A single function, `config_id()` in
`waveflow/calib/module_key.py`, is what both the timing resolver and the
[resource attributor](../resource/composite.md) read, so the two cannot drift on what "the same
configuration" means.

{: .note }
> The timing key was **not** always configuration-precise: it used the bare function name, so one
> `mem_r_stream_framed_task` directory served every memory width. Older libraries still load — the
> resolver falls back to the bare name — but a residual found that way reports `config_specific=False`
> in its confidence rather than claiming to match the width being built. The two shipped residuals are
> in exactly that state.

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
> before your project has one :  resolves to PACKAGED,  4 module records visible
> after your project publishes:  resolves to PROJECT,   0 module records visible
>                                shipped bus law reachable: False
> ```
>
> The moment your project owns a platform of a given name, the packaged one of that name stops being
> consulted **at all** — you do not inherit its bus law, its residuals, or its records. And giving your
> platform a *different* name does not help either: that is simply a new, empty platform.

That is the reason a project **seeds** its library from an upstream platform rather than inheriting
from one — see [Seeding](./create.md#seeding-do-not-recalibrate-the-framework).

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
> **Give a sweep its own platform name.** Reusing a shipped platform's name makes resolution find the
> *packaged* directory through its fallbacks and write there — bypassing `publish_calib`, which is
> meant to be the single writer of a tracked library.

## What is worth committing

Everything a platform publishes is small and text. The shipped reference holds a bus law, two timing
residuals, and the framework modules' measured configurations; the `fir_block` project library adds 31
more, together representing about 26 minutes of Vitis C-synthesis in well under 100 KB. The point of
committing either is that the next checkout gets those numbers as a *cache hit* rather than a toolchain
run.

What is deliberately not committed is the raw material: firing trees, solution directories, generated
RTL. All of it is reproducible from a re-sweep, and all of it is large.

## See also

- [Platform identity](./identity.md) — the manifest and the mismatch gate.
- [Managing a platform](./workflow.md) — creating, inspecting and publishing one.
- [Module keys and the record store](../calib/modules.md) — the `modules/` tier's contract.
