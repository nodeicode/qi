# Qi Roadmap — long-term goals

Tracks the strategic arc Qi is on. Short-term, actionable work lives in the
active OpenSpec change(s) under `openspec/changes/`; this file is the *why
next*, the intended direction across releases. It is not a promise or a
schedule — it records direction so agents and contributors can tell "in
scope for later" from "out of scope, deliberately."

## Current focus: the strategy-as-dataflow surface

Qi is re-orienting its strategy-authoring surface from "a passive protocol the
engine drives through a fixed 3-method pipeline" toward "**an author-owned body
that ingests typed data + portfolio/account state and produces typed Orders to
a venue**." The engine stops owning decisions and becomes the determinism /
replay / parity harness. This is being shaped in the active change
`strategy-dataflow` and is the foundation everything below builds on.

## Long-term goals

### 1. Generalize data ingestion breadth — bars first, then the rest

Support every data *type* Nautilus Trader does, on a schedule that keeps Qi
boring and truthful while it broadens:

- **[in progress] Bars first.** The dataflow surface ships bars-only
  (`bars@interval`), matching today's OHLCV reality, so the new strategy shape
  and venue-order seam land without a data-breadth dependency.
- **Ticks / trades** next — quote/trade streams for the same instruments.
- **Order-book (L1 → L2)** — top-of-book and depth/delta subscriptions for
  quote-driven and market-making strategies.
- **Multi-asset / derived markets** — an underlier plus its option chain, a
  future's front/back monthly contracts, options-on-futures; one strategy
  acting across several instruments.

Each step is a *data-port* widening, not a strategy-surface change: strategies
declare subscriptions, the cross-strategy data port decides how to fulfill them
in backtest (replay) and live (stream).

### 2. Re-derive and re-document the correctness story

The parity/truthfulness thesis holds — same strategy body both environments,
venue as the only seam — but it must be **re-stated** for the reframed
architecture (author-owns-decision, event-replay determinism) rather than the
old "engine owns every decision" framing. A later task updates the user-facing
correctness material (whitepaper, evaluation methodology, extensibility) to
match. **Specs and internals stay the source of truth through the refactor**;
docs catch up once the seams are stable.

### 3. Keep the SDK lean and the primitive count honest

As data breadth and the strategy surface widen, Qi must hold the "boring,
forkable foundation" line against Nautilus-style framework bloat: breadth on
the *data* and *seam* side, not on the *strategy* side. The closed-primitive set
stays counted; new composed conveniences are concepts, never 9+th primitives.

## Cross-cutting constraints

- **Parity by construction** is non-negotiable: any widening must preserve
  "same strategy code both environments; only the venue fills differ," guarded
  by deterministic event-replay golden tests.
- **Adjusted-bar / no-lookahead discipline** continues to apply to whatever
  data type is added.
- **Boring first:** a new capability ships in its simplest form, then broadens
  (bars before ticks before books) — never the reverse.