# Failover, error taxonomy and the kill switch

## Fail-open on availability

The routing layer is advisory. Every path that could fail is contained:

| Layer | Containment |
|---|---|
| Hook dispatch | Core already wraps hook calls in error handling; the hook body is additionally wrapped in `try/except` and always returns `None` |
| Dossier/redaction | Pure functions, no network; unexpected input types are rejected before use |
| Queue | Bounded (8); a full queue drops the sample rather than delaying the turn |
| Worker thread | Exceptions are swallowed inside the worker loop |
| Telemetry write | Wrapped; a disk error cannot affect the turn |
| State resolution | Any error resolves to `off` |

Result: with the routing service completely unreachable, an agent turn behaves exactly
as if the plugin were not installed. This has been tested with an unreachable
endpoint, an invalid credential and a malformed response.

## Error taxonomy

| Classification | Trigger | Typical operator meaning |
|---|---|---|
| `timeout` | socket timeout / read timeout | Service slow or unreachable; consider raising the budget or disabling |
| `urlerror_<Reason>` | connection refused, DNS failure, TLS failure | Service down or network path broken |
| `http_401` / `http_403` | credential rejected or expired | Rotate the credential |
| `http_429` | rate limited | Back off; do not retry in-line |
| `http_5xx` | server-side failure | Service incident |
| `malformed_response` | response is not a JSON object | Contract break |
| `malformed_answer_type` | answer type is not `choice` | Contract break |
| `malformed_unknown_choice` | choice outside the declared criteria | Contract/version mismatch |
| `malformed_missing_probabilities` | probabilities absent or empty | Contract break |
| `malformed_confidence` | confidence missing, out of range, or non-numeric | Contract break |

Classification exists so that failure statistics are actionable instead of a single
opaque error count. Malformed-response cases are separated from transport cases
because they require a code decision (contract drift), not an operational one.

## Kill switch

The switch is a **file**, not an environment variable. Hermes loads its `.env` with
`override=True` and a running process never re-reads its environment, so an env-var
switch would practically require a restart to disable. A file is effective on the
next turn.

```bash
# immediate, no restart, highest precedence
touch "$HERMES_HOME/jev_router/state/KILL"

# restore
rm "$HERMES_HOME/jev_router/state/KILL"
```

Precedence (first match wins):

1. `KILL` sentinel present → `off`
2. state file missing, unreadable, or not a JSON object → `off`
3. `tripped: true` → `off`
4. unknown mode string → `off`
5. `auto` without explicit approval → `shadow`
6. `auto` with missing/expired `auto_until` → `shadow`
7. `auto` with a heartbeat older than 120 s → `shadow`

Anything unexpected resolves to `off`; the resolver has no code path that returns
`auto` by default (asserted by test 17 of the kill-switch suite).

The stop cannot be lifted by tooling. `tools/init_state.py` has no `--force`: while the
sentinel exists it refuses (exit 2) and writes nothing, so recovery is a deliberate
`rm $JEV_STATE_DIR/KILL` followed by an explicit re-initialisation — visible in shell
history, in review, and in the audit log. Tests 21–25 of the kill-switch suite pin that
behaviour, including that the old flag is rejected. The installer follows the same rule:
it creates `mode.json` only when it is absent and preserves an existing state and the
sentinel byte-for-byte on a re-run.

Operator *intent* is never silently honoured either: the resolver reports `auto` if a
state file records it, but this release downgrades it to `shadow` in the plugin
(`router.state.AUTO_IMPLEMENTED` is `False`) and appends `auto_not_implemented` to the
mode audit log. That keeps the recorded state honest and the effective behaviour
truthful at the same time.

## Breaker design (for the future auto release)

The resolver already understands the shape a breaker needs, and models it as data:

```json
{ "mode": "off", "tripped": true, "by": "circuit-breaker", "reason": "5 consecutive routing failures" }
```

Intended semantics, to be wired only together with auto mode:

- N consecutive routing failures (default 5) → write `mode=off` with `tripped: true`;
- tripping is **sticky**: it is not cleared automatically, an operator clears it;
- `auto` additionally requires a live lease (`auto_until`) and a fresh heartbeat, so a
  crash, a hang or a forgotten experiment degrades to `shadow` instead of persisting;
- every resolution that deviates from the state file is appended to a mode audit log.

## Operator runbook

| Situation | Action |
|---|---|
| Something looks wrong with routing | `touch .../state/KILL` — effective next turn |
| Investigating a suspected leak | Set `ROUTER_MODE=off` in `.env` and restart, or use the sentinel for immediate effect |
| Routing service incident | Leave the sentinel in place; agent behaviour is unaffected |
| Credential rotation | Replace the credential in the environment/`.env`; no code change |
| Resuming observation | Remove the sentinel and confirm the next decision is recorded |
