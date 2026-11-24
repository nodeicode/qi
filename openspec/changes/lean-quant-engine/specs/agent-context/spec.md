## Purpose

Layered markdown context for AI agents — because most extensibility work on quant-engine will be done by agents, the repo MUST ship a small set of authoritative, code-anchored context documents that give an agent the invariants, map, and recipes in one read, kept in sync with the code surface.

## ADDED Requirements

### Requirement: Root orientation contract

The repo MUST contain a root `AGENTS.md` that an agent can read first and, in under one screen, learn: what the repo is, the invariants that must never be broken (parity, adjusted bars, determinism), the command map (test/lint/format per module), and where to look for the deeper documents (this capability's per-subsystem docs, `docs/extensibility.md`, openspec). It MUST NOT duplicate subsystem detail — it points at it.

#### Scenario: Orientation in one read
GIVEN an agent that has only read root `AGENTS.md`
WHEN it is about to make its first edit
THEN it knows the repo's invariants, the test commands, and the exact document to read next for its subsystem

### Requirement: Per-subsystem agent docs

Each public subsystem (strategy authoring, decision engine, venue, data access, execution, portfolio, backtest, config, observability) MUST have a dedicated agent-facing document under `agents/` that captures: what lives there (files), the local invariants and conventions (typing rules, state-machine rules, canonical timestamps), the extension points specific to it, and how to extend safely with a runnable example. Subsystem docs MUST be discoverable from root `AGENTS.md` by a stable index.

#### Scenario: Subsystem doc drives an extension
GIVEN an agent extending the venue subsystem (e.g. adding a venue backend)
WHEN it reads `agents/venue.md`
THEN it finds the protocol contract, the files involved, the invariants (e.g. naive-UTC boundary), and a runnable example to model the extension on

#### Scenario: Stable index from root
GIVEN a new capability is added
WHEN root `AGENTS.md` is read
THEN the new subsystem's agent doc is listed in the index

### Requirement: Extensibility recipes

`docs/extensibility.md` MUST contain executable recipes — add a strategy, add a data source, add a venue, add an executor, build on observability primitives — each with a working minimal example wired to the tests, not pseudocode. Every recipe MUST be verified to run against the installed SDK.

#### Scenario: Recipe runs as written
GIVEN a developer (or agent) following the "add a strategy" recipe
WHEN they copy it into a fresh checkout
THEN it runs and its test passes without modification

### Requirement: Context-code sync

Per-subsystem docs MUST be kept coherent with the code: a change that adds/renames/removes a public extension point, protocol, or invariant MUST update the corresponding agent doc and the extensibility recipe in the same change; CI MUST flag a repo whose root `AGENTS.md` references missing documents.

#### Scenario: Extension point change updates docs
GIVEN a change that renames an extension point in the execution subsystem
WHEN the change lands
THEN `agents/execution.md` (and any recipe referencing it) is updated in the same change

#### Scenario: Broken doc references fail CI
GIVEN a root `AGENTS.md` linking to a deleted subsystem document
WHEN CI runs
THEN it fails with a message naming the missing document

### Requirement: Baseline scope and growth

Version 1 MUST ship the root orientation contract, the per-subsystem docs for the v1 surface (strategy, engine, venue, data, execution, portfolio, backtest, config, observability), and the extensibility recipes. Refinement (deeper gotchas, worked examples from real strategies, migration notes) is continuous and MUST NOT block the launch baseline.

#### Scenario: Baseline complete at launch
GIVEN the public repo at launch
WHEN an agent reads `AGENTS.md` + `agents/*.md` + `docs/extensibility.md`
THEN every v1 subsystem is covered with invariants and at least the add-a-strategy recipe runs
