---
name: karpathy-guidelines
description: Maintenance-only engineering guidelines for working ON this workspace's own code. Use when writing, reviewing, refactoring or debugging the tools, tests and configuration here: keep it simple, make surgical changes, surface assumptions, define verifiable success criteria. Not part of /scrape, /rank or /shortlist and never changes the product boundary.
license: MIT
---

# Karpathy Guidelines

Behavioral guidelines to reduce common LLM coding mistakes, derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) on LLM coding pitfalls.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## Scope in this workspace

This is a MAINTENANCE skill. It governs how the code in this repository is changed; it governs nothing the product does at runtime.

- It applies to coding, code review, bug fixing and refactoring of `tools/`, `config/`, `.claude/` and the tests.
- It is not a dependency of `/scrape`, `/rank`, `/shortlist`, `/screen` or `/healthcheck`, and those commands must run identically whether or not it is loaded.
- It never widens the product boundary. `discover -> verify -> match -> rank -> shortlist -> stop` is unchanged, and nothing here authorises an application action, a new capability, a tool grant, or a relaxation of the trust boundary.
- Where it disagrees with `CLAUDE.md`, the project rules win. This advises on HOW to change code, not on WHAT this workspace is allowed to do.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" -> "Write tests for invalid inputs, then make them pass"
- "Fix the bug" -> "Write a test that reproduces it, then make it pass"
- "Refactor X" -> "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
3. [Step] -> verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## In this workspace, "verified" means

The deterministic gates already exist. Use them as the success criteria rather than inventing new ones:

```text
python tools/validate_workspace.py --deep
python tools/job_state.py doctor
python tools/preflight.py
python tools/candidate_config.py validate
python tools/match_evaluation.py validate-policy
python tools/application_audit.py audit
```

A behaviour change is finished when a test proves the OLD behaviour would have failed, not merely when the suite is still green.

## Attribution

MIT licensed. Principles derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) on LLM coding pitfalls.

The four numbered sections are reproduced from the upstream skill with their wording unchanged; the only edit is typographic, replacing the arrow character with `->` to match this workspace's ASCII convention. The `## Scope in this workspace` and `## In this workspace, "verified" means` sections are this workspace's own integration notes and are not upstream content.
