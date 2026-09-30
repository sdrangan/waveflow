---
title: User Setup
parent: Installation
nav_order: 1
has_children: false
summary: "Installing Waveflow as a library in your own project, with no clone: what a virtual environment is and why to use one, creating and activating it, and installing the package. Python 3.10 or newer. For modifying Waveflow itself, or running the bundled examples and tests, see Developer Setup instead."
---

# User Setup — Installing the Waveflow Package

This page is for **users** who want to *use* Waveflow as a library in their own
project — `import waveflow`, declare hardware, run simulations, generate HLS. You do
**not** need to clone the repository. If you instead want to modify Waveflow itself or
run the bundled examples and tests, see [Developer Setup](./developers.md).

Waveflow requires **Python 3.10 or newer**.

## What is a virtual environment (and why use one)?

A **virtual environment** is a self-contained Python installation dedicated to a single
project. Any packages you install into it are isolated from your system Python and from
your other projects, so one project's dependency versions can never break another's.
This is standard practice for Python work — it avoids the classic "everything worked
until I installed something else" problem — and Python ships with the `venv` tool to
create one. We recommend installing Waveflow into a fresh virtual environment.

## 1. Create and activate a virtual environment

From whatever directory holds your project:

```bash
python -m venv env          # create an environment named "env" (any name works)
```

This can take a minute and prints little; afterward an `env/` directory holds the
environment. Activate it:

```bash
.\env\Scripts\Activate.ps1   # Windows PowerShell
.\env\Scripts\activate.bat   # Windows Command Prompt
source env/bin/activate      # macOS / Linux
```

Your prompt shows `(env)` while it's active. If Windows PowerShell refuses to run the
activation script ("...Activate.ps1 is not digitally signed"), run this first:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## 2. Install Waveflow from GitHub

With the environment active:

```bash
(env) pip install git+https://github.com/sdrangan/waveflow.git
```

This downloads and installs the `waveflow` package and its dependencies — no clone
required. (pip uses `git` under the hood, so Git must be installed.)

## 3. Verify the install

```bash
(env) python -c "import waveflow; print('Waveflow installed at', waveflow.__file__)"
```

## What you get — and what needs the repo

`pip install` gives you the **Waveflow package**: the API for building data schemas,
components, interfaces, simulations, and HLS code generation inside your own scripts.

The **example walkthroughs** in this guide (the register-map, polynomial, histogram
designs, …) live in the repository's `examples/` directory, which a package install
does **not** include. To run those directly, clone the repo via
[Developer Setup](./developers.md), or simply follow along in the docs.

Optional AI-assistant integrations (the MCP server, semantic search, the VS Code
extension) are covered separately under [AI Tooling](../ai_tooling/) and are not needed
to use Waveflow.
