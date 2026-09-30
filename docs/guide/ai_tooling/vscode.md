---
title: Installing the VS Code Extension
parent: AI Tooling
nav_order: 1
has_children: false
summary: "Setting up the development environment for the Waveflow VS Code extension — Node.js, the build, and the debug run. Only for contributors working on the extension itself; using Waveflow needs neither Node.js nor this build environment."
---

# VS Code Extension Setup

This guide describes how to set up the development environment for the Waveflow VS Code extension. These steps are only required for contributors working on the extension itself. Users of Waveflow do not need Node.js or the extension build environment.

---

## 1. Install prerequisites

You will need:

- Node.js (LTS recommended): https://nodejs.org
- Visual Studio Code: https://code.visualstudio.com

No Python virtual environment is required for the extension.

To validate the Node.js installation, open a terminal (e.g., PowerShell on Windows or a terminal on macOS/Linux):

```bash
node -v
npm -v
```

The precise version numbers may change.

If you are on Windows and get a permission error, open PowerShell as administrator and run:

```powershell
Set-ExecutionPolicy RemoteSigned
```

---

## 2. Navigate to the extension folder

From the root of the repository:

```bash
cd waveflow-extension
```

This folder contains the extension's `package.json`, `tsconfig.json`, and TypeScript source files.

---

## 3. Install Node dependencies

Run:

```bash
npm install
npm install --save-dev @types/node
```

This installs:

- TypeScript compiler
- VS Code type definitions
- Node type definitions (required for `Buffer`, `process`, etc.)

All dependencies are local to this folder. Nothing is installed globally.

---

## 4. Build the extension

To compile once:

```bash
npm run compile
```

To compile continuously while editing:

```bash
npm run watch
```

This generates JavaScript output in the `out/` directory.

---

## 5. Launch the Extension Development Host

Open the **root** Waveflow repository in VS Code:

```
waveflow/
```

Then press **F5**.

This opens a new VS Code window called **Extension Development Host** with the extension loaded.

---

## 6. Open the toy example workspace

In the Extension Development Host window:

1. File → Open Folder…
2. Select:

   ```
   waveflow/examples/vscodedemos/
   ```

3. Open `myclass.py`
4. Run the command:

   ```
   Waveflow: Add Hello Method
   ```

The file should update in place.

---

## 7. Editing the extension

Edit files in:

```
waveflow-extension/src/
```

If you are running `npm run watch`, TypeScript rebuilds automatically.
Reload the Extension Development Host window with **Ctrl+R**.

Changes take effect immediately.

---

## 8. Common commands

| Command | Description |
|---|---|
| `npm install` | Install dependencies |
| `npm run compile` | Build once |
| `npm run watch` | Build continuously |
| **F5** | Launch Extension Development Host |
| **Ctrl+R** | Reload extension in the host window |
