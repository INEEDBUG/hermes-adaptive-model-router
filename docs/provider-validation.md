# Provider validation

Both routes referenced by the routing criteria were validated before the decision
service was allowed to include them, and the alternative route stayed marked
unavailable (`MIMO_AVAILABLE = False`) until every check had passed. Validation used
provider-reported usage/session metadata rather than configuration strings.

## Routing service contract

| Item | Finding |
|---|---|
| Endpoint | `POST /v1/systemone` with `{state, model, questions}` |
| Question type | literal `"choice"`, answered by an object carrying `choice`, `confidence`, `probabilities` |
| Advertised aliases | `jev-latest`, `jev-preview` (the list exposes aliases, not revisions) |
| Concrete revision | resolved from the response `model` field (observed: a pinned revision such as `jev-1.13.0`) and recorded in telemetry for reproducibility |
| Schema source | the service's own `openapi.json`, treated as the only contract |

The concrete revision is recorded next to every decision precisely because the alias
does not identify the revision that produced it.

## Route A — fast/cost-efficient model (DeepSeek V4.1 Flash)

Validated as the **production path**, i.e. under the gateway's real traffic rather
than in an isolated harness:

| Check | Evidence |
|---|---|
| Text completion | Real completions with usage accounting |
| Reasoning | Reasoning/thinking path exercised through the gateway runtime |
| Tool calling | Real tool executions driven end-to-end by the model |
| Multi-step tool loop | Multi-iteration agent loops with tool-call counters in the session store |
| Coding/debugging | Real files edited and verified by re-running the affected checks |
| Model identity & context metadata | Verified after an upstream model-id normalisation fix, including the effective context metadata reported for the model |
| Error paths | Invalid-model and timeout handling verified; failures surface as clean errors rather than hangs |

## Route B — high-capability model (MiMo V2.6 Pro)

Validated as an **alternative route** through the provider's native integration, with
an explicit matrix. Each item required independent evidence of a real call.

| ID | Check | Result |
|---|---|---|
| M1 | Text completion | PASS — provider/model confirmed from independent usage metadata |
| M2 | Reasoning | PASS — reasoning metadata reported by the API was recorded (no fabricated value) |
| M3 | Single tool call | PASS — a real command executed; tool-call counter advanced |
| M4 | Multi-step tool loop | PASS — a second step depended on the first; ≥2 tool calls, ≥3 API calls |
| M5 | Coding/debugging | PASS — failing baseline reproduced, fix applied, tests re-run independently and passing |
| M6 | Error handling | PASS via injected/mocked paths — invalid credential produced a clean `HTTP 401`, and the failure classifier was verified for 401 / 429 / timeout / 5xx |

Notes on rigour:

- **M4 was re-run.** The first attempt satisfied the letter of "two steps" with a
  single combined command, which does not demonstrate a multi-step loop; the test was
  rewritten so step two depends on step one.
- **M6's "provider unreachable" case is not claimed.** The container's host resolution
  could not be isolated without touching shared networking, so only the injected
  credential-failure path and mocked classification were exercised. Unverified is
  reported as unverified.
- **Fault injection produced no cost.** Rate-limit behaviour was verified with mocked
  responses instead of deliberately triggering real throttling.
- **Test traffic is not production traffic.** Every validation record uses a
  recognisable test turn id or an isolated log directory, and the statistics tool
  excludes it from production counts.

## Production shadow validation

| Check | Result |
|---|---|
| Plugin loaded by a live gateway | Confirmed from the gateway process's own load record |
| Decision recorded for a real user turn | Confirmed — a record whose session resolves to a real messaging platform |
| Executing model unchanged | Confirmed — `actual_model` equals the configured production model in every record |
| Content-free telemetry | Confirmed — prompt text, headers and memory never appear in the log |
| Fail-open under a real failure | Confirmed — one genuine routing timeout occurred during real traffic; the reply was delivered normally and the failure was classified in telemetry |

## What is deliberately not claimed

- No benchmark, cost-saving or latency-improvement figure: the real-traffic sample is
  small by design and is published as distributions rather than headline numbers.
- No claim that automatic switching is enabled, tested or safe under production
  concurrency.
- No claim about a provider outage path that was not actually exercised.
