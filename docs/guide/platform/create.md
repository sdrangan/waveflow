---
title: Creating a platform
parent: Platforms
nav_order: 1
has_children: false
audience: python
api: [waveflow_calib, SeedPlatformStep, Platform, seed_platform]
summary: "Setting up a project's own calibration library: create it with waveflow_calib new, seed it from an upstream platform so the framework's infra measurements come with it, wire SeedPlatformStep into the build so a fresh checkout just works, and get the .gitignore right — without the re-include, a project publishing exactly as documented writes into a directory git silently drops. Ends with what belongs in a shared library versus a project's own."
---

# Creating a platform

A project that calibrates anything of its own needs its **own** platform directory. This is how to set
one up, and the one non-obvious step — why you seed it rather than relying on the shipped one.

## 1. Create the directory

```bash
waveflow_calib new calib/platforms/myboard --part xc7z020clg484-1 --clk 100e6
```

`calib/platforms/` is the default `platforms_root`, so a platform there is found with no configuration.
`--part` and `--clk` are the [identity](./identity.md): every number stored here is only valid for that
part at that clock, and the mismatch gate has nothing to compare without them.

A non-FPGA technology declares its own counters:

```bash
waveflow_calib new calib/platforms/asic45 --part tsmc45 --clk 1e9 \
                   --res-types cell_area macros regs
```

{: .note }
> A platform is also created *implicitly* the first time a build selects a name that does not resolve.
> That is convenient, but the explicit command is what you want when setting up deliberately — it is
> the only place to declare `--res-types`, and it fails loudly rather than silently producing an
> identity-less directory.

## 2. Pull in the framework's infra measurements

An empty platform is almost never what you want, and the reason is
[first-match-wins resolution](./layout.md#where-platform-directories-live): once your project owns a
platform of a given name, the packaged one **stops being consulted at all**. Publishing a single record
of your own would otherwise cost you the bus law and the framework's component residuals.

So seed it:

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

### What comes across, and what that buys

Waveflow ships one reference platform, **`zynq7020_bfm_100mhz`** (xc7z020 at 100 MHz), holding a fitted
bus-transfer law, two component timing residuals, and measured area for the framework's own reusable
modules. Seeding from it means `MemRStream` / `MemWStream` arrive already measured, so a design
composing them gets area and timing with **no toolchain run**.

That is the whole point of a shared library: measurements for the parts of a design **you did not
write**. Your own modules are then measured into the same directory as you calibrate, sitting beside
the inherited ones.

If your target is a different part or clock, the shipped platform is not valid for it — see
[Platform identity](./identity.md) — and you calibrate from scratch rather than seeding.

{: .note }
> Seeding **copies**; it does not link. You duplicate the upstream data, and an upstream improvement
> does not reach you until you re-seed. That is deliberate: what you inherited is a reviewable commit
> and is *frozen*, so an upstream change cannot move your numbers without you seeing it. Calibration is
> measurement, and measurement should be pinned rather than resolved dynamically.

## 3. Wire it into the build

For a project whose DAG should work on a fresh checkout, do the same thing from the build:

```python
from waveflow.build.calib_steps import SeedPlatformStep

dag.add(SeedPlatformStep(name="platform", seed_from="zynq7020_bfm_100mhz"))
```

with the build selecting the platform:

```python
BuildConfig(root_dir=HERE, platform="myboard",
            part="xc7z020clg484-1", clk_freq=100e6)     # platforms_root defaults to calib/platforms
```

Create-if-absent and **idempotent** — once the platform is there the step is a no-op, so it costs
nothing to leave in permanently. Put it first: everything that reads or writes calibration depends on
it.

{: .note }
> **Why seeding is a DAG step when [publishing deliberately is not](./workflow.md#why-publish-is-not-a-dag-step).**
> The direction differs. Publishing writes **upstream**, into shared infra that other projects consume —
> a considered "I am satisfied" act, and never a build side effect. Seeding writes **downstream**, into
> this project's own library. It is ordinary setup, in the same direction as every other calibration
> step, and it touches nothing anyone else depends on.

## 4. Get the `.gitignore` right

A blanket `*.json` ignore covers build artifacts, so a tracked library has to be re-included past it:

```gitignore
/calib/work/                       # the churny work tier — never committed
!waveflow/calib/platforms/**       # the shipped reference library IS committed
!calib/platforms/**                # ...and so is a PROJECT's own library
```

{: .warning }
> **The third line is easy to omit and expensive to miss.** Without it, a project publishing exactly as
> documented writes into a directory git silently drops: the publish reports success, the files are on
> disk, and nothing is committed — so the next checkout has no calibration and every lookup misses.
>
> After setting up a library in a new repository, check `git status` actually sees it.

## What belongs where

The dividing line is **who wrote the module**, and it is not a judgement call — every record carries the
module's defining module in its `module.json`:

| | goes in | because |
|---|---|---|
| `waveflow.*` modules (`MemRStream`, …) | the **shipped** library | reusable by any design on that part; measuring them once benefits everyone |
| your own modules | **your project's** library | specific to your design; nobody else can use them |

A shared library that accumulated one project's modules would ship configurations no other user can
use, and would imply those modules are reference infrastructure when they are not.

That rule is enforced by a test, not by discipline: the shipped library is asserted to contain no
module whose `cls_module` falls outside `waveflow.*`.

## In the examples

`examples/fir_block` is the worked case — a project that measures its own modules
([resource modelling](../../examples/firblock/resources.md)) while composing framework modules it did
not write. The arrangement is exactly the one above:

```text
waveflow/calib/platforms/zynq7020_bfm_100mhz/               4 configs — MemRStream, MemWStream
examples/fir_block/calib/platforms/zynq7020_bfm_100mhz/    35 configs — those 4, seeded, plus the
                                                                        example's FirCompute x26,
                                                                        FirCmdRx x5
examples/fir_block/calib/work/zynq7020_fir_sweep/          untracked — where the sweep writes
```

**Each example carries its own library**, at the same `calib/platforms/` path a user project uses — so
an example directory *is* a project: copy it and the layout is already right. They deliberately do not
share one, because sharing a calibration library across projects is not something a user does.

The sweep writes the **work tier** under its own name, and a deliberate publish promotes the result
into the project library:

```bash
waveflow_calib publish calib/work/zynq7020_fir_sweep \
                       calib/platforms/zynq7020_bfm_100mhz --apply
```

Note the project library carries the framework's 4 configurations too — that is the seeding copy, and
it is why the example can compose `MemRStream` and get measured area without reaching back into the
package.

The contrast with `examples/mem_copy` is the point. That example calibrates `MemRStream` /
`MemWStream` — **framework** modules — so it correctly publishes *upstream* into the shipped library.
`fir_block` calibrates its own, so it behaves like a user project. Which library an example publishes
into follows from whose modules it measured, not from where the example happens to live.

## Next

- [Directory layout](./layout.md) — what ends up inside, and where platform directories are searched
  for.
- [Platform identity](./identity.md) — the manifest and the mismatch gate.
- [Managing a platform](./workflow.md) — inspecting one, and the work → publish flow.
