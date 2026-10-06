# Privacy-safe outcome signals

## Production status — authoritative

```
PRODUCTION STATUS = DEPLOYED_AND_CANARY_VALIDATED
runtime           = 922bd11aa3e019059acfc0195bf7dd1b24bc5e9c
generation        = canonical-922bd11
Router            = shadow
Auto              = disabled (JEV_AUTO_APPROVED absent)
G1                = INSUFFICIENT
NATURAL CANARY    = PASS (real human turn, the first one after the static deployment)
```

**Exact prospective per-turn structural outcomes begin only from `canonical-922bd11` onward.** No
historical generation is re-labelled exact, and nothing in this document is a routing-correctness
label.

Sections 1–12 below are the **historical pre-deployment findings and design record**: they explain
what Hermes could and could not produce *before* this deployment, and why the instrumentation was
required. They are kept so that no later phase can quietly upgrade an argument into a verdict. The
authoritative description of what runs today is the section
*“Prospective telemetry — deployed and canary-validated”*.

## 1. The boundary this phase must never cross

The default model struggling is **not** evidence that the other model would have done better.

```
DEFAULT_MODEL_STRUGGLE  !=  MIMO_WOULD_BE_BETTER
```

The counterfactual question ("would the escalated route have solved it?") is a separate research
problem that needs a real counterfactual. This phase therefore creates:

* no `ROUTING_CORRECTNESS_LABEL`;
* no single scalar "quality score", and no claim that any such number ranks two models.

Signals described here are **features** with an explicit attribution quality. The only signals
allowed to be called hard are explicit failure-class structural events.

## 2. Statements that must survive every later edit

1. A struggle proxy is not routing correctness. A feature that correlates with a bad turn does
   not tell the router what to do.
2. An observed struggle on the default model is not evidence that the other model would have
   succeeded. Without a counterfactual that sentence is unsupported.
3. Session aggregates must not be copied onto the turns of that session, and must never be
   divided by a turn count and reported as a per-turn value.
4. The single-turn-session subset permits an exact join and is **biased**: sessions with exactly
   one admitted turn are not representative of Hermes traffic. It is exploratory only.
5. Content never enters an outcome signal: no prompt, response, tool argument, tool result,
   memory or dossier body.
6. Auto remains disabled. Outcome signals are evidence for a future decision, never an
   authorisation to route live traffic.

## 3. Signal granularity (from the real schema, not from expectations)

`tools/outcome_signals.py` probes the live schema and prints a matrix with, per signal: source,
grain, availability, timestamp availability, turn/session join keys, cumulative-or-event,
mutable-or-append-only, privacy risk and reliability. The load-bearing rows:

| signal | grain | turn join key | cumulative/event | notes |
| --- | --- | --- | --- | --- |
| input / output tokens | `PER_SESSION`, `PER_SESSION_MODEL` | none | cumulative | totals only, no per-request context snapshot |
| cache read / write tokens | `PER_SESSION`, `PER_SESSION_MODEL` | none | cumulative | time window (`first_seen`/`last_seen`) does not say which turn a token belongs to |
| cost | `PER_SESSION`, `PER_SESSION_MODEL` | none | cumulative | pricing may be estimated, not actual |
| api call count | `PER_SESSION`, `PER_SESSION_MODEL` | none | cumulative | exact per-turn counts exist only through the request hook |
| tool call count | `PER_SESSION` | none | cumulative | per-message tool rows exist but carry tool names and arguments |
| rewind count | `PER_SESSION` | none | cumulative | rare, strong negative hint when present |
| end reason | `PER_SESSION` | none | event | lifecycle reason, not a per-turn verdict |
| message timestamps | `PER_MESSAGE` | inferred by window | event | the only structural per-turn timeline that exists historically |
| finish reason | `PER_MESSAGE` | inferred by window | event | `stop` marks a completed turn; `incomplete` / `length` mark truncation |
| runtime failure | `PER_SESSION` | none | event | recorded at session level |
| cancellation / exception termination | not per turn | none | — | only observable prospectively through a live hook |
| request context estimate | `PER_REQUEST` | time window only | event | present on a small minority of requests, with the unknown flag set on many |
| executing-model latency | `PER_REQUEST` | time window only | event | effectively unpopulated in the observed telemetry |
| user follow-up timing | `PER_MESSAGE` (derived) | derived | event | derivable now; ambiguous by nature |

The rule that follows from the table: a session-level value is **not** a per-turn value.

## 4. A human turn, and how reliably its boundary can be found

```
START = an admitted human-origin user turn
END   = the final assistant completion / terminal state belonging to that turn
NEXT  = the next human user message in the same session
```

Measured against the live store (roles, timestamps and status enums only — never content):

* `TURN_START_DETECTABLE = YES` — user-role rows carry a timestamp.
* `TURN_END_DETECTABLE = PARTIAL` — an assistant row with `finish_reason='stop'` marks completion,
  while `tool_calls` marks an intermediate step; interrupted and abnormally exited turns leave no
  completion marker at all.
* `NEXT_USER_DETECTABLE = YES` — next user-role row, or session end.
* `TURN_BOUNDARY_RELIABILITY = MEDIUM` — completions are not 1:1 with user rows (mid-turn steering
  messages are also user rows), and **no per-turn identifier is persisted for the executing
  model's work**, so a turn is inferred from ordering rather than recorded.

The follow-up gap (assistant completion → next human message) is computable and is reported as a
distribution only: `EXPERIMENTAL`, with **no threshold applied**. A 30-second gap is not "bad"; it
is also how normal conversation looks.

## 5. Historical join coverage (pre-deployment audit)

*HISTORICAL PRE-DEPLOYMENT FINDINGS — these are the numbers the read-only audit could join before
`canonical-922bd11` existed. They are not today's production state, and prospective records do not
exist for these generations.*

Reported per deployment generation, never merged (the two generations do not share semantics):

| | legacy_unversioned | canonical-dff8b11 |
| --- | --- | --- |
| admitted human turns | 101 | 3 |
| sessions | 28 | 2 |
| single-turn sessions (exact join) | 4 | 1 |
| multi-turn sessions (no replication) | 24 (97 turns) | 1 (2 turns) |
| max turns in one session | 8 | 2 |

So exact per-turn token, cache, cost and tool aggregates are available for **4 legacy turns and 1
canonical turn** and for nothing else. Everything else is either unjoinable or window-inferred.
`RETROSPECTIVE_STRUGGLE_ANALYSIS = DESCRIPTIVE_ONLY` at best — with a subset this small and this
biased, no incidence or median difference may be presented as evidence.

## 6. Hard signals versus soft features

`HARD_STRUGGLE_SIGNAL_AVAILABLE = PARTIAL`. Hard candidates, judged individually:

| candidate | available now | per-turn exact | false positive | false negative |
| --- | --- | --- | --- | --- |
| explicit runtime failure | PARTIAL | NO | LOW | HIGH |
| exception termination | NO | NO | LOW | HIGH |
| cancellation | NO | NO | LOW | HIGH |
| abnormal end reason | PARTIAL | NO | MEDIUM | MEDIUM |
| compression failure | PARTIAL | NO | LOW | HIGH |
| handoff failure | PARTIAL | NO | LOW | HIGH |
| truncated completion (`incomplete` / `length`) | YES | PARTIAL | LOW | MEDIUM |

Only the last one is per-turn exact-ish, and it is rare. Everything else is session-level, which is
why the hard signal cannot be claimed as reliable.

Soft features — explicitly **not** failures: `tool_call_count`, `turn_duration`, `rewind`,
`user_followup_gap`, `input/output token volume`, `cache reuse`, `API call pressure`, `cost`. Each
carries `expected_direction`, `ambiguity` and `false_positive_risk` in the tool's output. Many tool
calls may mean a hard task, or a task that simply needs many tools.

## 7. Per-turn exactness, answered

```
INPUT_TOKENS_PER_TURN_EXACT   = NO
OUTPUT_TOKENS_PER_TURN_EXACT  = NO
CACHE_READ_PER_TURN_EXACT     = NO
CACHE_WRITE_PER_TURN_EXACT    = NO
COST_PER_TURN_EXACT           = NO
PROMPT_CACHE_HIT_PER_TURN     = NO
```

with one documented exception: `SINGLE_TURN_SESSION_EXACT_JOIN = YES` — when a session contains
exactly one admitted human turn in the observation window, the session aggregate *is* that turn's
aggregate.

```
CACHE_SWITCH_COST_MEASURABLE_NOW = PARTIAL
```

`CACHE_REUSE_CONTEXT` (`PER_SESSION_MODEL`: cache read, input, cache write, api calls, first/last
seen) supports a session-window reuse ratio per model — context only. It does **not** support the
claim "switching model loses X tokens of cache for this turn", because no per-request delta is tied
to a turn. That claim must stay unmade until prospective telemetry exists.

## 8. `DEFAULT_MODEL_STRUGGLE_VECTOR_V1`

```json
{
  "hard_failure": "enum | null",
  "turn_duration_ms": "number | null",
  "tool_call_count": "number | null",
  "rewind_delta": "number | null",
  "followup_gap_ms": "number | null",
  "input_tokens": "number | null",
  "output_tokens": "number | null",
  "cache_read_tokens": "number | null",
  "cache_write_tokens": "number | null",
  "cost": "number | null",
  "attribution_quality": "EXACT | PARTIAL | UNAVAILABLE"
}
```

No scalar score. No prompt, response, tool argument, tool result, memory or dossier field may ever
be added. `attribution_quality` is mandatory and is `EXACT` only for the single-turn subset;
`PARTIAL` for window-inferred values such as duration and follow-up gap; `UNAVAILABLE` for anything
that has no per-turn source at all.

`DEFAULT_MODEL_STRUGGLE_SIGNAL_AVAILABLE = PARTIAL`.

## 9. Why prospective instrumentation was required before `canonical-922bd11`

*HISTORICAL PRE-DEPLOYMENT FINDINGS — this section records why a per-turn record was needed at all.
The record it argues for now exists and is deployed; see the authoritative section below.*

`RELIABLE_POST_TURN_HOOK_AVAILABLE = PARTIAL`. There is no single hook guaranteed for every turn.
From the read-only source audit:

| hook | when it fires | joins a turn | content-free metrics | risk |
| --- | --- | --- | --- | --- |
| `post_llm_call` | once per turn, after the tool loop | YES (`turn_id`, `session_id`, `task_id`) | NO payload metrics (payload carries user/assistant text) | skipped on some abnormal early exits; a recorder must be content-blind |
| `post_api_request` | once per physical provider request, turn-scoped | YES (`turn_id`, `api_call_count`) | YES (usage token buckets including cache read/write, `finish_reason`, `api_duration`) | high volume; aggregate, never persist per event |
| `api_request_error` | once per failed request | YES | YES (`status_code`, error type, `retry_count`, `retryable`) | store enums only, never an error body |
| `pre_api_request` | once per request, before sending | YES | YES (`message_count`, `tool_count`, `approx_input_tokens`) | an estimate, not a measurement |
| `agent_loop_stopped` | when a running turn is interrupted | PARTIAL (session key, platform, reason) | YES | interruption only |
| `on_session_end` | documented as a turn/session terminal event carrying `completed` / `failed` / `interrupted` and `turn_exit_reason` | YES per the documented payload | YES | the plugin-visible fire site could not be confirmed from the read-only audit, so it is reported as **documented, not verified** |
| context-engine `on_turn_complete` | per turn, from the finalization seam | YES (`turn_id`, `api_call_count`, `interrupted`, `failed`, `turn_exit_reason`) | YES (usage shape) | internal extension point (engine override only); abnormal early returns skip it |

Historically, none of this was recorded per turn, which is exactly why the token and cache
aggregates cannot be attributed to a turn after the fact. At audit time the candidate hooks showed
only that a turn-scoped, content-free terminal record was **feasible to design** — it did not exist
yet, which is what the later deployment implemented (and what the authoritative section below
describes).

## 10. Prospective design (historical pre-deployment design record)

*This is the design as written before deployment. The implemented schema, its contract and its
production status are described in the authoritative section below; nothing here authorises a
deployment.*

`turn-outcome-v1`: append-only, at most one terminal record per admitted human turn.

```
schema_version, deployment_generation, platform, turn_origin, actual_model,
terminal_status, duration_ms,
input_tokens_delta, output_tokens_delta, cache_read_delta, cache_write_delta, cost_delta,
tool_call_delta, rewind_delta, runtime_failure(bool), attribution_quality
```

* Values are aggregated from the per-request hooks during the turn and finalised at the turn
  terminal; a record is written only when a terminal is observed, and the record states its
  `attribution_quality`.
* If no terminal is observed, no record is written — a missing record must stay distinguishable
  from a successful turn, so absence is never "success".
* The join identifier may use the existing local private turn correlation, but no identifier may
  enter a public aggregate.
* The follow-up gap is unknown at turn completion, so an outcome record must never be mutated to
  append it. Either derive it at query time or append a separate `turn-followup-v1` record.
* Nothing in this design authorises enabling Auto.

## 11. What is still missing

Missing for genuinely per-turn structural outcomes: a turn-start cumulative usage snapshot, a
turn-end cumulative usage snapshot, per-turn cache read/write deltas, a per-turn token delta,
execution duration for the executing model, an explicit terminal status, and a structured runtime
exception boolean. Also missing: a per-request context snapshot tied to a turn, a per-turn cache
hit/miss flag, provider retry counts in the store, executing-model latency, and any task-completion
verdict.

## 12. Tooling

```
python3 tools/outcome_signals.py --generation all
python3 tools/outcome_signals.py --generation canonical-922bd11 --json /path/outside/the/deployment.json
python3 tools/routing_quality.py --generation canonical-922bd11 --outcome-aggregate /path/aggregate.json

Use the generation you actually mean: prospective per-turn outcome records exist only from
`canonical-922bd11` onward, so a prospective analysis that names an older generation is asking about
a cohort that has no such records. Historical cohorts stay addressable by naming them explicitly
(for example `--generation legacy_unversioned`).
```

`tools/outcome_signals.py` is read-only, offline, generation-aware, refuses to replicate a session
aggregate onto sibling turns, emits no turn or session identifier, and reports `attribution_quality`
with every value it exposes. Its write rule names the hazard rather than banning a directory tree:
it will not write inside the telemetry/log directory in use, or inside the deployment's `logs/` tree,
or onto the state database or the plugin tree, and it will not overwrite a file that already exists.
A fresh aggregate under the caller's own report directory is theirs to place.

The routing-quality integration is optional: without `--outcome-aggregate` the tool's output is
byte-for-byte what it was before. With it, the extra view carries `CORRELATION_NOT_CAUSATION = YES`
plus an explicit list of what it cannot say. Outcome signals are never treated as ground truth by
the routing-quality analyser.

`G1_REAL_TRAFFIC_SAMPLE` remains `INSUFFICIENT`: struggle features do not add sample breadth,
longitudinal evidence, concurrency coverage or context/cache instrumentation.

# Prospective telemetry — deployed and canary-validated

**Deployed to production and validated on a real natural human turn.** Production runs generation
`canonical-922bd11` (runtime SHA `922bd11aa3e019059acfc0195bf7dd1b24bc5e9c`) in `shadow` with Auto
disabled and `JEV_AUTO_APPROVED` absent; the plugin registers the five hooks described below.

The first real human turn after that deployment produced exactly one routing observation and exactly
one matching `turn-outcome-v1` record: `outcome_scope = routing_attempt`,
`terminal_status = completed`, `attribution_quality = EXACT_PROSPECTIVE`, no degradation reason,
content-blind, no model switch. Exact per-turn structural outcome evidence therefore accumulates
**from `canonical-922bd11` onwards**; earlier generations stay inexact and are never re-labelled.

```
turn-outcome-v1              = DEPLOYED
generation                   = canonical-922bd11
natural canary               = VALIDATED
attribution                  = EXACT_PROSPECTIVE when no degradation reason is present
                               (PARTIAL_PROSPECTIVE otherwise, with a closed reason)
MISSING_TERMINAL_IS_SUCCESS  = NO
OUTCOME_SCOPE                = routing_attempt
content blind                = YES
Router shadow                = YES
Auto                         = NO
```

Standing constraints of that deployment: the router remains in `shadow` mode, Auto stays disabled
(`JEV_AUTO_APPROVED` absent, no `JEV_AUTO*` key exists), and `G1_REAL_TRAFFIC_SAMPLE` remains
`INSUFFICIENT` — outcome telemetry adds structural per-turn facts, not sample breadth or a
correctness label. `DEFAULT_MODEL_STRUGGLE != ROUTING_CORRECTNESS` and
`DEFAULT_MODEL_STRUGGLE != MIMO_WOULD_BE_BETTER`: none of these fields is a verdict on answer
quality, and none of them proves that a counterfactual model would have succeeded.

Read this part with the following consequences in mind:

* per-turn exact figures start existing only **from `canonical-922bd11` onwards**; nothing here makes
  the historical records exact retroactively;
* `missing terminal != success` — a turn whose terminal hook was never observed leaves no record,
  and analytics must keep the gap visible (`ADMITTED_SHADOW_TURNS - TERMINAL_OUTCOMES`);
* billed usage is not context size: `api_input_tokens_sum` sums the **uncached** input bucket over
  the physical requests of a turn, and a tool loop repeats the context on every request;
* `DEFAULT_MODEL_STRUGGLE != MIMO_WOULD_BE_BETTER` — the candidate records structural execution
  facts and can never express a counterfactual model win.

## Hook payload contract (re-pinned from the Hermes source, not from prose)

Every cell states `PRESENT`, `ABSENT` or `CONDITIONAL` for the payload the hook actually receives.

| field | pre_api_request | post_api_request | api_request_error | post_llm_call | agent_loop_stopped |
| --- | --- | --- | --- | --- | --- |
| `turn_id` | PRESENT | PRESENT | PRESENT | PRESENT | **ABSENT** |
| `session_id` | PRESENT | PRESENT | PRESENT | PRESENT | **ABSENT** (`session_key` only) |
| `task_id` | PRESENT | PRESENT | PRESENT | PRESENT | ABSENT |
| `platform` | PRESENT | PRESENT | PRESENT | PRESENT | PRESENT |
| `turn_origin` | PRESENT (the gateway's structural label) | **ABSENT** | **ABSENT** | **ABSENT** | **ABSENT** |
| `model` | PRESENT | PRESENT (`response_model` too) | PRESENT | PRESENT | ABSENT |
| `api_call_count` | PRESENT | PRESENT | PRESENT | ABSENT | ABSENT |
| `retry_count` | PRESENT | ABSENT | PRESENT (`max_retries`, `retryable` too) | ABSENT | ABSENT |
| `usage` | ABSENT | PRESENT (normalized buckets) | ABSENT | ABSENT | ABSENT |
| `finish_reason` | ABSENT | PRESENT | ABSENT | ABSENT | ABSENT |
| `api_duration` | ABSENT (`started_at` only) | PRESENT | PRESENT | ABSENT | ABSENT |
| status / error metadata | `retry_count` only | `assistant_content_chars`, `assistant_tool_call_count` | `status_code`, `reason`, `retryable`, error type (CONDITIONAL) | ABSENT | `reason`, `invalidation_reason` |
| content-bearing keys | `user_message`, `conversation_history`, `request_messages`, `system_prompt`, `request` | `response`, `assistant_message` | `error`, `request` | `user_message`, `assistant_response`, `conversation_history` | none |

Two facts drive the design:

* `turn_origin` exists **only** on `pre_api_request`, so the accumulator is created there, on the
  plugin's existing admission; the other hooks can only *resolve* a turn, never admit one.
* `agent_loop_stopped` carries a session key and no turn id, so an interruption can only be
  attributed when exactly one open accumulator matches that key.

`CONTENT_BLIND = YES`: the handlers copy only allow-listed keys out of the payload, so even though
the content-bearing keys above are present in `kwargs`, nothing here reads, copies, hashes,
inspects or serializes them.

## Normalized usage semantics

`agent/usage_pricing.normalize_usage()` maps every provider shape onto one canonical shape, so the
buckets mean the same thing regardless of provider:

```
input_tokens       = the UNCACHED portion (Anthropic reports it directly; Chat/Codex totals
                     include cached tokens and the cache buckets are subtracted back out)
cache_read_tokens  = prompt-cache hits
cache_write_tokens = prompt-cache writes
output_tokens      = generated tokens
prompt_tokens      = input_tokens + cache_read_tokens + cache_write_tokens   (derived)
total_tokens       = prompt_tokens + output_tokens                           (derived)
```

Therefore:

```
API_INPUT_TOKENS_SUM          = the uncached input bucket summed over a turn's physical requests
API_INPUT_TOKENS_MAX_REQUEST  = the largest single request's uncached input bucket
API_PROMPT_TOKENS_MAX_REQUEST = the largest single request's full consumed context
                                (input + cache_read + cache_write), recomputed by this candidate
MAX_REQUEST_INPUT_CAN_APPROXIMATE_CONTEXT = PARTIAL
```

`PARTIAL` and not `YES`: a request's `prompt_tokens` is the tokens that request consumed, which is
close to its context size but is not a snapshot of it, and the *uncached* input alone says nothing
about context size. Neither number may be called a ground-truth context snapshot. One provider
caveat is recorded in the source itself: on MiniMax-M3's Anthropic wire `cache_read` carries a
constant floor and is not a reliable hit signal.

## Turn correlation

```
TURN_CORRELATION_SCHEME = sha256(structural turn_id), full 64-hex digest, local join only
```

The digest is computed from the structural `turn_id` and from nothing else — not from a prompt, a
message, or a dossier. Raw identifiers are not written to the record. The digest is private and
local: it is not an identity, it does not enter a public aggregate, and an analyzer that wants to
join it to routing telemetry re-hashes the shadow record's `turn_id` locally instead of reading an
identifier out of the telemetry.

## Accumulator state machine

```
admit (pre_api_request, first call of the turn)  -> OPEN
OPEN + post_api_request      -> OPEN   (request counters, usage buckets, duration, finish reason, model)
OPEN + api_request_error     -> OPEN   (error/retry counters only; never a terminal)
OPEN + post_llm_call         -> COMPLETED -> at most one record written -> accumulator dropped
OPEN + agent_loop_stopped    -> INTERRUPTED -> record written only if exactly one open turn
                                 matches the session key; then dropped
terminal/unknown turn + any hook -> ignored (no second record, no rewrite)
never observed terminal     -> NO RECORD (MISSING_TERMINAL, never success)
```

Bounded memory: at most `_MAX_OPEN = 64` open accumulators, at most 512 remembered request ids per
turn and at most 8 model labels. Eviction drops the **oldest unfinished** accumulator and never
writes a record for it.

## Record schema

```
schema_version              "turn-outcome-v1"
deployment_generation       content-free generation label
platform / turn_origin      closed labels from the boundary
turn_correlation            sha256 digest (local join only)
actual_model_first / _last / model_switch_count
terminal_status             "completed" | "interrupted"   (closed enum)
duration_ms                 first seen -> terminal
api_request_count / api_success_count / api_error_count / retry_count_observed
api_input_tokens_sum / api_output_tokens_sum
api_cache_read_tokens_sum / api_cache_write_tokens_sum
api_input_tokens_max_request / api_output_tokens_max_request
api_cache_read_tokens_max_request / api_cache_write_tokens_max_request
api_prompt_tokens_max_request
api_duration_ms_sum / api_duration_ms_max
finish_reason_last          short enum token or null
runtime_error_observed / interrupted_observed
attribution_quality         "EXACT_PROSPECTIVE"
```

`terminal_status` stays a two-value enum. `failed` and `cancelled` are absent because no hook
proves them today: a request error does not end a turn, and `agent_loop_stopped` reports an
interruption. A wrong classification is worse than a missing record, so
`MISSING_TERMINAL_IS_SUCCESS = NO`.

## Failure policy

```
OUTCOME_TELEMETRY_FAILURE_POLICY = FAIL_OPEN_FOR_HERMES_EXECUTION + FAIL_CLOSED_FOR_ANALYTICS
```

Recording can never raise into a turn, block a request, or change model execution; every handler
returns `None` and swallows its own failure. Analytics can never read absence as success: the
analyzer reports `MISSING_TERMINAL_OUTCOMES` against the shadow-turn denominator.

Writing is append-only JSONL under `<JEV_LOG_DIR>/outcomes/turn-outcome-YYYY-MM-DD.jsonl` — a
separate file per UTC day, never inside `shadow-*.jsonl`, because the two schemas differ and mixing
them would make either unreadable by its own tooling. Lines are appended with `O_APPEND` (no
truncation, no rewrite), the writer is thread-safe, and the modes request owner-only access.

## Future deployment delta (reported, not applied)

```
PLUGIN_HOOKS_TO_ADD = post_api_request, api_request_error, post_llm_call, agent_loop_stopped
                      (pre_api_request already registered)
NEW_RUNTIME_FILES   = router/outcome.py
NEW_TELEMETRY_PATH  = <JEV_LOG_DIR>/outcomes/turn-outcome-YYYY-MM-DD.jsonl
NEW_ENV_KEYS        = none — the existing JEV_LOG_DIR already defines the telemetry root, and a
                      separate subdirectory is enough to keep the schemas apart
RESTART_REQUIRED    = YES — a plugin hook-registration change only takes effect on a new process,
                      so a future deployment needs a new frozen runtime generation plus one
                      controlled official restart
```

Nothing in this section authorises enabling Auto, and nothing here is deployed.

## Hardening pass (deployment review)

The first implementation carried four correctness defects that only bite a *future* deployment.
They were fixed in the repository; the semantics below are the ones the record means from now on.

### 1. Request identity — read from the source, not assumed

``agent/conversation_loop.py`` assigns the id once per outer-loop iteration, *before* the retry
loop::

    s.api_request_id = agent._current_api_request_id = f"{s.turn_id}:api:{s.api_call_count}"

``_run_api_retry_loop`` (``while retry_count < max_retries``) never reassigns it, while
``build_api_request`` fires ``pre_api_request`` on every pass. So::

    API_REQUEST_ID_SCOPE        = LOGICAL_CALL
    RETRY_REUSES_API_REQUEST_ID = YES
    PHYSICAL_ATTEMPT_IDENTITY   = (api_request_id, retry_count)   # observable on pre / error only

Two namespaces are therefore kept apart, which is what stops an error hook from swallowing the
success that follows it::

    attempt namespace  (api_request_id, retry_count)     shared by pre_api_request / api_request_error
    response namespace (api_request_id, api_call_count)  post_api_request

### 2. Counting model and the "never silently false" rule

```
api_logical_call_count = distinct api_request_id
api_request_count      = distinct attempts observed (+ a success whose attempt hook was unseen)
api_error_count        = distinct failed attempts
api_success_count      = distinct successful responses
retry_count_observed   = attempts observed with retry_count > 0
```

``DUPLICATE_HOOK_POLICY = SUPPRESS_DUPLICATE_AND_DEGRADE`` and
``REQUEST_DEDUP_SATURATION_POLICY = SATURATE_THEN_DEGRADE``: a repeated key inside one namespace is
suppressed, and past capacity the key sets stop growing. **Both paths degrade the turn** to
``PARTIAL_PROSPECTIVE``, so a suppressed or unprovable count can never be presented as exact::

    EXACTNESS_NEVER_SILENTLY_FALSE = YES

A cross-namespace or cross-kind sighting of the *same* attempt (pre then error) is expected: it is
neither double counted nor degraded. Verified by a 512-attempt saturation fixture plus duplicate
hooks after saturation, and by an error → retry → success fixture where all four counters stay exact.

### 3. Outcome cohort equals the routing-attempt cohort

```
OUTCOME_COHORT_CONTRACT = one accumulator == one turn that reached an actual canonical routing
                          observation attempt (outcome_scope = 'routing_attempt')
```

The accumulator opens only after the human-provenance gate, a structurally usable message, the
internal marker invariant, a built dossier and the privacy decision — and immediately before
``shadow.submit``. A turn that stops earlier (empty message, marker violation, privacy fallback)
has **no outcome record at all**, so it can never be an unmatched outcome dragging a denominator
down. The analyzer reports three separate numbers instead of one mixed one::

    ADMITTED_SHADOW_TURNS           shadow observations of the generation that reached a routing attempt
    MATCHED_TERMINAL_OUTCOMES       records whose correlation hashes back to such a turn
    MISSING_TERMINAL_OUTCOMES       ADMITTED_SHADOW_TURNS - matched        (never success)
    UNMATCHED_TERMINAL_OUTCOMES     records matching no admitted turn, shown explicitly, never pooled

``TERMINAL_COVERAGE_FORMULA = MATCHED_TERMINAL_OUTCOMES / ADMITTED_SHADOW_TURNS`` — ``len(all rows)``
is never used as the matched count.

### 4. Duration, switches, quality and the day partition

```
DURATION_SEMANTIC   = observed_execution_duration_ms
                      accumulator open (post-admission, pre-submission) -> observed terminal hook;
                      never user-perceived latency, never the full arrival->response wall time,
                      never model latency
MODEL_SWITCH_COUNT  = TRANSITION count: increments exactly when the newly observed model differs
                      from actual_model_last; models_seen is only a bounded unique inventory and
                      never decides the count (A->B->A is 2, not 1), and the terminal hook passes
                      through the same logic instead of overwriting actual_model_last
ATTRIBUTION_QUALITY = closed enum [EXACT_PROSPECTIVE, PARTIAL_PROSPECTIVE]; reasons are a closed
                      enum [duplicate_hook_event, request_identity_saturated] — no free-form string
OUTCOME_DAY_PARTITION = UTC (time.gmtime(); independent of the process timezone)
```

Analyzer guarantees: exact and partially attributed turns are reported separately and never pooled
into one number; the parser rejects (rather than coerces) an unknown schema, an unexpected field, an
invalid terminal status, an invalid attribution quality, a non-enum degradation reason, a malformed
numeric or a wrong type, and prints **counts by closed reason only** — never the offending value.

### 5. Pre-deployment gates in the repository

```
tools/check_outcome_hook_contract.py   source-compatibility gate (exit 3 = fail closed):
                                       required hooks present, load-bearing payload fields present,
                                       OPTIONAL vs REQUIRED distinguished, manifest hooks ==
                                       register(ctx), version of record consistent.
                                       LIVE_REQUEST = NO, CREDENTIALS_USED = NO, and it is explicitly
                                       NOT a runtime validation.
tests/test_hook_contract.py            proves the gate passes on a complete fixture tree, fails on a
                                       dropped hook or a dropped load-bearing field, and passes on
                                       the installed Hermes source when one is present
tests/test_deployment_rehearsal.py     exports the candidate (git archive HEAD, plus a worktree
                                       overlay while uncommitted), loads it in a fresh process through
                                       a fake hook registry, isolated JEV_LOG_DIR and shadow state,
                                       with sockets blocked: 5 hooks register, the routing observation
                                       is unchanged, one schema-valid record is appended, mode stays
                                       shadow, Auto never enabled, and nothing is installed
```

Version of record: ``plugin/plugin.yaml`` and ``router/__init__.py`` carry the same number, asserted
by the gate and the suite. The repository tags releases (v0.1.0 .. v0.2.0, one tag per release);
``0.3.0`` is deliberately untagged: it is the deployed-and-validated telemetry generation, but no
release is published for it and none of the milestone work creates a tag.
