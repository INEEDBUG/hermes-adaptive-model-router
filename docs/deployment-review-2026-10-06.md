# Prospective outcome telemetry — deployment review (2026-10-06)

Review of the repository candidate *before* any deployment decision. **Nothing here is deployed.**
Production still runs ``canonical-dff8b11`` in ``shadow`` with Auto disabled, and no environment
variable, plugin file, runtime generation or gateway process was touched.

## Blockers found and fixed

| # | Blocker | Why it mattered | Fix |
| --- | --- | --- | --- |
| 1 | ``_note_model()`` used the unique-model list as if it were the last-model sequence, so ``A → B → A`` counted as **1** switch | model-attribution drift would be under-reported for a whole turn | transition logic on ``actual_model_last``, ``models_seen`` demoted to a bounded inventory; the terminal hook now passes through the same logic instead of overwriting ``actual_model_last`` |
| 2 | request de-duplication used the raw ``api_request_id``, which Hermes reuses for every retry of one logical call | an error attempt could swallow the success that follows it, while a legitimate retry could be dropped — with ``EXACT_PROSPECTIVE`` still claimed | identity re-pinned from the source: attempt namespace ``(api_request_id, retry_count)``, response namespace ``(api_request_id, api_call_count)``; suppression and saturation both degrade to ``PARTIAL_PROSPECTIVE`` |
| 3 | the accumulator opened right after ``_admit()``, before the message/marker/dossier/privacy gates | a turn could have a terminal outcome with **no** routing observation, silently mixing two cohorts | cohort boundary moved to just before ``shadow.submit``; ``OUTCOME_COHORT_CONTRACT`` and ``outcome_scope = 'routing_attempt'`` recorded in the schema |
| 4 | the analyzer's terminal coverage could not show unmatched outcomes | a record with no admitted shadow turn would have been averaged into a routing denominator | ``MATCHED_TERMINAL_OUTCOMES`` / ``MISSING_TERMINAL_OUTCOMES`` / ``UNMATCHED_TERMINAL_OUTCOMES`` reported separately with an explicit formula |
| 5 | ``duration_ms`` was unnamed and could be read as user-perceived latency | an analyst could quote it as latency or model speed | ``DURATION_SEMANTIC = observed_execution_duration_ms`` recorded in the schema and documented as accumulator-open → terminal hook |
| 6 | the parser accepted unexpected fields and unknown enums | a content-bearing or malformed row could enter an aggregate | fail-closed ``validate_record``: unknown schema, unexpected field, invalid status/quality/reason, malformed numeric and wrong type all reject; counts by closed reason only, values never echoed |
| 7 | the day file name depended on the process timezone | two hosts could partition one day differently | ``OUTCOME_DAY_PARTITION = UTC`` via ``time.gmtime()``, asserted under four timezones |
| 8 | manifest version ``0.3.0`` vs module ``0.1.0`` | version drift between manifest, library and docs | single version of record, enforced by the new gate; ``0.3.0`` stays untagged (the tag convention is one tag per release: v0.1.0 .. v0.2.0, and nothing is released or deployed this round) |

Retained by design, re-verified: content blindness (structural allow-lists), the reused
``_admit`` boundary and unchanged rejection counters, append-only writing with owner-only
modes, bounded memory with eviction that never fabricates a terminal, and the failure policy
``FAIL_OPEN_FOR_HERMES_EXECUTION + FAIL_CLOSED_FOR_ANALYTICS``.

## Pre-deployment gates

```
tools/check_outcome_hook_contract.py   source-only compatibility gate; exit 3 = fail closed
tests/test_hook_contract.py            gate self-tests + manifest/registration + version consistency
tests/test_deployment_rehearsal.py     frozen export loaded in a fresh process through a fake hook
                                       registry, isolated JEV_LOG_DIR and shadow state, sockets blocked
tests/test_outcome_telemetry.py        170 offline checks over the module and the handlers
```

The gate is a **source** gate: it reads ``/opt/hermes`` and proves the five hooks still exist and
still carry the fields the module needs. It is explicitly not a runtime validation, sends no
request and reads no credential.

## Future deployment delta (reported, not applied)

```
OLD_RUNTIME_SHA             = dff8b117d38f597617497fca5567b0e7ec0bde9a
NEW_RUNTIME_SHA             = <hardening commit, see the report for this round>
OLD_GENERATION              = canonical-dff8b11
NEW_GENERATION              = canonical-<sha7 of NEW_RUNTIME_SHA>
PLUGIN_HOOKS_OLD             = [pre_api_request]
PLUGIN_HOOKS_NEW             = [pre_api_request, post_api_request, api_request_error,
                                post_llm_call, agent_loop_stopped]
NEW_RUNTIME_FILES            = [router/outcome.py]
OUTCOME_LOG_PATH             = <JEV_LOG_DIR>/outcomes/turn-outcome-YYYY-MM-DD.jsonl
ENV_MUTATION                 = JEV_ROUTER_ROOT  (-> the new generation directory)
                               JEV_DEPLOYMENT_GENERATION (-> canonical-<sha7>)
                               UNCHANGED: JEV_AVAILABLE_ROUTES, JEV_SKIP_COUNTER_PATH,
                                          thresholds (0.65 / 0.15 / 3s), platform allowlist
RESTART_REQUIRED             = YES   (hook registration only takes effect in a new process)
RESTART_COUNT                = 1     (one controlled convergence restart; no rollback restart expected)
AUTO                         = NO    (Auto stays disabled and is not part of this delta)
```

Rollback design: restore the old plugin directory, point ``JEV_ROUTER_ROOT`` back at
``generations/canonical-dff8b11``, restore ``JEV_DEPLOYMENT_GENERATION``, and restart once. Routing
telemetry, outcome telemetry and the rejection counter are **append-only and are never restored**;
new outcome records stay in place and remain distinguishable by ``deployment_generation``, and
their ``turn_correlation`` can never collide with the older cohort because it is derived from the
turn id of a different turn.

## Readiness

```
DEPLOYMENT_REVIEW_RESULT = READY
G1_REAL_TRAFFIC_SAMPLE   = INSUFFICIENT (instrumentation does not widen G1)
AUTO_ENABLED             = NO
PRODUCTION_DEPLOYED      = NO
```

``READY`` means the repository candidate passes every gate this round defined; it does **not**
authorise a deployment. The next milestone is an explicit deployment authorisation.
