# Shadow mode

## Rationale

A routing layer makes two different claims: *"my decisions are good"* and *"acting on
my decisions is safe"*. Only the first can be evaluated without touching production.
Shadow mode makes that separation explicit — decisions are collected, execution is
not changed, and the two questions are answered in order.

## Mode resolution

````python
res = state.resolve()              # {'mode', 'source', 'reason'}
if res['mode'] == 'off':
    return None                    # behave exactly like "not installed"
````

The **state file is the runtime authority**: it is resolved once per turn, and a
missing, unreadable, corrupt or non-object file resolves to `off`. `ROUTER_MODE`
(via the library's config helper) is only a default; it does not enable collection by
itself. `tools/init_state.py` writes the file explicitly and atomically, refuses to
write while a `KILL` sentinel is present, and never writes `auto`.

Resolution order, first match wins:

| # | Condition | Result |
|---|---|---|
| 1 | `KILL` sentinel exists (or cannot be stat'ed) | `off` |
| 2 | `mode.json` missing / unreadable / not a JSON object | `off` |
| 3 | `tripped: true` | `off` |
| 4 | unknown mode value | `off` |
| 5 | `auto` without explicit approval | `shadow` |
| 6 | `auto` with missing/expired `auto_until` | `shadow` |
| 7 | `auto` with stale `heartbeat` (> 120 s) | `shadow` |
| 8 | otherwise | as recorded |

Two independent gates protect automatic behaviour: the state file must say `auto`,
**and** the environment must carry an explicit approval value. A single edit of a
configuration string cannot enable autonomy.

**Automatic switching is not implemented in this release.** If a state file records
`auto` (operator intent), the resolver reports that intent faithfully, but the plugin
resolves it to `shadow` while `router.state.AUTO_IMPLEMENTED` is `False` and appends an
`auto_not_implemented` entry to the mode audit log. Nothing in telemetry can therefore
suggest that automatic routing took place.

## State file

```json
{
  "mode": "shadow",
  "updated_at": 1760000000,
  "auto_until": null,
  "heartbeat": null,
  "tripped": false,
  "by": "operator"
}
```

`write_state()` writes atomically (temp file + rename), so a reader never observes a
half-written file. The reader itself performs one or two `stat` calls per turn and at
most one read of a sub-1KB file when its mtime changed.

## What is recorded

Per decision:

| Field | Purpose |
|---|---|
| `timestamp`, `turn_id` | Correlate a decision with a turn (no content) |
| `jev_model` | Concrete routing model revision behind the alias |
| `route`, `confidence`, `p_deepseek`, `p_mimo` | Decision and its certainty |
| `latency_ms`, `input_tokens`, `output_tokens` | Cost and latency envelope |
| `success`, `error` | Failure taxonomy |
| `actual_model` | What really executed — the shadow invariant |
| `would_execute` | What a future auto mode would have chosen |
| `mode` | Mode in force for that turn |
| `task_length`, `tool_use`, `shell`, `coding`, `debugging`, `research`, `long_context`, `destructive_action`, `production_change` | Content-free task features |
| `redaction_count` | How many redaction hits the turn produced (a count, never content) |
| `dossier_token_estimate` | Rough **routing-input** size of the dossier — **not** the conversation-context or prompt-cache size |

Never recorded: the user's message, the dossier body, credentials, memory, tool
output, host details. The allow-list is enforced by a test (`F4`), so a field added
to the record fails the suite until the allow-list is updated deliberately.

## Reading the telemetry

`tools/shadow_stats.py` produces the aggregate view, including confidence
percentiles, margin distribution, latency percentiles and per-category/per-context
route breakdowns. A single record looks like `examples/shadow-record.example.json`.

## Sample hygiene

Real traffic and test traffic are separated by construction, not by convention:

- a record counts as production traffic only if its session exists in the Hermes
  session database with a genuine messaging platform as the source;
- manual and fault-injection runs use recognisable turn-id prefixes and are bucketed
  as excluded;
- CLI one-shot sessions are excluded by their session source.

Both exclusion lists are printed with the statistics, so the separation is auditable
rather than asserted.
