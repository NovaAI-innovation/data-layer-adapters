# data-layer-adapters (placeholder)

**Status:** placeholder. Implementation lives in its own repository.

**Upstream:** `github.com/NovaAI-innovation/data-layer-adapters`

This directory is reserved by the `data-layer` umbrella as a wiring slot.
When the upstream repo is cloned here (or linked via `.gitmodules`), it owns:

- `lib/install.sh` — idempotent applier (`install | verify | status | reset`)
- Per-framework subdirectories (e.g. `a0/`) holding each framework adapter's plugin glue, MCP wiring, prompts, hooks
- `docs/adapters.md` — how each framework adapter consumes the service-level DSNs
- `docs/decisions/` — ADRs (append-only)

## Contract with the umbrella

`bootstrap adapters` will shell-out to `lib/install.sh install` here. The
submodule must:

1. Expose `lib/install.sh` with subcommands: `install`, `verify`, `status`, `reset`.
2. Read each per-adapter env from `$DATA_LAYER_ADAPTERS_<FRAMEWORK>_*` (set in `.env.example`).
3. Be idempotent: re-running `install` must be a no-op when already wired.
4. Return non-zero exit on `verify` failure so the umbrella's smoke test catches it.
