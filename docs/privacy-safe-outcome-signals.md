# Privacy-safe outcome signals

Status: **design + read-only audit**. Auto is disabled. Nothing here is deployed, and nothing here
is a routing-correctness label.

This document records what execution-outcome evidence Hermes can actually produce for a single
human turn today, what it cannot, and what a future per-turn outcome record would have to look
like. It is written so that a later phase cannot quietly upgrade a proxy into a verdict.

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

## 5. Historical join coverage

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

## 9. Why prospective instrumentation is required

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
aggregates cannot be attributed to a turn after the fact. The candidate hooks show that a
turn-scoped, content-free terminal record is **feasible to design** — not that it exists.

## 10. Prospective design (design only — do not deploy from this document)

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
python3 tools/outcome_signals.py --generation canonical-dff8b11 --json /path/outside/the/deployment.json
python3 tools/routing_quality.py --generation canonical-dff8b11 --outcome-aggregate /path/aggregate.json
```

`tools/outcome_signals.py` is read-only, offline, generation-aware, refuses to replicate a session
aggregate onto sibling turns, refuses to write inside a deployment, emits no turn or session
identifier, and reports `attribution_quality` with every value it exposes.

The routing-quality integration is optional: without `--outcome-aggregate` the tool's output is
byte-for-byte what it was before. With it, the extra view carries `CORRELATION_NOT_CAUSATION = YES`
plus an explicit list of what it cannot say. Outcome signals are never treated as ground truth by
the routing-quality analyser.

`G1_REAL_TRAFFIC_SAMPLE` remains `INSUFFICIENT`: struggle features do not add sample breadth,
longitudinal evidence, concurrency coverage or context/cache instrumentation.
