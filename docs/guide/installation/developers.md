---
title: Developer Setup
parent: Installation
nav_order: 2
has_children: false
---

# Developer Setup — Cloning and Installing for Development

This page is for **developers** who want to modify Waveflow, contribute changes, or run
the bundled examples and tests. If you only want to *use* Waveflow as a library in your
own project, see [User Setup](./users.md) — you don't need to clone the repository.

## 1. Clone the repository

```bash
git clone https://github.com/sdrangan/waveflow.git
```

The repository is updated frequently. To fetch the latest and discard any local changes:

```bash
git fetch origin
git reset --hard origin/main
```

## 2. Create and activate a virtual environment

Use a virtual environment to isolate the project's dependencies (see
[User Setup](./users.md) for what a virtual environment is and why). From the directory
just outside `waveflow`:

```bash
python -m venv env
.\env\Scripts\Activate.ps1    # Windows PowerShell  (.bat for cmd; `source env/bin/activate` on macOS/Linux)
```

On Windows PowerShell, if activation is blocked as "not digitally signed", run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first.

## 3. Install in editable mode with the dev tools

From the repository root, with the environment active:

```bash
(env) pip install -e ".[dev]"
```

This installs `waveflow` **editable** — your source edits take effect immediately, no
reinstall — plus the development extras (`pytest`, `ruff`, `black`, `mypy`). You only
need to do this once per environment.

Verify, then run the tests:

```bash
(env) python -c "import waveflow; print(waveflow.__file__)"
(env) pytest -m "not vitis"      # the fast suite (skips the Vitis HLS integration tests)
```

The Vitis HLS integration tests run under `pytest -m vitis` and require a Vitis
installation — see [Connecting Vitis and Vivado](./vitis.md) to set that up, and
[Synthesis](../comp_codegen/) for the flow itself. Run `test_amd_tools` to check whether
your AMD tools are visible to Waveflow.

## Maintaining the requirements files

Runtime and dev dependencies live in `pyproject.toml`; `pip install -e ".[dev]"` reads
them directly, so you normally don't touch `requirements*.txt`. The repo keeps pinned
`requirements*.txt` files for reproducibility. To regenerate them after changing
dependencies:

```bash
python -m pip freeze > requirements.txt
sed 's/[<>=~!].*//' requirements.txt > requirements-loose.txt   # version-stripped
```

PowerShell equivalent for the loose file:

```powershell
(Get-Content requirements.txt) -replace "[<>=~!].*","" | Set-Content requirements-loose.txt
```

When editing `requirements.txt`, drop these lines if present:

- `pywin32==...` — Windows-only; remove it for cross-platform installs.
- `-e git+https://github.com/sdrangan/waveflow.git@...#egg=...` — not needed; you've
  already installed Waveflow editable.
