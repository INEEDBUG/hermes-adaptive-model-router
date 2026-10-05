# Production / repository reconciliation

This document records a verified divergence between **what this repository specifies**
and **what one private production deployment currently runs**. It exists so that the two
are never silently conflated: repository semantics are the canonical public line, while
the deployment described below is an observed state that has not yet been converged onto
it.

Nothing here is a claim that the deployment is broken. It is in daily use, in shadow
mode, with automatic switching disabled and the human-turn boundary active.

## Statement of record

```
PUBLIC_REPOSITORY_IS_NOT_CURRENTLY_A_BYTE-FOR-BYTE_DESCRIPTION_OF_PRODUCTION = YES
```

Verified by comparing every router module and plugin file of the deployment against every
file of every commit in this repository's history: no deployed file is byte-identical to
any committed revision, and the deployment's own revision history is not part of this
repository. The comparison is deterministic (file digests and per-commit blob digests) and
was performed read-only.

## PUBLIC_REPOSITORY_SEMANTICS (v0.2.0, `main`)

What this repository states and tests:

- **Route availability is configuration, never an assumption.** `would_execute` is
  restricted to the routes an operator named in `JEV_AVAILABLE_ROUTES`; with the public
  default (empty) it is `null`. Assuming a provider exists is treated as exactly how an
  unvalidated route reaches production.
- **Strict human-turn provenance.** A turn is observed only when `platform` is
  allow-listed **and** `turn_origin == "user"`; a missing or unrecognised origin fails
  closed (rejected, never defaulted to human).
- **Skip telemetry is modularised** (`router/skip_telemetry.py`), with rejection counters
  kept in a separate content-free file.
- **Mode resolution is a closed ladder** (kill sentinel → unreadable state → `tripped` →
  unknown mode → `auto` without approval → expired `auto_until` → stale `heartbeat` →
  as recorded), and `auto` is not implemented in this release.
- **The suites and documents in this repository describe the behaviour above** — the
  counts published in the READMEs and in `docs/` were re-verified against this code.

## CURRENT_PRIVATE_PRODUCTION_DEPLOYMENT (observed 2026-10-06)

What the deployment actually does, as observed read-only:

- **Shadow only**, automatic switching disabled; the executing model is unchanged by a
  routing decision.
- **Human-turn gate active and effective**: a structural `turn_origin` integration patch
  is applied to the host agent and its drift checker exits `0` (patch intact, plugin gates
  present, fail-closed proof holds). Observed rejection counters are entirely non-human
  origins, and no human-origin turn is missing from the record set.
- **Based on a divergent, never-committed variant.** Route availability in the deployed
  code is derived from a hard-coded availability flag rather than from
  `JEV_AVAILABLE_ROUTES`, so `would_execute` is populated where the public line would
  report `null`.
- **Not identical to any public commit** — including the module split that the public line
  performs (the deployment integrates rejection counting inside the plugin instead of a
  separate module).
- **Still functioning as designed for shadow collection**: decisions recorded, execution
  untouched, rejection telemetry content-free.

Consequence for evidence: the deployment's observed `would_execute` values are historical
production behaviour, **not** canonical v0.2.0 evidence. See
`docs/g1-shadow-evidence-2026-10-06.md`, which carries the same warning on the field
itself.

## Why this is documented instead of fixed

The divergence is a *semantics* question first and an install question second. Choosing
which semantics are canonical changes what future telemetry means, what the rollout gates
measure, and what a migration would have to preserve — none of which should be decided as
a side effect of a documentation or repository update. Until that decision is taken and an
installation is explicitly authorised, the honest statement is: public line = canonical
specification; deployment = observed state with drift.

## Convergence rule

Converging production onto this repository is a separate, explicitly authorised piece of
work, and it must proceed in this order:

1. **Decide canonical semantics.** `JEV_AVAILABLE_ROUTES`-based availability versus the
   deployment's flag-based availability, decided deliberately and recorded — including
   what happens to `would_execute` continuity.
2. **Offline test first.** The chosen semantics must pass the offline suites, with tests
   added for whichever behaviour changes.
3. **Write a migration plan.** Files, state files, environment variables and telemetry
   format, with the exact commands to be run.
4. **Preserve existing shadow telemetry.** Historical records stay as they are; a semantics
   change is a discontinuity in the field's meaning and must be documented as one, not
   back-filled.
5. **Explicit production authorisation.** A named, scoped authorisation for that install —
   never implied by a repository commit.
6. **Controlled install**, with the documented restart primitive and no fallback
   primitives.
7. **Post-install validation**: drift checker exit `0`, mode still `shadow`, auto still
   disabled, human-turn gate still fail-closed, rejection telemetry still content-free,
   and a fresh statistics snapshot.

**A repository update must never auto-synchronise production.** Merging documentation,
publishing evidence or repairing CI in this repository changes nothing about what a
deployment runs; installation is a separate authorised act.
