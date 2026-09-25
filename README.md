# hermes-adaptive-model-router

A privacy-preserving adaptive LLM routing layer for **Hermes Agent**, using a
TypeSafe JEV routing service to choose between a fast/cost-efficient model and a
high-capability model — while keeping fail-open behaviour, session safety, a
runtime kill switch and production observability.

> **Status: shadow-first.** Routing decisions are collected and logged, but the
> model that actually executes a turn is still chosen by the gateway's own
> configuration. Automatic provider switching is **designed, not enabled**.

Built for / tested with **Hermes Agent v0.21.5** (Nous Research). This repository is
an independent plugin/integration — it is not affiliated with Nous Research and
contains no part of the Hermes source tree.

---

## Problem

An agent that runs long tool-calling loops on a single model is always paying the
wrong price somewhere:

| Situation | Cost of a single-model agent |
|---|---|
| Trivial turn (status check, one command) | The most capable model is billed at premium latency and price |
| Genuinely hard turn (architecture, multi-file debug) | A cheap/fast model produces shallow work that has to be redone |
| Manual switching | Poor UX, and mid-loop switches can break context and prompt caching |
| Third-party router in the request path | New privacy surface (full prompts leave the host) and a new single point of failure |

## Solution

Route with a **decision service that sees almost nothing**, and act on the decision
only after the routing layer has proven itself on real traffic.

```mermaid
flowchart TD
    U[User turn] --> G[Hermes Gateway]
    G --> SR[Shadow Router plugin<br/>pre_api_request hook]
    SR --> RD[Sanitized Routing Dossier<br/>redacted, truncated, current turn only]
    RD --> JEV[TypeSafe JEV<br/>route decision + probabilities]
    JEV --> DEC[Route decision<br/>deepseek_flash / mimo_pro]
    DEC --> LOG[(Shadow telemetry<br/>JSONL, content-free)]
    G --> ACTUAL[Actual production path<br/>unchanged model]
```

The decision is recorded; the production path is untouched. That is the whole point
of the shadow-first rollout: **observable before automatic**.

### Future auto routing (designed, not enabled)

```mermaid
flowchart TD
    JEV[JEV route decision] --> OVR[Session-scoped one-turn<br/>model override]
    OVR --> DS[deepseek_flash]
    OVR --> MM[mimo_pro]
    DS --> LOOP[Hermes agent loop]
    MM --> LOOP
    LOOP --> REST[Automatic restore of the<br/>session runtime in finally]
    classDef future fill:#332,stroke:#a80,color:#fff
    class OVR,REST future
```

Everything marked *future* is architecture work in `docs/auto-routing-design.md`.
It is not implemented in this release, and enabling it additionally requires an
explicit approval flag that the code checks itself.

## Design principles

1. **Lowest sufficient model.** Route by task requirements, not by habit.
2. **Privacy by design.** The routing service receives a redacted, truncated dossier
   derived from the current turn — never memory, history, tool output or credentials.
3. **Fail-open.** A routing layer that can break the agent is worse than no routing
   layer. Timeouts, HTTP errors and malformed responses are classified and dropped.
4. **No single point of failure.** The decision service is strictly advisory; the
   turn proceeds regardless of its answer.
5. **Session isolation.** Any future switch is scoped to one session and one turn.
6. **Observable before automatic.** Shadow mode first, statistics second, autonomy last.
7. **Reversible changes.** A sentinel file turns everything off without a restart.
8. **Minimal upstream modification.** One plugin hook; zero Hermes core patches.

## Current status

| Capability | Status |
|---|---|
| Production shadow collection | **Validated** on a live messaging gateway |
| DeepSeek V4.1 Flash integration | **Validated** (text, reasoning, tools, multi-step loop, coding, error paths) |
| MiMo V2.6 Pro integration | **Validated** (same matrix) |
| Privacy redaction + privacy fallback | **Validated** |
| Runtime kill switch | **Validated** (21/21 parser tests, fail-safe = off) |
| Real/test telemetry separation | **Validated** |
| Automatic provider switching | **Design stage — not enabled** |
| Concurrency isolation for auto mode | **Analyzed; one empirical test still outstanding** |

No cost-saving, performance or scale claim is made here: the sample of real shadow
turns is intentionally small and is reported as distributions, not as headline
numbers.

## How it works

1. **Hook.** The plugin registers a single observer hook (`pre_api_request`) that the
   Hermes core already calls before each LLM request — and already wraps in
   fail-open error handling. The hook always returns `None`, so no context is
   injected and no request content is modified.
2. **First call of a turn.** Routing happens once per turn: retries and subsequent
   tool-loop iterations are explicitly skipped (`api_call_count`/`retry_count`).
3. **Dossier.** A minimal JSON object is built from the current user message:
   redacted and truncated text, boolean requirement flags (tool use, shell, coding,
   debugging, research, long context), risk flags (destructive action, production
   change) and the set of available routes.
4. **Redaction.** Deterministic, offline, regex-based: private key material, prefixed
   API keys, bearer tokens, `key=value` secrets, emails, IPv4/IPv6 addresses,
   `user@host` targets and long opaque tokens. If private key material is detected,
   the turn is **not** sent anywhere and a local privacy fallback is recorded.
5. **Decision.** `POST /v1/systemone` with a `choice` question and two criteria;
   the response carries a choice, a confidence and probabilities. Every failure is
   classified as `timeout`, `http_<code>`, `urlerror_*` or `malformed_*`.
6. **Telemetry.** A bounded queue plus a daemon worker keep the routing call off the
   request path; a full queue drops the sample instead of delaying the turn. Records
   contain outcome data and task features only.
7. **Mode.** Every turn re-resolves the runtime mode from a local state file
   (kill sentinel → state file → breaker → approval/lease/heartbeat). Fail-safe is
   `off`.

## Repository layout

```
router/     the routing library (config, redact, dossier, client, shadow, state)
plugin/     the Hermes Agent plugin (plugin.yaml + pre_api_request hook)
tests/      offline test suites (33 routing checks, 21 kill-switch checks)
tools/      production statistics with real/test sample separation
docs/       architecture, shadow mode, privacy model, failover, validation, auto design
examples/   fully synthetic configuration and telemetry samples
```

## Quick start

```bash
git clone https://github.com/INEEDBUG/hermes-adaptive-model-router
cd hermes-adaptive-model-router

# 1. try it without touching a running agent
JEV_LOG_DIR=/tmp/jev-shadow-logs python3 tests/test_router.py
python3 tests/test_state.py

# 2. install as a Hermes plugin
cp -r plugin "$HERMES_HOME/plugins/jev-shadow-router"
export JEV_ROUTER_ROOT="$PWD"          # lets the plugin import the router package
#   then enable it for the gateway (Hermes config):
#   hermes config set plugins.enabled '["jev-shadow-router"]'
```

Configuration is environment-first, then `.env`, then built-in defaults
(see `examples/config.example.env`):

| Variable | Default | Meaning |
|---|---|---|
| `ROUTER_MODE` | `shadow` | `off` / `shadow` (auto requires explicit approval) |
| `JEV_AUTO_APPROVED` | unset | explicit approval gate for `auto`; unset forces `shadow` |
| `JEV_MODEL` | `jev-latest` | routing model alias |
| `JEV_MIN_CONFIDENCE` | `0.65` | below this the simulated decision prefers the capable route |
| `JEV_MIN_MARGIN` | `0.15` | minimal top-2 probability margin |
| `JEV_TIMEOUT_SECONDS` | `3` | hard cap for the routing call |
| `TYPESAFE_API_KEY` | — | credential for the routing service (never logged) |
| `JEV_STATE_DIR` | `$HERMES_HOME/jev_router/state` | kill sentinel + state file |
| `JEV_LOG_DIR` | `$HERMES_HOME/logs/router` | shadow telemetry |

## Observability

`tools/shadow_stats.py` reports `real_turns`, route counts and shares, confidence
(mean/median/p10/p90), probability margin distribution, JEV latency
(mean/p50/p95/max), timeout/error counts, privacy fallbacks and task-category /
context-size breakdowns per route.

Real turns are separated from everything else: a record only counts as production
traffic when the session embedded in its `turn_id` exists in the Hermes session
database with a real messaging platform as its source. Manual tests, CLI one-shot
sessions and fault-injection runs are bucketed as excluded and listed explicitly, so
they can never inflate production statistics.

## Validation

```
router offline + live groups ..... 33/33 checks pass
kill-switch resolver ............. 21/21 checks pass
production shadow ................ validated through a real messaging gateway
fail-open ........................ validated under injected failures and
                                   one observed real routing timeout
```

The live routing group performs a real decision call and is skipped automatically
when no credential is configured, so the suite is runnable by anyone. See
`docs/provider-validation.md` for the per-provider matrix.

## Privacy and security

See [`SECURITY.md`](SECURITY.md) and [`docs/privacy-model.md`](docs/privacy-model.md).
Summary: the decision service receives a redacted dossier, never raw private content;
telemetry is content-free; credentials are never logged; the kill switch makes a
third-party call impossible without a restart.

## Kill switch

```bash
touch "$HERMES_HOME/jev_router/state/KILL"   # effective on the next turn
```

Precedence: `KILL` sentinel → state file presence/validity → breaker flag →
approval + lease + heartbeat. Anything unreadable, corrupt or missing resolves to
`off`. See `docs/failover-and-kill-switch.md`.

## Limitations

- Redaction is pattern-based, not a formal data-loss-prevention guarantee.
- The provider-side prompt cache is invalidated by provider switching; per-turn
  switching is therefore treated as a cost, not a feature (see the auto design doc).
- Auto mode has **no** completed concurrency test yet; the isolation requirements it
  must satisfy first are documented, not assumed.
- Statistics from a small real-traffic sample are reported as distributions only.

## License

MIT — see [`LICENSE`](LICENSE).
