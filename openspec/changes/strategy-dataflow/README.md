# strategy-dataflow

Reshape the existing engine pipeline **in place** into a strategy-to-venue
data-flow architecture. The 3-method `StrategyProtocol` (signal strings routed
through `SIGNAL_REGISTRY` → `SIZING_STRATEGIES` → asset-class executors) is
replaced by an author-owned body that declares typed instruments + typed data
subscriptions + a venue, sizes from instrument contract facts and an injected
read-only account projection, and submits typed `Order`s to the venue. The
venue is the only environment seam; determinism is pinned by a recorded
decision transcript over the same transformed code path.

**Rules of this change:** no backward compatibility (the legacy 3-method path
is deleted, never gated), no layering (every commit replaces one legacy
mechanism and deletes it in the same commit), and the full suite + ruff run
after **every commit** — no red commits.
