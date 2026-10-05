# Routing-quality evaluation

This note fixes the method used to judge whether the shadow router's recommendations are any
good. It exists because two different things are easy to confuse: what the routing service
recommends, and what the router policy would actually execute. Reporting the second as if it
were the first overstates the service and hides the policy's own contribution.

## Two routes per turn

Every shadow record carries both, and they answer different questions:

| field | meaning |
| --- | --- |
| `route` — **DIRECT_JEV_ROUTE** | the route the JEV service recommended for the turn |
| `would_execute` — **POLICY_WOULD_EXECUTE** | the route the router policy would have used, after its confidence and margin gates and after filtering by the routes this deployment declares as available |

`would_execute` is a *policy* output. It depends on thresholds and on configuration, so a
change of threshold changes it without the service having said anything different. Any figure
about model mix must name which of the two it is describing.

## Override taxonomy

When the two differ, the policy has overridden the service. The reason is not inferred from
field names or assumed from the direction of the change: the frozen policy functions
themselves are replayed over each recorded decision, and the reason is classified from that
replay:

| reason | condition, per the policy code |
| --- | --- |
| `LOW_CONFIDENCE_ESCALATION` | the decision was usable but its confidence was below the gate; the policy attributes the turn to the capable route |
| `LOW_MARGIN_ESCALATION` | confidence passed but the top-two probability gap was below the margin gate |
| `CONFIDENCE_AND_MARGIN_ESCALATION` | both gates failed |
| `FAIL_CLOSED` / `FAIL_CLOSED_ESCALATION` | the decision was unusable (for example a timeout); the fail-open policy never lets an unusable decision silently route work to the weaker route |
| `ROUTE_UNAVAILABLE` | the preferred route is not among the routes this deployment declares as available, so the deployment's configured route is used instead, or nothing is claimed |

The replay is checked against the records: if the replayed `would_execute` does not reproduce
what the deployment recorded, the tool reports the fidelity and the taxonomy must be treated
as approximate rather than trusted. A fidelity of 1.0 means the taxonomy describes exactly the
policy that produced the data.

Escalations are additionally split into `LOW_CONFIDENCE_ONLY`, `LOW_MARGIN_ONLY` and `BOTH`,
because those three have different implications for calibration: the first says the service
was unsure, the second says it was nearly split, and the third says both.

## There is no ground truth

`confidence`, `margin`, task features and `would_execute` are **model signals, not correctness
labels**. The telemetry records what the router decided and what the service said; it does not
record whether the route that ran actually solved the task better than the alternative would
have. Therefore:

- an override is neither good nor bad on its own; it is countable, and its cost is
  describable, but its value is not measurable from this data;
- no threshold is "best". Threshold sweeps describe sensitivity, not optimality;
- a single canary turn demonstrates that the integration works, not that the routing is
  correct.

The standing question this phase cannot answer is whether low confidence on a recommendation
for the default route actually predicts turns where the alternative would have materially
improved the outcome enough to justify the switch's cost — including the prompt cache the
switch discards.

## Generation separation is mandatory

Legacy records (`legacy_unversioned`, written before the deployment generation existed) and
canonical records (`canonical-dff8b11` and later) do not agree on the meaning of
`would_execute`, on how route availability is resolved, or on counter paths. Routing-policy
figures are therefore always reported for exactly one generation; asking for "all" yields
record counts only and withholds the routing figures rather than printing a mixed number.
Legacy and canonical records must also never be concatenated into one sequence for
longitudinal analysis.

Fields that stay comparable across generations (same semantics on both sides): confidence,
margin, JEV latency, the direct recommended route, and the recorded task-feature flags — with
the caveat that the legacy variant read route availability from a hard-coded constant, so its
`would_execute` and any route-availability figure are not comparable. Counter totals are never
comparable, for the reason below.

## Known counter contamination

An offline test run once wrote its fixture rejections into the production rejection counter:
a running gateway exports its own environment to child processes, and the suite did not
isolate the counter path. Consequences and policy:

- the event is recorded as **KNOWN_SYNTHETIC_COUNTER_CONTAMINATION** in a content-free sidecar
  ledger (event type, affected source, reconstruction, confidence);
- the raw counter is **not** rewritten, trimmed, restored or "cleaned": append-only history
  stays as it is;
- a raw absolute counter total must therefore never be quoted as a real rejection total. The
  synthetic volume was reconstructed by replaying the pre-fix test revision in an isolated
  scratch tree (deterministic per run), but the contaminated window's aggregate is still marked
  `UNUSABLE_FOR_REAL_REJECTION_RATE`, because a per-run figure cannot be shown to account for
  the whole window and some rejection reasons are also legitimate production reasons;
- no subtractive correction is applied: the tool refuses to produce a "real" total by
  subtracting a synthetic delta.

Shadow routing telemetry was never affected, and the routing-quality statistics read that
telemetry rather than this counter.

## Context and cache signals

Auditing the existing runtime telemetry (schema level, read only) gives:

| signal | status | note |
| --- | --- | --- |
| real context size | partial | session and session×model token totals with time windows exist; there is no per-request context snapshot, and the router's own `dossier_token_estimate` describes the request to the routing service — it is not the conversation context size and must not be reported as one |
| prompt cache | partial | cache read/write tokens exist per session and per session×model; a reuse ratio is derivable per window, but there is no explicit per-turn hit/miss flag |
| tool calls | yes | per-session counts, and per-message tool names |
| tool errors | no | tool results are message content; counting errors would require reading content |
| retries | partial | API call counts exist; provider retries are not recorded as such |
| turn duration | partial | message timestamps allow a derivation |
| final status / cancellation / exception | yes / partial / partial | session end reason plus recorded handoff and compression failures |
| user follow-up timing | yes | derived from message timestamps; no content needed |
| model latency | no | the router records routing-service latency, not provider latency |
| task-completion verdict | no | does not exist anywhere |

So an outcome proxy can be assembled today only from structural signals — abnormal session
end, recorded runtime failures, follow-up gap, tool-call pressure, cache reuse ratio, rewind
— and every one of them is ambiguous on its own. None is a correctness label; they are
candidates for an experimental quality proxy, not a substitute for ground truth.

## Tooling

`tools/routing_quality.py` produces these aggregates: read only, offline, generation aware,
refusing to mix generations, never writing into a deployment path, and emitting no prompt,
dossier, turn identifier, session identifier or tool argument. `tests/test_routing_quality.py`
covers the aggregates and the isolation contract — including the case where the parent
environment already carries deployment paths, which the suite must neither read nor write.
