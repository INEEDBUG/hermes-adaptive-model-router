# Offline convergence rehearsal

How to rehearse a production → canonical convergence **without touching a deployment**, and
what such a convergence has to preserve. Companion to
[`production-reconciliation.md`](production-reconciliation.md), which records the observed
divergence, and to [`g1-shadow-evidence-2026-10-06.md`](g1-shadow-evidence-2026-10-06.md),
which publishes the shadow cohort.

## Why a rehearsal, and not an install

The deployed line and this repository differ in *semantics*, not only in bytes: how a route
is declared available, how `would_execute` is derived, which counter file holds rejections.
Rehearsing those differences offline is what makes a later install a controlled act rather
than an experiment on live traffic. Nothing in this document installs, restarts or enables
anything.

## Convergence contract

A convergence install must state these explicitly; the defaults of the repository are
deliberately inert.

| Setting | Convergence value | Why |
|---|---|---|
| `JEV_AVAILABLE_ROUTES` | `deepseek_flash,mimo_pro` | Availability must be configured, never assumed (`router/config.py`). These are the two routes the deployment validated and the two the routing request advertised, so the request contract and the shadow cohort stay comparable. Without it the dossier advertises **no** route and `would_execute` becomes `null` — honest, but a different request than the deployment was sending. |
| `JEV_ALLOWED_PLATFORMS` | unchanged (`feishu`) | Gate 1 of the human-turn boundary. |
| `JEV_DEPLOYMENT_GENERATION` | e.g. `canonical-0.2.0` | Content-free migration label written into new records; see below. |
| `JEV_SKIP_COUNTER_PATH` | only if counters already exist elsewhere | Points the writer at an existing counter file instead of starting a second, empty series. |
| `ROUTER_MODE`, `JEV_MIN_CONFIDENCE`, `JEV_MIN_MARGIN`, `JEV_TIMEOUT_SECONDS`, `JEV_MODEL` | unchanged | A convergence is a code-line alignment, not a rollout-gate advance. |
| `JEV_AUTO_APPROVED` | must remain unset | `auto` stays disabled; the installer never writes it. |

`mode.json` remains the authoritative runtime state: a missing file resolves to `off`, an
existing file (including a tripped breaker) is preserved byte-for-byte, and a `KILL`
sentinel is never bypassed — the installer refuses to write state while it exists.

## Deployment generation metadata

Every record written from this revision on carries `deployment_generation`, a bounded,
character-restricted label (`[a-z0-9._-]`, ≤64 chars) resolved from
`JEV_DEPLOYMENT_GENERATION`; an unset value resolves to the stable default `unversioned`.

* Records written **before** the key existed are reported by `tools/shadow_stats.py` as
  `legacy_unversioned`. They are never rewritten and never re-derived.
* `would_execute` means different things in the two generations (the legacy deployment
  decided executability from a hard-coded route list; the canonical line decides it from
  configured availability), so the tool refuses to publish a merged figure: when more than
  one generation is present, `would_execute` is `null`, a warning is printed, and the
  per-generation blocks are the only quotable numbers. `--generation NAME` restricts an
  analysis to a single policy; `--group-by-generation` states that intent explicitly.

## Rejection-counter compatibility

The counter file is content-free: day buckets of `platform|turn_origin|reason` counts over
a closed reason vocabulary. Two things differ between the generations — the filename
(`skipped-non-user-turn.json` vs the canonical `skipped-turn-counters.json`) and the day
basis (local vs UTC):

* `JEV_SKIP_COUNTER_PATH` overrides the location verbatim, so an existing file keeps being
  appended to and its history is neither copied nor orphaned.
* Reads are atomic read/modify/replace; counts are monotonic and existing day buckets are
  left as they are.
* Because the day basis changes, one migration boundary may split a single calendar day
  across two keys. Totals remain complete — every bucket is summed exactly once — and the
  historical keys are never rewritten to UTC.

## Safety note: the live-call harness is not a validation tool

A private deployment may ship a single-file harness that drives the plugin end to end and
performs a **real routing request** when the resolved mode is not `off`. It is a
fault-injection scratch tool, not an offline validator, and it must never be run against a
live deployment. Canonical validation is the five offline suites in this repository:

```
python3 tests/test_router.py
python3 tests/test_state.py
python3 tests/test_turn_boundary.py
python3 tests/test_secret_scan.py
bash    tests/test_installer.sh
python3 tests/test_convergence_rehearsal.py
python3 tools/init_state.py --dry-run
python3 tools/secret_scan.py --root . --git
```

## Rehearsal runbook

Everything below runs under a throwaway root; the routing client is replaced by a counting
stub and the credential is a dummy value, so no request can leave the host.

1. **Synthetic environment.** `HERMES_HOME`, `JEV_STATE_DIR`, `JEV_LOG_DIR`,
   `JEV_SKIP_COUNTER_PATH`, `JEV_ROUTER_ROOT` all point inside the rehearsal root;
   `JEV_AVAILABLE_ROUTES`, `JEV_ALLOWED_PLATFORMS`, `JEV_DEPLOYMENT_GENERATION` carry the
   convergence values; thresholds stay at their production values.
2. **Candidate tree.** Copy the repository checkout into the rehearsal root and use that
   copy as `JEV_ROUTER_ROOT`, so the installer and the plugin load the candidate, not the
   checkout.
3. **Installer rehearsal.** `--dry-run` first (it must announce its backups and write
   nothing), then a real install into the throwaway `HERMES_HOME` with a stand-in `hermes`
   CLI. Assert: fresh state initialised as `shadow`; existing `shadow`, existing `off`,
   tripped and `KILL` states preserved byte-for-byte; `--state-mode auto` refused; a second
   run idempotent (one `JEV_ROUTER_ROOT` line, one plugin entry, one plugin directory);
   existing `plugins.enabled` entries merged, never replaced; installed files not
   world-writable.
4. **Telemetry continuity.** Pre-seed a synthetic legacy JSONL (no generation field) and a
   synthetic canonical JSONL, plus a legacy counter file with a historical day bucket. Run
   the boundary rehearsal, then assert the legacy file is byte-identical, new records
   append, the legacy counter bucket keeps its count, and the statistics tool reports two
   generations with separate `would_execute` figures.
5. **Human-turn boundary.** Feed the plugin a matrix of synthetic turns: admitted
   (`feishu` + `user`, first API call), duplicate first call, second API call, retry,
   `background_review`, `subagent`, `internal_notification`, unknown origin, missing origin,
   non-allowlisted platform, missing platform, empty allowlist — and two simultaneous
   sessions. Assert exactly one decision per admitted turn, none for every rejected class,
   no cross-session contamination, and a content-free counter for each rejection reason.
6. **Redaction.** Synthetic values only: `api key=`, `api<TAB>key=`, `api-key=`, `api_key=`,
   `apikey=`, `access/refresh/auth token=`, `token=`, `client secret=`, `password=`,
   `credential=` must all be redacted, while email, IPv4, bearer and long opaque tokens keep
   their documented behaviour and ordinary prose is left untouched.
7. **Dossier equivalence.** Compare structure only — key set, route list, boolean feature
   schema, metadata types — against the known request contract. No prompt content is
   compared, printed or stored.
8. **Rollback simulation.** Snapshot the throwaway home, install, restore the snapshot and
   assert: state, telemetry and counters byte-identical; plugin directory gone; `.env` and
   configuration restored; then re-install and assert the converged state returns with a
   single plugin identity and exactly one `JEV_ROUTER_ROOT` line.

## What the rehearsal does not cover

It cannot validate that the routing service tolerates a given payload, that a live gateway
loads the plugin the way the stub does, or that a real restart behaves as documented. Those
belong to a separately authorised install with its own snapshot, quiescence gate, official
restart primitive, post-install validation and rollback trigger list.
