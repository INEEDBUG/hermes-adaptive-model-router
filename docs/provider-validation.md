# Provider validation

Both routes referenced by the routing criteria were validated before the decision
service was allowed to use them. That fact is configuration, not a constant: a route
counts as executable only when the deployment names it in `JEV_AVAILABLE_ROUTES`, whose
public default is **empty** — so a shadow record can never imply a provider that was
not validated here (with nothing configured, `would_execute` is `null`). Validation
used provider-reported usage/session metadata rather than configuration strings.

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

## Human-origin scope of a validation run

The router only observes human-origin turns. Internal, subagent, background and continuation turns are rejected before Routing Dossier construction.

Two structural conditions are evaluated before any dossier is built, and both must hold:
the turn's `platform` must be in `JEV_ALLOWED_PLATFORMS` (gate 1), and its `turn_origin`
must be `user` (gate 2, which is authoritative). Gate 2 is what makes gate 1 meaningful:
an internal notification, a background-review fork, a compaction continuation or a
subagent turn can inherit the platform label of the session that spawned it, so a platform
allowlist on its own does not describe *who* sent the turn. `turn_origin` is a structural
label supplied by the gateway (see `patches/hermes-v0.21.5-turn-origin.patch`); it is never
inferred from message text here, and a missing, empty or unrecognised label is rejected
rather than defaulted to `user`.

**A validation run and the shadow observation period therefore count the same traffic
class.** Auxiliary, subagent, background and continuation turns are excluded *before* a
dossier exists, so a route validated against this repository's methodology is validated
against the traffic the router will actually observe, and no result here is diluted by
machine-generated turns. The rejection itself is counted locally and content-free (`date`,
`platform`, `turn_origin`, `reason`, `count` only); the counting write path has no message
parameter, so no turn content can reach it.

Until the human-origin sample is large enough to evaluate, **no routing-quality claim is
supported** — not accuracy, not cost, not latency, and not a switching benefit. Automatic
switching is not implemented in this release, production mode remains `shadow`, and nothing
has been observed in auto mode.

## How the suites are executed

| Run | Checks |
|---|---|
| `python3 tests/test_router.py` (default) | 39/39 — pure offline, no network, no credential |
| `RUN_LIVE_TESTS=1 python3 tests/test_router.py` | 46/46 — adds the live routing group |
| `python3 tests/test_state.py` | 26/26 — kill-switch resolver + initialiser boundary, offline |
| `python3 tests/test_secret_scan.py` | 24/24 — scanner controls: positive, negative, adversarial |
| `bash tests/test_installer.sh` | 36/36 — stand-in `hermes` CLI, target vs default home, existing state preserved |
| `python3 tests/test_turn_boundary.py` | 37/37 — human-turn provenance boundary (dual gate, fail-closed paths, per-turn counting, concurrency isolation, content-free telemetry), offline, no network, no credential |

The live group is **opt-in by flag, not by credential discovery**: a credential merely
being present on the machine never causes an external call, so the default run is safe
anywhere. CI (GitHub Actions) runs every offline run in the table above (the live group
stays opt-in by flag), the state initialiser in `--dry-run` mode, the dependency-free
secret scan and `tools/check_turn_origin_patch.py` — all offline and network-free, with no
secrets configured. The drift check is invoked so that it must *fail* when it cannot see a
Hermes checkout (exit `3`, never a silent pass), which is also the state in which the
router stays fail-closed. The scan is built to fail in the same spirit: findings exit 1,
and objects left unscanned (over the size cap) exit 3 rather than reporting clean.

## What is deliberately not claimed

- No benchmark, cost-saving or latency-improvement figure: the real-traffic sample is
  small by design and is published as distributions rather than headline numbers.
- No routing-quality conclusion of any kind, because the human-origin sample is still
  accumulating and is not yet large enough to support one.
- No claim that automatic switching is enabled, tested or safe under production
  concurrency — it is not implemented, nothing was observed in auto mode, and validity of
  the shadow decisions says nothing about the safety of acting on them.
- No claim about a provider outage path that was not actually exercised.
