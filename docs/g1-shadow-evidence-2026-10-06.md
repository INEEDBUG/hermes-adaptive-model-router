# G1 shadow evidence snapshot

Aggregated, content-free metadata only: no turn ids, session ids, prompts, message
text, Routing Dossiers, tool arguments, host or filesystem details, credentials or raw
telemetry lines. Every number below is a count, a percentage or a percentile.

## Scope

- **Shadow only.** Automatic provider switching is not implemented and not enabled:
  decisions are recorded, execution is unchanged.
- **Human-origin Feishu traffic only** — every record passed both eligibility gates
  (`platform` in the deployment's allow-list **and** `turn_origin == "user"`) before a
  dossier existed. Non-human origins are counted separately and produce no record and no
  routing call.
- **Production / test separation.** A record counts as production traffic only when the
  session embedded in its turn id exists in the Hermes session database with a real
  messaging platform as its source. Manual fault-injection runs, CLI one-shot sessions
  and subagent sessions are bucketed as excluded. The excluded buckets are reported with
  the statistics so the separation is auditable, not asserted.
- **Snapshot date:** 2026-10-06. The underlying log is append-only and live traffic
  continues, so counts grow after this snapshot; the figures here describe the state
  reconciled on that date.
- **Not a claim of auto readiness.** This document publishes distributions. It does not
  assert that the auto decision rule has been validated, and it is not approval to enable
  automatic routing.

## Sample

| Metric | Value |
|---|---|
| Real human shadow decisions (snapshot) | **122** |
| Successful routing decisions | **116** |
| Timeouts | **6** |
| Records carrying a provenance label (`platform` + `turn_origin`) | 93 |
| Pre-v0.2.0 legacy records without provenance labels | 29 |

Excluded from the production cohort by construction: CLI one-shot sessions (11),
subagent sessions (10), manual fault-injection runs (3).

The 29 unlabelled records predate the provenance boundary and belong to the historical
sample. The boundary is verified fail-closed today (missing, empty or unrecognised
`turn_origin` is rejected), but provenance **cannot be added retroactively** to records
written before the label existed — they are reported here as legacy history, never
re-interpreted as verified human turns.

## Route distribution

Among the 116 successful decisions:

| Route | Count | Share of successful decisions |
|---|---|---|
| `mimo_pro` | 76 | 65.5% |
| `deepseek_flash` | 40 | 34.5% |

## Confidence distribution

| Percentile | Value |
|---|---|
| p50 | 0.66 |
| p75 | 0.91 |
| p90 | 0.95 |
| p95 | 0.98 |

(n = 116 successful decisions)

## Probability-margin distribution

Margin is `|p(deepseek) − p(mimo)|` as recorded with the decision.

| Percentile | Value |
|---|---|
| p50 | 0.66 |
| p75 | 0.92 |
| p90 | 0.94 |
| p95 | 0.98 |

(n = 116)

## JEV latency

| Percentile | Value |
|---|---|
| p50 | 1180 ms |
| p75 | 1721 ms |
| p90 | 2489 ms |
| p95 | 3013 ms |

(n = 122 decisions; max 3861 ms)

## Error envelope

| Class | Count |
|---|---|
| `success` | 116 |
| `timeout` | 6 |
| other routing error classes in the human-origin cohort | 0 |

The record schema carries `success` **and** `error` for the same decision, so these are
two views of one population, not two failure lists: the 6 timeouts are exactly the 6
decisions with `success = false` (116 + 6 = 122). They must not be added together and
reported as 12 independent failures. Timeouts are JEV-side decision timeouts; the
executing model was unaffected, as shadow mode never changes execution.

Other error classes do appear in the log as a whole (`http_401`, a connection-refused
URL error, and two further timeouts), but every one of them belongs to the excluded
test / one-shot / subagent buckets — none occurred in the human-origin cohort.

## Near threshold

Definition: `0.60 ≤ confidence < 0.70` **or** `0.10 ≤ margin < 0.20` — i.e. decisions
sitting inside ±0.05 of the deployment's shadow thresholds (confidence 0.65, margin 0.15).

| Metric | Count |
|---|---|
| Near threshold (definition above) | **26** |
| Confidence < 0.65 (below the shadow threshold) | 56 |
| Confidence < 0.75 (below the design rule's stickiness bar) | 71 |
| Margin < 0.15 (below the shadow threshold) | 9 |
| Margin < 0.30 (below the design rule's stickiness bar) | 24 |

## Task-feature aggregates

Content-free boolean features recorded with each decision (n = 122; a decision can carry
several):

| Feature | True |
|---|---|
| `tool_use` | 121 |
| `long_context` | 114 |
| `destructive_action` | 110 |
| `production_change` | 95 |
| `coding` | 93 |
| `shell` | 62 |
| `debugging` | 24 |
| `research` | 22 |

Task length: long 114, medium 3, short 5.

## Important limitation: `would_execute`

| `would_execute` | Count |
|---|---|
| `mimo_pro` | 112 |
| `deepseek_flash` | 10 |

> **THIS IS NOT CANONICAL v0.2.0 WOULD_EXECUTE EVIDENCE.**
>
> The observed deployment is a private, never-committed variant of this repository that
> still derives route availability from a hard-coded flag (`MIMO_AVAILABLE = True`). The
> public v0.2.0 line instead restricts `would_execute` to the routes an operator named in
> `JEV_AVAILABLE_ROUTES` and reports `null` when nothing is configured. The two semantics
> are not interchangeable, so these counts describe **historical production behaviour
> only**.
>
> This field must not be used for auto approval, predicted savings, future route policy,
> or as evidence that G1 has passed. See `docs/production-reconciliation.md`.

## Observations that constrain the auto rule

- **Confidence is bimodal, not tight.** A large share of decisions sit far above the
  design rule's stickiness bar (p75 = 0.91), while 56 of 116 successful decisions sit
  *below* the shadow threshold and 71 below the 0.75 bar the design rule requires. A rule
  that demands ≥ 0.75 would therefore rarely fire on this traffic — an observation about
  sample shape, not a routing-quality verdict.
- **Margins carry the same shape** (24 of 116 below 0.30), and 26 decisions sit within
  ±0.05 of a threshold, which is where a small parameter change would flip the outcome.
- **Latency is small relative to a turn** (p95 = 3.0 s) but non-zero, and 6 timeouts
  show the decision path can fail closed without touching execution.

## G1 decision

```
G1_REAL_TRAFFIC_SAMPLE = INSUFFICIENT
```

Reasons, stated as facts about this snapshot:

1. **7 sampled days only**, with gaps (no decisions on several days in the window).
2. **Uneven day distribution** — the largest single day holds 44 of 122 decisions (36%),
   so the sample is not a stationary slice of traffic.
3. **No longitudinal consecutive-turn analysis** — the design rule depends on
   "challenger wins ≥ 2 consecutive turns" and on cooldown per session; that sequence
   structure has not been extracted or evaluated.
4. **No real context-size signal** — `dossier_token_estimate` measures the routing input
   only, never the agent's live conversation context.
5. **No prompt-cache signal** — the switching cost that dominates the design's sticky rule
   is not measured by current telemetry at all.
6. **Production / repository `would_execute` semantics differ** (see above), so the
   field cannot be used to evaluate the public rule as written.

Supporting evidence is not proof. The sample is large enough to describe what shadow
mode observed; it is not yet sufficient to validate an execution-changing decision rule,
and G2–G4 remain not started. Automatic routing stays off.
