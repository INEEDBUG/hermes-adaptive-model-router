# Legacy variant sanitization review

Scope: decide what of a **private, never-committed deployment variant** is worth preserving as
history, what should be dropped, and whether a public legacy branch is justified. Nothing in
this review required publishing deployment source: the variant's value is its *semantic model*,
which is fully expressible as data plus fixtures.

Method: every divergence was enumerated against the canonical line, each file was scanned for
16 categories of deployment-specific content (absolute paths, accounts, host/container
identifiers, internal names, addresses, mail, credential shapes, endpoints, environment values,
state/log locations, deployment comments, timestamps, session/turn identifiers, recorded
payloads, live-call behaviour, deployment assumptions), and the historical behaviour was then
reconstructed in isolation instead of copied. Matched values were never printed, stored or
published — only categories and counts were.

## 1. What was reviewed

13 code artefacts: the plugin module and manifest, the seven router modules, the production
statistics tool, the patch checker, the patch body, and the deployment's single-file test
harness. No environment file, credential, log, telemetry record, state file, configuration or
backup was copied.

10 of 13 contain deployment-specific content; 3 are clean (`plugin.yaml`, `router/__init__.py`,
`router/redact.py`). The heaviest concentration is in the test harness (11 categories,
including endpoints, credential shapes and live-call behaviour) — which is exactly the artefact
that must not be published.

## 2. Semantic classification

| divergence | class | preserve as reference |
|---|---|---|
| hard-coded route availability (module constant + repeated in the routing payload) | `PRIVATE_DEPLOYMENT_ONLY` | yes — as *data*: it is the value the convergence must pin explicitly |
| legacy `would_execute` truth table (never null) | `HISTORICALLY_USEFUL` | yes — historical records must stay readable, never rewritten |
| inline skip counters, legacy filename, local day basis | `HISTORICALLY_USEFUL` | yes — as the migration contract for the counter override |
| fixed deployment paths for env / log / state resolution | `PRIVATE_DEPLOYMENT_ONLY` | no — replaced by `HERMES_HOME`-derived resolution with overrides |
| state resolution (state file authoritative, missing → off, sentinel → off) | `CANONICAL_ALREADY_SUPERSEDES` | no — identical semantics |
| human-turn gate (platform allowlist + `turn_origin == user`) | `CANONICAL_ALREADY_SUPERSEDES` | no — identical, canonical is stricter about scope |
| auto guard | `CANONICAL_ALREADY_SUPERSEDES` | no — canonical additionally refuses to implement auto |
| redaction pattern differences | `CANONICAL_ALREADY_SUPERSEDES` | no — the variant was stronger only before the canonical repair |
| patch checker differences | `CANONICAL_ALREADY_SUPERSEDES` | no |
| statistics tool differences | `CANONICAL_ALREADY_SUPERSEDES` | no — canonical is generation-aware |
| single-file live-call harness | `LIVE_ONLY_TOOLING` | description only — never source |
| module export tables, manifest version fields | `BUGGY_LEGACY_BEHAVIOUR` / cosmetic | no |

## 3. Sanitization rules

16 deterministic rules were derived and applied to the reconstruction (absolute paths →
symbolic placeholders; accounts, host identifiers, internal names, mail, credential shapes,
addresses beyond documentation ranges, timestamps and real identifiers → omitted; environment
values dropped while keeping key names; state/log locations respelled with public symbolic
locations; deployment comments neutralised; live-call paths excluded; deployment assumptions
expressed as data, never as code). The reconstruction is built from the canonical baseline plus
that data — not by editing a copy of the deployment, so no unreviewed line can survive.

Result: a sanitized candidate of six files — a warning README, the semantic model, the ruleset,
an expectation fixture, an offline runner, and the harness decision note. It contains no source
from the deployment, no credential, no log, no telemetry record and no absolute deployment path.

## 4. Verification

* canonical secret scanner over the candidate: **clean**
* deployment-identifier audit (production paths, runtime paths, container-shaped identifiers,
  private address ranges, mail, credential shapes, session/turn identifiers, telemetry markers):
  two residual matches, both adjudicated — the provenance anchor commit id (a public canonical
  commit, not a host identifier) and the legacy counter *filename* (a bare name needed to state
  the migration contract, carrying no path and no payload). Both are accepted deliberately.
* behavioural fixtures (offline, stub client that fails the test if invoked, temporary
  directory only): 9/9 — the historical route list is expressible as configuration, the payload
  advertises it, every recorded truth-table outcome is reproduced, a merged record can never
  name an unconfigured route, the legacy counter filename is accepted through the documented
  override, counters stay content-free and single-bucket, a request for automatic mode still
  resolves to shadow, the state file stays authoritative, and the sentinel still forces `off`.

## 5. Live-call harness decision

Not published, in any form that can execute. The deployment's harness performs a real routing
request whenever the resolved mode is not `off`; publishing it would ship a tool that talks to a
live routing service. Its non-live sections are described as a matrix with the canonical offline
suites that cover the same ground; canonical validation remains the five offline suites plus the
convergence rehearsal.

## 6. Is a public legacy branch worth it?

**No.** Reproducibility adds nothing (the model is data, and the code is not what ran);
historical auditability is already served by this review, the reconciliation notes and the
public issue record; rollback value is small (the rollback that matters is state and
environment, not code); a sanitized rewrite is *not* the artefact that ran, so its evidentiary
weight is low; and it carries real costs — maintainer confusion, accidental deployability (a
branch that looks like a working variant), redundant privacy exposure and permanent upkeep for
a line that must never merge. The genuinely reusable part is the model, and it belongs in main
as documentation.

## 7. What main still needs before convergence

Nothing further in code. Convergence is blocked only by deployment-side configuration acts,
each needing its own authorisation:

1. set `JEV_AVAILABLE_ROUTES=deepseek_flash,mimo_pro` on the deployment (the repository must not assume it);
2. point the counter override at the existing legacy counter so accumulated rejections are preserved;
3. perform the install and the post-convergence verification in a shadow-only state.

## 8. Controlled convergence readiness

**CONDITIONAL.** Already satisfied on main: CI green; offline rehearsal green; redaction
regression repaired with provably failing-then-passing tests; route availability explicit and
tested; counter compatibility present; generation metadata and generation-aware statistics
present; turn-origin checker passing; installer idempotent with state/KILL preservation;
rollback rehearsed. Outstanding conditions: the two deployment configuration acts above, and the
standing ones — automatic mode stays disabled, thresholds unchanged, no live provider request
during convergence, and a shadow-only observation window afterwards.
