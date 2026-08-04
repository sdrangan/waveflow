---
title: Creating a platform
parent: Platforms
nav_order: 2
has_children: false
audience: python
api: [waveflow_calib, SeedPlatformStep, BuildConfig, seed_platform]
summary: "One platform per project, not per module. Where the directory goes (<project>/calib/platforms/<name>, one subdirectory per target); why you seed it from an upstream platform rather than recalibrating the framework components your design composes; how a build names one and creates-or-confirms it against a stored identity; and the .gitignore lines — without the re-include, a project publishing exactly as documented writes into a directory git silently drops."
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

Navigate to your project's top directory — for the worked example, `examples/fir_block` — and run:

```bash
waveflow_calib new calib/platforms/myboard --part xc7z020clg484-1 --clk 100e6
```

The path is relative, so running it from the project root is what puts the platform where
`platforms_root` will look for it.

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

So rather than starting empty, you can **seed** your platform from an existing one. Waveflow ships
pre-built platforms for its own infrastructure components, stored inside the installed package:

```text
<path/to/waveflow>/calib/platforms/
```

### Finding one to seed from

List every platform visible from where you are:

```bash
waveflow_calib list
```

```text
name                         part                       clock   source
zynq7020_bfm_100mhz          xc7z020clg484-1          100 MHz   .../waveflow/calib/platforms
```

It prints each platform's **identity**, not just its name, because a name alone does not tell you
whether its numbers are valid for the part you are building.

**If one matches the target you intend**, seed from it by name:

```bash
waveflow_calib new calib/platforms/myboard --from zynq7020_bfm_100mhz
```

```text
seeded from …/waveflow/calib/platforms/zynq7020_bfm_100mhz  (78 file(s))
platform 'myboard' at calib/platforms/myboard
  part      : xc7z020clg484-1
  clock     : 100.0 MHz
```

{: .note }
> `--from` takes the platform **name**, not a path. It is resolved through the same search order a build
> uses, so you never spell out where the packaged library lives.

`--from` copies exactly what `publish` promotes — identity, bus law, timing residuals, module records —
and none of the raw firing trees. It **inherits the identity**, so `--part` / `--clk` become optional,
and it refuses a non-empty target unless `--force`.

**If nothing matches your target**, that is fine: create the platform with its own `--part` / `--clk`,
and recalibrate the infrastructure components your design actually uses. Those fits land in your own
project platform and are reused from then on — you pay for them once, and only for the components you
compose.

Your own modules are then measured into the same directory, sitting beside whatever you inherited.

### What ships to seed from

Waveflow ships one reference platform, **`zynq7020_bfm_100mhz`** (xc7z020 at 100 MHz, idealized BFM
memory), holding a fitted bus-transfer law, two component timing residuals, and measured area for the
framework's reusable modules. Seeding from it means `MemRStream` / `MemWStream` arrive already measured,
so a design composing them gets area and timing with **no toolchain run**.

It ships **as package data inside `waveflow`**, so a `pip`-installed build resolves it with no checkout
at all. [Managing a platform](./workflow.md#the-reference-platform-end-to-end) covers how it was built.

If your target is a different part or clock the shipped platform is not valid for it
([why](./identity.md#the-target-part-clock-and-memory)) — you calibrate from scratch rather than seeding,
and the mismatch guard below makes that a visible choice rather than a silent one.

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

## Doing it from the build instead of by hand

Everything above is a one-off setup command. For anything beyond a single design you will want the
build to do it, so a fresh checkout works with no manual step — see the [Build System](../build/) for
the DAG these steps live in.

Two pieces. The build **names** its platform:

```python
config = BuildConfig(
    root_dir=HERE,
    platform="myboard",                # the platform name
    part="xc7z020clg484-1",            # this build's target
    clk_freq=100e6,
    platforms_root="calib/platforms",  # the write target, and the first root searched
)
```

and a step **creates it if absent**, seeded, so nothing has to exist beforehand:

```python
from waveflow.build.calib_steps import SeedPlatformStep

dag.add(SeedPlatformStep(name="platform", seed_from="zynq7020_bfm_100mhz"))
```

`SeedPlatformStep` is create-if-absent and **idempotent** — once the platform is there it is a no-op —
so it costs nothing to leave in permanently. Put it first: everything that reads or writes calibration
depends on it.

For a multi-step build this is the preferred route. The platform then flows through
`config.platform_info` to every step that needs it, and to codegen — `set_part` / `create_clock` are
taken from it — so nothing restates the part and the RTL is synthesized for exactly the target its
models are valid for.

{: .note }
> **Naming a platform on a build is create-or-confirm, not just a lookup.** If it does not exist, it is
> created and its manifest seeded from the build's `part` / `clk_freq`. If it does, the build's target is
> **checked against the stored manifest** and a mismatch raises `PlatformMismatchError` — pass
> `allow_platform_mismatch=True` to downgrade it to a warning. Either way the *stored* values win: they
> are what the numbers are valid for. That check is the whole reason a stored measurement cannot be
> quietly used against the wrong device.

{: .note }
> **Why seeding is a build step when [publishing deliberately is not](./workflow.md#why-publish-is-not-a-dag-step).**
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
