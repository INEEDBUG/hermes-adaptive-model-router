# Production convergence closeout — 2026-10-06

The observed production deployment has been converged onto the frozen canonical runtime
candidate. Production is now the canonical shadow implementation: it observes a turn,
records a routing decision, and **does not choose the executing model**.

This note records what was validated, in content-free form. It is deliberately short: the
evidence behind it is metadata-only and is summarised in aggregate, not republished.

## Frozen identity

| item | value |
| --- | --- |
| runtime candidate | `dff8b117d38f597617497fca5567b0e7ec0bde9a` |
| deployment generation | `canonical-dff8b11` |
| release tag created | no |
| deployment retry | #1, after an executor-side check was fixed |

The candidate was not re-derived for the retry: the immutable runtime tree staged earlier
was re-verified instead (no `.git`, manifest identity exact, router and plugin hashes equal
to the frozen revision, secret scan clean, permissions read-only) and reused.

## Why there was a retry

The first convergence attempt stopped **before** any restart and restored itself
completely. The cause was in the executor's own validation logic, not in the runtime: the
check that counts active plugin directories matched directories by substring, so the hidden
rollback directory was counted as a second active plugin and a correct swap was reported as
a failure.

The detector now requires an exact basename match, and the check was verified against a
fixture tree containing decoy directories (`other-shadow-router`, a bare `shadow`, a
suffixed backup name, and dotted rollback/stage directories) before the retry was allowed
to run. Only `jev-shadow-router` may count as active.

## Static acceptance

| check | result |
| --- | --- |
| preflight, quiescence, fresh backup | pass |
| immutable runtime reused and re-verified | yes |
| environment mutation limited to the four canonical keys | yes |
| installer dry-run | pass |
| plugin swap, active plugin count | pass (exactly one) |
| pre-restart validation | pass |
| official convergence restarts | 1 |
| rollback restarts | 0 |
| static acceptance | PASS |

Every static invariant was re-read afterwards, read-only: no drift.

## Natural human canary

A real human turn on the real gateway was observed after static convergence completed.
For that turn:

| item | result |
| --- | --- |
| canonical JEV attempts | 1 |
| canonical shadow records | 1 |
| duplicate attempts | none |
| canonical runtime exception | none |
| turn provenance | user origin, allowed platform |
| route availability used | `deepseek_flash,mimo_pro`, resolved from the deployment's own configuration |
| JEV service result | success |
| structural canary | PASS |
| single attempt per human turn | PASS |
| non-user turns | zero admitted; the rejection-counter sample for this window is not used as evidence (see below) |
| executing model during the turn | `deepseek-flash` (unchanged) |

The last two rows are the point of the exercise. No non-user turn was admitted — the only
record for the window carries user provenance — and although the router's simulation for this
turn resolved to the alternative provider, the model that actually served the turn did not
change. That is the shadow contract holding under real traffic.

Deliberately, `NON_USER_ADMISSION_CHECK` is reported as **NOT_OBSERVED** rather than PASS:
the rejection counter for the window is not uncontaminated evidence. Running this
repository's offline suites *inside* a configured deployment was found to write the suites'
fixture rejections into the deployment's counter series, because a running gateway exports
its configuration to the child processes that run the tests and one suite did not isolate
the counter path. Both offline suites now redirect their log, state, counter and runtime
paths into a private temporary tree and clear inherited `JEV_*` values; the fix was verified
by an identical before/after snapshot of the production paths. Counter history was not
rewritten — the append-only series was left as it is — and the shadow telemetry that the
routing-quality statistics read was never affected.

`would_execute` followed the canonical configured-availability semantics: the value is
derived from the routes this deployment explicitly declares, and a record may only present
a route as executable if it is configured. The legacy behaviour, which assumed provider
availability from a hard-coded constant, is not in the path.

## Invariants after convergence

| invariant | value |
| --- | --- |
| router mode | shadow |
| auto routing | disabled (no approval key present) |
| executing model | `deepseek-flash` |
| confidence threshold | 0.65 |
| margin threshold | 0.15 |
| JEV timeout | 3s |
| allowed platform | feishu |
| JEV model | `jev-latest` |
| configuration file | unchanged |
| turn-origin integration | pass |
| rejection counters | single series continued, monotonic, no second series |
| append-only history | not restored from a snapshot |

## Result

`FINAL_STATE = CONVERGENCE_VALIDATED`

The canonical shadow integration has been observed handling a real human turn exactly once,
with correct provenance and configured route availability, while the executing model stayed
unchanged.

## What this is not

- **Not an auto-readiness claim.** Auto routing remains disabled and there is no approval
  key. Nothing here grants the router model-selection authority.
- **Not a routing-quality claim.** `G1_REAL_TRAFFIC_SAMPLE` remains **INSUFFICIENT**. One
  observed canary turn proves the integration works; it says nothing about whether the
  recommendations are good.
- **Not a performance or cost claim.** No latency, throughput or saving claim is made.

## Next focus

Routing-quality evaluation on the canonical generation is the next milestone, and it is the
phase that actually tests the project's premise — whether a JEV-style router deserves model
selection authority:

1. canonical-generation human shadow cohort
2. recommendation quality across providers
3. confidence calibration
4. margin calibration
5. false escalation
6. missed escalation
7. timeout and error impact
8. latency trade-off
9. model-cost trade-off
10. real context-size signal
11. prompt-cache signal
12. longitudinal multi-turn sequences
13. two-session empirical concurrency
14. a future bounded-auto safety gate

Auto routing stays disabled throughout.
