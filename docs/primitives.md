# Primitives — the counted index (design D11)

Qi exposes a **closed, counted set of 8 primitives**. It is an editorial
reduction, not a vocabulary: membership is derived by test, not vibes (D11),
and numbering is for counting/indexing, never ranking.

**Derivation** (why a thing is a primitive):

- **parity-load-bearing** — if backtest and live disagree here, results lie
  (engine decisions, position, portfolio, instrument, venue identity,
  adjusted-bar / interval semantics);
- **universal-seam** — every strategy touches this regardless of venue/asset
  (data, execution, config, observability).

**Tier** (replaceability, D13 — decided by how Qi is used):

- **pinned** — the spine: engine decisions, portfolio/position accounting,
  instrument-ontology root rules;
- **port** — replaceable behind conformance: venue, data source, executor,
  instrument type.

## The closed 8

| # | Primitive | Home | Derivation | Tier |
|---|---|---|---|---|
| 1 | venue | `quant.venue` | parity-load-bearing | port |
| 2 | portfolio | `quant.portfolio` | parity-load-bearing | pinned |
| 3 | position | `quant.portfolio` | parity-load-bearing | pinned |
| 4 | instrument | `quant.instruments` | parity-load-bearing | port (type) / pinned (ontology root) |
| 5 | observability | `quant.observability` | universal-seam | port (emitter) / pinned (span-key set) |
| 6 | config | `quant.config` | universal-seam | port |
| 7 | data | `quant.data` / `DataSourceProtocol` | universal-seam | port |
| 8 | engine | `quant.core.engine` | parity-load-bearing | pinned |

## What is deliberately NOT a primitive

Anything narrow, non-universal, or alpha-specific: caches, clocks, message
buses, risk engines, and the strategy itself are implementation details or
alpha-owned, not primitives. A composed convenience (a universe, a rotation, a
hedge shape) is a **concept**, not a 9th primitive — see `docs/concepts/`.

## Why it matters

The set is the contract surface for the extension model (`docs/
extensibility.md`): **pinned** members are where parity lives and are the least
replaceable; **port** members are replaced behind per-port conformance suites
(`quant/tests/extensions/test_conformance_*.py`), which run against both the
backtest and live roots. A replacement that passes its port's suite in both
roots is parity-safe; one that does not must not paper/live a run.

This index is mirrored in root `AGENTS.md` and in the OpenSpec design's D11;
keep all three in sync when the set changes (membership is derived by test, so
a change starts there).