# Runtime config — `quant.config` (spec `runtime-config`, design D6)

Live configuration is a **passive, version-checked poll** with pluggable
backends. Local env/file are the defaults so the SDK runs with zero AWS; SSM
Parameter Store is an opt-in prod backend (`quant[ssm]` extra, resolved lazily
so `import quant.config` never touches boto3). Config applies synchronously
between ticks — no restarts, no threads in the hot path.

## Invariants (do not break without discussion)

1. **Local-first, zero AWS by default.** With no cloud config present, the SDK
   reads live config from `QI_CONFIG_JSON` (env) and/or a local JSON file and
   never attempts a cloud call. `boto3` is not imported by `quant.config`.
2. **One lightweight backend call per poll.** `ConfigSource.scan()` is a single
   cheap call (file stat / SSM get / env check); the hot path is exactly that.
3. **Version short-circuit.** An unchanged backend version returns `None`
   without re-parsing the payload. A changed valid version is delivered
   **exactly once**; subsequent polls return `None` until it changes again.
4. **Adopt-once validation, silent errors.** An invalid payload is refused
   while its version is committed (the error is logged once per version, prior
   config stays active). Any backend poll error returns `None` and never
   raises — the previously-adopted config stays active.
5. **Validation is generic; reference resolution is deferred.** The shell
   validates the payload schema (a dict with a `strategies` list; each entry
   carries a string `name`). Resolving venue/strategy names happens at apply
   time in the runtime's strategies layer, not in the generic shell.

## Where it lives

| Path | Responsibility |
|---|---|
| `quant/src/quant/config/base.py` | `ConfigSource` protocol, `BaseConfigSource` poll machinery, `validate_config` / `parse_config` |
| `quant/src/quant/config/file.py` | `FileConfigSource` — version = `(mtime_ns, size)` stat |
| `quant/src/quant/config/env.py` | `EnvConfigSource` — reads `QI_CONFIG_JSON` |
| `quant/src/quant/config/sync.py` | `ConfigSync` — the generic version/validate/apply shell |
| `quant/src/quant/config/ssm.py` | `SSMConfigSource` — prod backend (`quant[ssm]`), lazy import |
| `quant/src/quant/config/_registry.py` | `register_source` / `config_source_by_name` (built-ins `file`, `env`; `ssm` lazy) |

## Quickstart — local-first config offline

```python
import json
import pathlib
import tempfile

from quant.config import ConfigSync, FileConfigSource

payload = {"strategies": [{"name": "example_equity", "clients": ["paper"]}]}

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump(payload, f)
    path = pathlib.Path(f.name)

sync = ConfigSync(FileConfigSource(path))

print(sync.poll() is not None)   # True   (first read adopts the payload)
print(sync.poll() is None)       # True   (unchanged version short-circuits)

# Bump the version -> the config is adopted exactly once more.
path.write_text(json.dumps({**payload, "version": 2}))
print(sync.poll() is not None)   # True    (re-adopted)
print(sync.poll() is None)       # True    (stable again until next bump)
```

Backends resolve by name: `ConfigSync.from_backend("env")` and
`config_source_by_name("file", path=...)` / `("ssm", ...)` (the latter only
imports boto3 on first use). An unset env var or absent file is a silent,
no-config zero-AWS state — the runtime starts immediately.

## Extension points

- **New config backend** (a KV store, Kubernetes configmap, redis): implement
  `ConfigSource` (set `name`, implement `scan()`) and register it —
  `register_source("my_backend", MySource)` — then select it by name.
- **Different payload schema** — keep the generic shell; validation is one
  function (`validate_config`) that a runtime can replace per policy.
- **Apply behavior** — applying an adopted config (add/remove strategies,
  per-strategy overrides, cache invalidation) belongs to the runtime's
  strategies layer (the live trader), not the generic shell.

## Verify

The quickstart above was validated by executing its code paths (`uv run python -
<<'PY' ... PY`): first adoption, unchanged-version short-circuit, re-adoption
after a version bump, and the silent no-config env/unset behavior all matched.