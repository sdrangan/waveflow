---
title: Skills
parent: Agentic DSE
nav_order: 3
audience: python
summary: "How an AI agent is given a method, for readers new to agents. What the model actually sees (text only; the harness reads files and runs tools for it), the three ways to give it instructions (prompt, CLAUDE.md, skill), what a skill is (a folder with SKILL.md: a description that is always listed, a body loaded on demand, files read only when needed), how a skill is invoked and expanded ($ARGUMENTS is replaced by the harness before the model sees the text), and why the DSE experiment uses one: the same skill, context and prompt serve a user in their own project and an agent in a clean sandbox."
---
# Skills

**Under construction.** The skill described here is
[`examples/dse_cg/skill/dse/SKILL.md`](../../../examples/dse_cg/skill/dse/SKILL.md); the tooling around
it is not written yet.

This page is for readers who have not worked with AI agents. It explains how an agent is given a
method to follow, and in particular what happens to a line like

```markdown
Your task: **$ARGUMENTS**
```

before the agent reads it.

## What the model sees

A language model reads text and writes text, and that is all it does. It cannot open a file, run a
program or remember an earlier session. Everything it knows about the task at a given moment is the
text in front of it, called its **context**.

An **agent** is a model put in a loop by a program called the **harness**. Here the harness is Claude
Code. The harness assembles the context, sends it to the model, and acts on what comes back:

```text
          ┌──────────────────────── harness (Claude Code) ─────────────────────────┐
          │                                                                          │
 you ───► │  context = instructions + tool list + conversation so far                │
          │        │                                                                  │
          │        ▼                                                                  │
          │      model ──► text for you, or a tool call: Read("DSE.md"), Bash("python run.py")
          │        ▲                                   │                              │
          │        │                                   ▼                              │
          │        └──── the tool's output, as text ◄── harness runs the tool         │
          └──────────────────────────────────────────────────────────────────────────┘
```

When we say "the agent reads a file", the model has written a request, `Read("DSE.md")`; the harness
has read the file and put its contents into the conversation as text; and the model has then read
that text. The model never touches the disk. The same goes for running Python: the model writes the
command, the harness runs it, and the output comes back as text.

So the question for any instruction file is: **when does the harness put it into the context, and in
what form?**

## Three ways to give an agent instructions

|                         | when it reaches the context                           | good for                                                                                     |
| ----------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **The prompt**    | when you type it                                      | the task at hand                                                                             |
| **`CLAUDE.md`** | at the start of every session in that project, always | facts true of every task in the project: how to run the tests, where things are, house rules |
| **A skill**       | only when it is invoked                               | a method for one kind of task, too long to retype and not needed in every session            |

A `CLAUDE.md` costs context in every session, whether the task needs it or not. A skill costs almost
nothing until it is used. A project usually has one `CLAUDE.md` and any number of skills.

## What a skill is

A skill is a folder holding a file called `SKILL.md`, plus any other files the method needs:

```text
.claude/skills/dse/          a project skill (personal ones go in ~/.claude/skills/)
    SKILL.md                 the skill
    dselib/__init__.py       a file it uses: here, the Python library the method calls
```

`SKILL.md` has two parts: a short header (YAML front matter) and a Markdown body.

```markdown
---
name: dse
description: Explore a hardware design's parameter space to meet an objective ...
argument-hint: <path to the design's DSE.md> <objective>
---

# Design-space exploration

Your task: **$ARGUMENTS**

The first argument is the path to the design's `DSE.md`; the rest is the objective. ...
```

## How a skill reaches the model

A skill reaches the context in three stages, each bigger than the last and each only when needed.

**1. At the start of a session, only the header.** Claude Code finds every skill in the project's
`.claude/skills/` and the user's `~/.claude/skills/`, and adds each one's `name` and `description` to
the context as a short list. The model now knows that a skill called `dse` exists and what it is for.
It has not seen the body.

**2. When the skill is invoked, the body.** There are two ways to invoke a skill:

- **You** invoke it by typing its name as a command: `/dse` followed by its arguments.
- **The model** invokes it itself, when a request matches a skill's description. It does this
  with a tool call, the same way it asks for a file.

Either way, Claude Code reads `SKILL.md`, **fills in the placeholders**, and adds the result to the
conversation as a message. Only then does the model read the body.

**3. While the model works, any other file, on request.** A file in the skill's folder, such as
`dselib/`, reaches the context only if the model asks to read it, like any other file. A skill can
point to a long reference without that reference costing anything until it is needed.

## Where `$ARGUMENTS` comes from

`$ARGUMENTS` is a placeholder for the harness, not text for the model. When you type a skill command,
everything after the skill's name is the **arguments**. Before the model sees the body, Claude Code
replaces `$ARGUMENTS` with them, much as a shell replaces `$1` in a script.

Suppose you type:

```text
/dse examples/dse_cg/DSE.md Minimize LUTs subject to MSE <= -20 dB at SNR 10 dB and <= 4,000 cycles per solve.
```

The skill's name is `dse`. The arguments are everything after it:
`examples/dse_cg/DSE.md Minimize LUTs subject to ...`. Claude Code puts the body into the
conversation with the placeholder filled, roughly like this:

```markdown
Base directory for this skill: /home/me/project/.claude/skills/dse

# Design-space exploration

Your task: **examples/dse_cg/DSE.md Minimize LUTs subject to MSE <= -20 dB at SNR 10 dB and <= 4,000 cycles per solve.**

The first argument is the path to the design's `DSE.md`; the rest is the objective. Read `DSE.md`
first. ...
```

The model never sees `$ARGUMENTS`. It sees your text in its place. The body then tells it how to
split that text: the first word is a path, the rest is the objective. Splitting is the model's job;
the harness only substitutes.

The first line matters too. Claude Code tells the model where the skill's folder is. That is how
the line in our skill that says

```python
import sys; sys.path.insert(0, "<this skill's directory>")
```

can work anywhere: the model writes the real directory in place of `<this skill's directory>` when it
writes its script. Unlike `$ARGUMENTS`, that substitution is done by the model, which is why the
text describes it in words rather than as a placeholder.

If the model invokes the skill on its own (stage 2, second way), it supplies the arguments itself,
from what you asked for.

## The DSE skill, end to end

Here is everything that happens when you ask for a CG design in your own project:

1. **Session start.** The context holds your `CLAUDE.md` and the skill list, including
   `dse: Explore a hardware design's parameter space ...`.
2. **You type** `/dse examples/dse_cg/DSE.md Minimize LUTs subject to ...`.
3. **Claude Code expands the skill**: it reads `SKILL.md`, substitutes `$ARGUMENTS`, and adds the
   body to the conversation, with the skill's directory.
4. **The model reads the body** and follows it. It asks to read `examples/dse_cg/DSE.md`, and the
   harness returns the design's parameters, constraints, workloads and costs.
5. **The model writes Python** that imports `dselib` from the skill's directory and calls `space()`,
   `golden(...)`, `pysim(...)`. The harness runs it. Each call goes to the DSE service, which runs
   the tool and charges the time.
6. **The model keeps going**: reading results, deciding what to synthesize, calling `synth` and
   `rtlsim`, until it calls `submit(params, report)` or the budget runs out.

Who knows what:

|              | what it holds                     | how it reaches the model                                        |
| ------------ | --------------------------------- | --------------------------------------------------------------- |
| `SKILL.md` | the method, for any design        | expanded on `/dse`, with your arguments                       |
| `DSE.md`   | this design                       | read by the model, because the skill tells it to                |
| your prompt  | this objective                    | the arguments, substituted into the skill                       |
| `dselib`   | the evaluation functions          | imported by the model's scripts; `help()` shows the reference |
| the service  | the budget, the ledger, the tools | answers `dselib` calls; never in the context                  |

## Why a skill, for the experiment

The [experiment](index.md) runs this same flow with no person present. A program creates a fresh,
empty directory (the **sandbox**) holding only:

- the skill;
- `DSE.md`;
- copies of the design's docs and source.

It starts Claude Code there with no `CLAUDE.md` and none of the user's settings or memory, and sends
the same line a user would type, `/dse DSE.md <objective>`. The two arms of the experiment get the
same files, except that the traditional arm's copy of the skill, `DSE.md` and `dselib` has every
mention of `pysim` removed.

So the instructions the agents follow in the experiment are exactly the ones a user would use. The
experiment measures the flow you would actually run, not one made up for the test.

## Things worth knowing

- **A skill is advice, not a program.** The model reads the body and usually follows it, but nothing
  forces it to. Rules that must hold, such as the budget or "submit only a measured design", are
  enforced by the DSE service, not by the text.
- **The model sees everything in the text, including Markdown comments.** An HTML comment is hidden
  when Markdown is displayed, but it reaches the model as written. Our skill's source files carry
  markers that the generator removes before the skill is installed. The model never sees the
  markers.
- **The description decides when the model invokes a skill on its own.** A vague description means a
  skill that is missed or used for the wrong task. Typing the command avoids the question, which is
  why the experiment always does.
- **The same format works outside Claude Code.** Programs built on the Claude Agent SDK can load the
  same skill folders. The experiment's runner uses that to start a session in the sandbox.
