## Purpose

Live configuration as a passive, version-checked poll with pluggable backends — local env/file is the default so the SDK runs with zero AWS, SSM Parameter Store is an optional prod backend. Config applies synchronously between ticks; no restarts, no threads in the hot path.

## ADDED Requirements

### Requirement: Local-first default

With no AWS configuration present, the SDK MUST read live config from the local environment (env vars) and/or a local config file — it MUST NOT require boto3, IMDS, or any cloud dependency for configuration.

#### Scenario: Zero-AWS startup
GIVEN no cloud credentials and a local config file configured
WHEN the runtime starts
THEN it reads and watches config from the file (and/or env) and never attempts any cloud call

### Requirement: Passive version-checked polling

A config source MUST expose `poll() -> dict | None` returning a validated config only when the backend's content version changed; unchanged versions and any error MUST return `None` without raising. The hot path MUST be a single lightweight backend call (file stat / SSM get / env check).

#### Scenario: Unchanged version short-circuits
GIVEN a backend whose version equals the last seen
WHEN `poll()` is called
THEN it returns `None` and does not re-parse the payload

#### Scenario: Changed version returns config once
GIVEN a backend whose version increased with a valid payload
WHEN `poll()` is called
THEN it returns the new config dict once, and subsequent polls return `None` until the version changes again

#### Scenario: Poll errors are silent
GIVEN the backend is unreachable or returns garbage
WHEN `poll()` is called
THEN it returns `None` and prior config stays active

### Requirement: Validation before adoption

A payload MUST be validated (dict with a `strategies` list; each entry carries a string name and known keys; venue/strategy names resolve) before adoption; an invalid payload MUST be refused while the current version is committed, so the same error is not re-logged every cycle. The operator bumps the version to retry.

#### Scenario: Invalid payload refused once
GIVEN a backend version whose payload fails validation
WHEN `poll()` is called repeatedly
THEN the error is logged once per version, the payload is never adopted, and prior config remains active

### Requirement: Pluggable backends

Config backends MUST implement a common `ConfigSource` interface (current version + payload + poll semantics); local file/env and SSM backends MUST ship. Backend selection MUST be by configuration.

#### Scenario: Backend switch by config
GIVEN a runtime configured for the SSM backend
WHEN it starts
THEN it polls and validates via SSM Parameter Store
GIVEN a runtime configured for the file backend
WHEN it starts
THEN it polls and validates the local file and never touches SSM

### Requirement: Live application without restarts

The runtime MUST apply adopted config changes synchronously between ticks: add new strategy instances, remove gone ones, and apply per-strategy overrides while preserving state of untouched strategies. A partially valid update MUST apply its valid part and log the rejected parts. Post-application, affected engines MUST invalidate caches so decisions never mix old/new config.

#### Scenario: Strategy added/removed live
GIVEN a config update adding one strategy and removing another
WHEN applied
THEN the new strategy starts next tick, the removed one is wound down, and untouched strategies keep their state

#### Scenario: Partial update applied partially
GIVEN an update where some overrides are valid and others are not
WHEN applied
THEN valid overrides apply, invalid ones are logged and skipped, and unchanged strategies are untouched

#### Scenario: Cache invalidation on adoption
GIVEN a config adoption that changes a strategy's parameters
WHEN next tick processes
THEN the strategy's cached signals reflect the new parameters (invalidated, recomputed)
