# Concepts — recipe vs contract (design D12)

Anything that is an **amalgamation of existing primitives** — a dynamic
universe with listing/delisting, a rotation, a hedge shape — is *not* a new
primitive (the counted set stays at 8 — see `docs/primitives.md`). It is
shipped here, in `docs/concepts/`, in exactly one of two flavors:

**recipe** — prose + a worked example, assembled on demand. Use when the
composition is *not* parity-sensitive: the reader assembles primitives by hand
and the outcome has no backtest/live truth to hold.

**contract** — prose + a **runnable parity/invariant check**. Use when the
composition *is* parity-sensitive, i.e. backtest and live must agree on it by
construction (a universe must be point-in-time identical across backtest and
live — survivorship). A doc asserting parity is a claim; a contract makes it
verifiable for the user's actual implementation.

| Flavor | When | What it ships |
|---|---|---|
| recipe | assembled on demand, not parity-sensitive | prose + worked example |
| contract | parity-sensitive composition | prose + runnable check (guarded in `quant/tests/extensions/`) |

## Shipped concepts

- [`universe.md`](universe.md) — **contract**: point-in-time membership =
  instruments ∘ data ∘ membership rule, with a runnable survivorship parity
  check across backtest/live (survivorship-bias detection).