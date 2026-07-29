---
title: Creating a platform
parent: Platforms
nav_order: 2
has_children: false
audience: python
api: [waveflow_calib, SeedPlatformStep, Platform, seed_platform]
summary: "One platform per project, not per module. Covers where the directory goes (<project>/calib/platforms/<name>, one subdirectory per target), why you seed it from an upstream platform rather than recalibrating the framework components your design composes, the commands and the build step that do it, and the .gitignore lines — without the re-include, a project publishing exactly as documented writes into a directory git silently drops."
---

# Creating a platform

## One platform per project, not per module

A platform is a **target**, not a component. Every module in your design is measured *against* the same
part at the same clock, so they all share one platform — a design with twenty modules has one platform
directory holding twenty sets of records, not twenty directories.

You need a second platform only when the **target** changes: a different part, a different synthesis
clock, a different memory system. Those are [what identifies one](./identity.md).

## Where it goes

```text
<project>/
    calib/
        platforms/                    <- platforms_root: one subdirectory per TARGET
            zynq7020_bfm_100mhz/          xc7z020 @ 100 MHz
            zynq7020_bfm_200mhz/          the same part at another clock is a DIFFERENT platform
        work/                         <- untracked scratch; sweeps write here
```

`calib/platforms/` is the default `platforms_root`, so a platform there is found with no configuration.

### For example: the block FIR

`examples/fir_block` is a project in exactly this sense, and its layout is the one above:

```text
examples/fir_block/
    fir_block.py                                     the design
    calib/platforms/zynq7020_bfm_100mhz/             its platform — 35 module configurations
    calib/work/zynq7020_fir_sweep/                   untracked; the sweep writes here
```

Four modules (`FirCmdRx`, `MemRStream`, `FirCompute`, `MemWStream`) and **one** platform between them.
Copy that directory and you have the layout a project of your own wants.

## Creating the directory

```bash
waveflow_calib new calib/platforms/myboard --part xc7z020clg484-1 --clk 100e6
```

`--part` and `--clk` are the identity: every number stored here is only valid for that part at that
clock, and the mismatch gate has nothing to compare without them. A non-FPGA technology also declares
its counters:

```bash
waveflow_calib new calib/platforms/asic45 --part tsmc45 --clk 1e9 \
                   --res-types cell_area macros regs
```

{: .note }
> A platform is also created *implicitly* the first time a build selects a name that does not resolve.
> That is convenient, but the explicit command is what you want when setting up deliberately — it is the
> only place to declare `--res-types`, and it fails loudly rather than silently producing an
> identity-less directory.

## Seeding: do not recalibrate the framework

Your design almost certainly composes components you did not write — `MemRStream`, `MemWStream`, and any
other framework module. Those already have measured timing and resource models on the reference platform,
and **recalibrating them means spending your own toolchain time re-deriving numbers that already exist.**

So rather than starting empty, **seed** your platform from an upstream one:

```bash
waveflow_calib new calib/platforms/myboard --from zynq7020_bfm_100mhz
```

```text
seeded from …/waveflow/calib/platforms/zynq7020_bfm_100mhz  (78 file(s))
platform 'myboard' at calib/platforms/myboard
  part      : xc7z020clg484-1
  clock     : 100.0 MHz
```

`--from` resolves through the usual search path and copies exactly what `publish` promotes — identity,
bus law, timing residuals, module records — and none of the raw firing trees. It **inherits the
identity**, so `--part` / `--clk` become optional, and it refuses a non-empty target unless `--force`.

Your own modules are then measured into the same directory, sitting beside the inherited ones.

### What ships to seed from

Waveflow ships one reference platform, **`zynq7020_bfm_100mhz`** (xc7z020 at 100 MHz, idealized BFM
memory), holding a fitted bus-transfer law, two component timing residuals, and measured area for the
framework's reusable modules. Seeding from it means `MemRStream` / `MemWStream` arrive already measured,
so a design composing them gets area and timing with **no toolchain run**.

If your target is a different part or clock the shipped platform is not valid for it
([why](./identity.md#why-part-and-clock-and-memory)) — you calibrate from scratch rather than seeding,
and the mismatch guard makes that a visible choice rather than a silent one.

{: .warning }
> Seeding is not optional bookkeeping. Platform resolution is
> [first-match-wins on the whole directory](./layout.md#where-platform-directories-live): the moment your
> project owns a platform of a given name, the packaged one **stops being consulted at all**. Publishing
> a single record of your own into an *unseeded* platform therefore **costs** you the bus law and every
> framework residual you were relying on.

{: .note }
> Seeding **copies**; it does not link. You duplicate the upstream data, and an upstream improvement does
> not reach you until you re-seed. That is deliberate: what you inherited is a reviewable commit and is
> *frozen*, so an upstream change cannot move your numbers without you seeing it. Calibration is
> measurement, and measurement should be pinned rather than resolved dynamically.

## Seeding from the build

For a project whose DAG should work on a fresh checkout, do the same thing as a build step:

```python
from waveflow.build.calib_steps import SeedPlatformStep

dag.add(SeedPlatformStep(name="platform", seed_from="zynq7020_bfm_100mhz"))
```

with the build selecting the platform:

```python
BuildConfig(root_dir=HERE, platform="myboard",
            part="xc7z020clg484-1", clk_freq=100e6)     # platforms_root defaults to calib/platforms
```

Create-if-absent and **idempotent** — once the platform is there the step is a no-op, so it costs nothing
to leave in permanently. Put it first: everything that reads or writes calibration depends on it.

{: .note }
> **Why seeding is a DAG step when [publishing deliberately is not](./workflow.md#why-publish-is-not-a-dag-step).**
> The direction differs. Publishing writes **upstream**, into shared infra that other projects consume —
> a considered "I am satisfied" act, and never a build side effect. Seeding writes **downstream**, into
> this project's own library. It is ordinary setup, in the same direction as every other calibration
> step, and it touches nothing anyone else depends on.

## What to `.gitignore`

Two rules, and both matter. A blanket `*.json` ignore usually covers build artifacts, so a tracked
library has to be re-included past it:

```gitignore
**/calib/work/                     # the churny work tier — never committed, at any depth
!calib/platforms/**                # ...but a project's library IS committed
```

{: .warning }
> **The re-include is easy to omit and expensive to miss.** Without it, a project publishing exactly as
> documented writes into a directory git silently drops: the publish reports success, the files are on
> disk, and nothing is committed — so the next checkout has no calibration and every lookup misses.
>
> After setting up a library in a new repository, check `git status` actually sees it.

{: .note }
> Note the `**/` on the work-tier rule. A gitignore pattern with a separator in the *middle* is anchored
> to that file's own directory, so a bare `calib/work/` matches only at the repository root — a project
> nested deeper (an example under `examples/<name>/`) would have its work tier tracked, churning the tree
> with corpus files and raw firing trees.

## What belongs in *your* library versus the shipped one

The dividing line is **who wrote the module**, and it is not a judgement call — every record names the
module's defining module in its `module.json`:

| | goes in | because |
|---|---|---|
| `waveflow.*` modules (`MemRStream`, …) | the **shipped** library | reusable by any design on that part; measuring them once benefits everyone |
| your own modules | **your project's** library | specific to your design; nobody else can use them |

A shared library accumulating one project's modules would ship configurations no other user can use, and
would imply those modules are reference infrastructure when they are not. The rule is enforced by a test
rather than by discipline: the shipped library is asserted to hold no module whose `cls_module` falls
outside `waveflow.*`.

That is also why `examples/mem_copy` behaves differently from `examples/fir_block`. `mem_copy` calibrates
`MemRStream` / `MemWStream` — *framework* modules — so it correctly publishes **upstream** into the
shipped library. `fir_block` calibrates its own, so it publishes into its own. Which library an example
publishes into follows from whose modules it measured, not from where the example lives.

## Next

- [Directory layout](./layout.md) — what ends up inside, and the search order across roots.
- [Managing a platform](./workflow.md) — inspecting one, and the work → publish flow.
