---
title: Connecting Vitis and Vivado
parent: Installation
nav_order: 3
has_children: false
summary: "Waveflow runs Python simulations with no AMD tools installed; synthesis and RTL-level simulation need Vitis and Vivado 2025.1 or newer. Point Waveflow at them by setting WAVEFLOW_VITIS_PATH to the directory holding your AMD installs — Vivado is found automatically beside Vitis — then confirm the setup with the bundled test_amd_tools command."
---

# Connecting Vitis and Vivado

Waveflow needs **no AMD/Xilinx tools** for a large part of what it does. Declaring data
schemas and hardware modules, wiring interfaces, and running Python simulations all work on
a plain Python install — so you can follow most of this guide, and complete most of the
example designs, with nothing else installed.

The AMD tools become necessary when you want to **realize** a design rather than model it:

- **Synthesis** — translating a module's Python behavior into RTL through Vitis HLS
  (`sv_synth`, the `pytest -m vitis` tests, C synthesis and C/RTL cosimulation).
- **RTL-level simulation** — running the generated RTL in Vivado's `xsim`, including the
  cycle-accurate BFM gates (`pytest -m xsi`).
- **Implementation** — place and route for real resource and timing numbers (`sv_impl`).

For any of those, Waveflow has to know where your tools are installed.

Waveflow requires **Vitis and Vivado 2025.1 or newer**. Older releases may work but are not
tested, and `test_amd_tools` (below) will warn about them.

## Install Vitis and Vivado

Install the **AMD Unified Installer** and select *Vitis*, which includes Vivado. Both tools
are installed together and version-locked, which is why Waveflow only needs to be told about
one of them.

If you are working on a shared or managed machine — a university lab or compute server —
the tools are likely installed already. Ask your administrator for the install directory, or
look for an environment module (see [Shared servers](#shared-servers-and-environment-modules)).

## Point Waveflow at the tools

Waveflow finds Vitis through the **`WAVEFLOW_VITIS_PATH`** environment variable. Set it to the
directory that *contains* the version directories — not to the version directory itself, and
not to the executable:

```text
WAVEFLOW_VITIS_PATH
        |
        v
   C:\Xilinx\2025.1\Vitis\bin\vitis-run.bat        (Windows)
/tools/Xilinx/2025.1/Vitis/bin/vitis-run           (Linux)
```

So on a default install you would set it to `C:\Xilinx` or `/tools/Xilinx`.

**Windows** (Command Prompt) — `setx` writes the value permanently, but only takes effect in
a *new* terminal:

```bat
setx WAVEFLOW_VITIS_PATH "C:\Xilinx"
```

**Windows** (PowerShell):

```powershell
[Environment]::SetEnvironmentVariable("WAVEFLOW_VITIS_PATH", "C:\Xilinx", "User")
```

You can also set it through *Settings → System → About → Advanced system settings →
Environment Variables*.

**Linux / macOS** (bash or zsh) — add to `~/.bashrc` or `~/.zshrc` so it persists:

```bash
export WAVEFLOW_VITIS_PATH=/tools/Xilinx
```

**Linux** (tcsh or csh) — add to `~/.cshrc`:

```tcsh
setenv WAVEFLOW_VITIS_PATH /tools/Xilinx
```

Pointing the variable directly at the `vitis-run` / `vitis-run.bat` executable also works, if
you want to pin one exact installation.

### Which version gets picked

If several versions live under the directory you name, Waveflow selects the **newest** —
`2026.1` over `2025.1`. To pin an older one, point `WAVEFLOW_VITIS_PATH` at that version's
`vitis-run` executable directly.

If you do not set the variable at all, Waveflow searches the standard install locations
(`C:\Xilinx` on Windows; `/tools/Xilinx` and `/opt/Xilinx` on Linux). A default install is
often found with no configuration — but setting the variable explicitly is the reliable path,
and the only option for an install anywhere else.

### Vivado

You do not normally need to configure Vivado. Waveflow looks for it **beside the Vitis it
found**, which keeps the two versions matched, and falls back to a `vivado` on your `PATH`.

Only a **split install**, where Vivado does not sit next to Vitis, needs the companion
variable:

```bash
export WAVEFLOW_VIVADO_PATH=/tools/Xilinx/Vivado
```

## Check the setup

Waveflow ships a command that reports exactly what it can find:

```bash
test_amd_tools
```

It locates each tool, runs it to read its release, and prints a report. A working setup:

```text
Waveflow AMD/Xilinx tool check
==============================

Vitis
  status   : OK
  version  : 2026.1
  path     : /tools/Xilinx/2026.1/Vitis/bin/vitis-run

Vivado
  status   : OK
  version  : 2026.1
  path     : /tools/Xilinx/2026.1/Vivado/bin/vivado

All tools found: Vitis 2026.1, Vivado 2026.1.
Waveflow is ready for synthesis (minimum 2025.1).
```

If the tools cannot be found, the report says so and repeats the instructions above. Launching
the tools takes a few seconds each; add `--fast` to skip that and read versions from the
install paths instead. The command exits `0` when both tools are found at a supported release
and `1` otherwise, so it can gate a setup script or a CI job.

Prefer to call it from Python?

```python
from waveflow.toolchain import probe_amd_tools

for info in probe_amd_tools():
    print(info.name, info.version, info.path, info.meets_min)
```

## Shared servers and environment modules

Managed multi-user machines often expose the tools through
[environment modules](https://modules.readthedocs.io/) rather than a standard install path.
Loading the module puts `vivado` on your `PATH` and sets the license variable, but it does
**not** set `WAVEFLOW_VITIS_PATH` — so do both:

```tcsh
module load xilinx                        # whatever your site calls it
setenv WAVEFLOW_VITIS_PATH /eda/xilinx    # the directory holding 2026.1/, etc.
```

Here `/eda/xilinx` is an example; use the root your administrator gives you. Run
`module show <name>` to see which directory the module adds to `PATH` — the Waveflow variable
wants the directory two levels above `Vitis/bin`.

Licensed steps (notably Vivado implementation) also need a license server, normally set by the
module as `XILINXD_LICENSE_FILE`. If implementation fails with a licensing error while
synthesis works, that variable is the thing to check.

## Verify end to end

With the tools connected, the Vitis integration tests should run:

```bash
pytest -m vitis
```

These invoke real C synthesis and cosimulation and take considerably longer than the Python
suite. The everyday development loop skips them:

```bash
pytest -m "not vitis and not xsi"
```

The `xsi` RTL gates additionally need a prior C synthesis of each design; see
[Developer Setup](./developers.md) and the timing guide.

## A note on C++ compilers

The XSI flow compiles a small C++ testbench that drives the elaborated RTL. On **Windows** it uses
the mingw `g++` bundled inside the Vivado install, so nothing extra is required. On **Linux** it
uses the **system `g++`**, which must be on your `PATH`:

```bash
g++ --version      # any reasonably recent GCC will do
```

The flow itself is driven by `run.bat` on Windows and `run.sh` on Linux. Both scripts take the same
arguments and are copied into every design's `xsi/` workspace, so a workspace built on one platform
can be simulated on the other.
