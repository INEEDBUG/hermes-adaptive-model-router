# Privacy model

## Threat model

The routing decision requires a third-party service to read *something* about the
turn. The design question is therefore not "can we avoid sending data" but "what is
the smallest thing we can send, and what happens when that thing is still unsafe".

Assumed adversaries/risks:

1. The routing service (or anything on its path) reading private content.
2. Credential material leaking into a third-party request or into local logs.
3. Long-term memory or conversation history being shipped off-host as a side effect
   of "smart routing".
4. A routing outage being mistaken for an agent outage.

## Human-turn provenance boundary (v0.2.0)

> The router only observes human-origin turns. Internal, subagent, background and
> continuation turns are rejected before Routing Dossier construction.

A routing observation **is a privacy event**: it sends a sanitised, truncated copy of the
current turn to a third-party decision service. Only a human-origin turn may cause one.
Turns that carry the same *topic* but not the same *origin* — a self-injected
notification, a background-review fork, a compaction continuation, a delegated subagent —
are therefore not "less private"; they are not eligible to be observed at all.

The boundary is two gates, **both evaluated before a Routing Dossier is built**:

| # | Gate | Condition | Question it answers |
|---|---|---|---|
| 1 | Platform allowlist | `platform` ∈ `JEV_ALLOWED_PLATFORMS` | which platforms may ever be observed |
| 2 | Turn provenance (authoritative) | `turn_origin == "user"` | whether *this* turn is a human turn |

Failing either gate returns immediately: the `redact → dossier.build → shadow.submit`
chain is never entered — no text is redacted, no dossier is built, nothing is queued — and
the routing service is never reached.

The public default for `JEV_ALLOWED_PLATFORMS` is **empty**, which means no platform is
allowed: the router stays inert until an operator names one (`feishu`, `telegram`, …).
So the default deployment observes nothing, and a turn that is not provably human is not
observed even when its platform *is* named.

### Why the platform allowlist alone is not a privacy boundary

Gate 1 answers "which platform", not "who spoke". An internal notification, a
background-review fork, a compaction continuation or a subagent turn can carry the same
platform label as a human message, because a fork inherits `platform` from the session that
spawned it — a background review of a Feishu conversation reports `platform=feishu`.
Verified in production: internal-notification **and** background-review turns both arrived
with `platform=feishu` and were stopped only by gate 2. Allow-listing a platform without
gate 2 would have admitted internal turns of that platform.

### Where the provenance label comes from

`turn_origin` is supplied **structurally** by the Hermes core, decided before the request is
assembled (see `patches/hermes-v0.21.5-turn-origin.patch`, built and tested against Hermes
Agent v0.21.5):

`user`, `internal_notification`, `compaction_continuation`, `background_review`, `subagent`,
`oneshot`, `cron`, `curator`, `api_server`, `unknown`.

Message text is **never** used to decide `user`. A missing, empty or unrecognised origin is
rejected — never defaulted to `user` (fail-closed). One consequence is deliberate: if the
integration is absent (after a Hermes upgrade, for instance), *every* turn lacks the label
and *every* turn is rejected, so routing goes quiet instead of reverting to unconditional
observation. `tools/check_turn_origin_patch.py` verifies the version target, the patch
markers and the plugin's two gates, and proves the behaviour offline; exit 3 means drift —
keep the router fail-closed/off until the patch is re-applied, and never "fix" a missing
label by treating it as `user`.

### Correction to the v0.1.x wording

Earlier versions of these documents said the integration needs "zero Hermes core patches".
That is no longer accurate, and the accurate sentence is:

> Shadow routing logic remains plugin-based, while strict human-turn provenance requires a
> minimal Hermes v0.21.5 turn_origin integration patch.

### Optional defense in depth

`JEV_INTERNAL_MARKERS` is an **optional** anomaly detector (`'||'`-separated). If a turn's
origin says `user` but its text carries a configured internal marker, the turn is skipped
and counted as `invariant_violation`. It can only withhold eligibility, never grant it, and
it is never the boundary.

## Data minimisation

Sent in the dossier:

- the **current turn only**, after deterministic redaction, truncated to at most
  1200 characters;
- boolean requirement flags: tool use, shell, coding, debugging, research, long context;
- boolean risk flags: destructive action, production change, sensitive-content detected;
- runtime counters: previous failures, verification failed;
- the advertised route list.

Never sent:

- conversation history or the session transcript;
- long-term memory / memory documents;
- tool output or file contents;
- credentials, tokens, API keys, connection strings;
- hostnames, internal addresses, device identifiers;
- the system prompt.

The dossier builder does not even receive those inputs: it is called with the current
user message as its only content argument, so the guarantee is structural rather than
a matter of remembering to filter.

An admitted human turn is also the only kind of turn that reaches that call. The two
provenance gates are evaluated first (`docs/architecture.md`), so a rejected turn never has
a dossier built for it — there is nothing to filter.

## Deterministic redaction

Redaction runs locally, before the dossier leaves the process. It uses no model and no
network, so it never transmits data outside the process and cannot become a leak channel
itself.

**Scope of the guarantee.** Redaction is a finite, documented rule set applied to text —
it is **not** a confidentiality guarantee and **not** a DLP control. Anything outside
those patterns travels as part of the sanitised turn text. Treat it as strong hygiene plus
a structural reduction of what leaves the host, not as certification.

| Kind | Pattern (summary) | Replacement |
|---|---|---|
| `private_key` | PEM private key blocks | `[REDACTED_PRIVATE_KEY]` (and marked unsafe) |
| `api_key` | `sk-`/`tp-`/`pk-`/`rk-` prefixed keys, `apikey_…` | `[REDACTED_KEY]` |
| `bearer` | `Bearer <token>` | `Bearer [REDACTED]` |
| `kv_secret` | `api_key`, `token`, `password`, `secret`, `credential`, … `: value` | `<key>=[REDACTED]` (key name kept for context) |
| `email` | e-mail addresses | `[REDACTED_EMAIL]` |
| `ipv4` / `ipv6` | IP literals | `[REDACTED_IP]` |
| `ssh_target` | `user@host` | `[REDACTED_CREDENTIAL]` |
| `long_token` | opaque strings ≥ 40 chars | `[REDACTED_TOKEN]` |

Every redaction increments a hit counter; the count (not the content) is recorded in
telemetry, which gives an operational signal without storing the secret.

The count is exact, and that is asserted offline: a `key=value` secret that matched twice in
the same turn used to be double-counted, and the v0.1.1 hardening commit fixed it — one
secret counts as one hit, three as three, with the redacted text byte-identical. v0.2.0
keeps that behaviour, so `redaction_count` can be compared across releases.

## Privacy fallback

If redaction encounters material it cannot sanitise with confidence — private key
blocks — the turn is **not** sent for routing at all. A local record is written with
`route = LOCAL_PRIVACY_FALLBACK` and the reason, and the turn proceeds with the
gateway's configured model. Fail-closed on privacy, fail-open on availability: the
two policies are deliberately kept apart.

## Logging discipline

- Telemetry has a fixed, tested field allow-list; adding a field that could carry
  content fails the test suite.
- Authorization headers are never logged, at any level.
- Credentials are read from the environment or a local `.env` file; they are never
  written back, echoed, or included in error output.
- Accepted shadow records carry the turn's provenance labels (`platform`,
  `turn_origin`) as bounded enumeration values — never the text that was used to
  decide them.
- The repository ships synthetic examples only. No production telemetry, session
  database or log is part of this project.

### Rejection counters (content-free)

A rejected turn must leave a trace, otherwise a silent misconfiguration is
indistinguishable from a busy deployment. That trace may not contain content either: it is
a JSON counter file whose entries carry only `date`, `platform`, `turn_origin`, `reason`
and `count`. A synthetic example is `examples/skipped-turn-counters.example.json`.

Banned in that telemetry: prompt text, message bodies, Routing Dossiers, tool input or
output, memory, the system prompt, session/turn/message ids, and credentials. Two
properties enforce that structurally rather than by convention:

- the write path takes **no message argument at all**, so it cannot receive turn content;
- every label is normalised to a bounded `[a-z0-9_-]` token, and an unknown reason collapses
  to a closed-vocabulary category — no free-form string can reach the file even if a caller
  passes one.

Reason categories: `allowlist_empty`, `missing_platform`, `platform_not_allowed`,
`missing_origin`, `origin_not_user`, `invariant_violation`, `other`.

## What the routing service receives, and what it can still infer

Be precise here, because the honest statement is stronger than an absolute one:

**It does receive** — for an admitted **human-origin** turn only — the sanitised,
truncated text of the current turn, plus boolean requirement flags (tool use, shell,
coding, debugging, research, long context), boolean risk flags (destructive action,
production change) and the runtime counters. Saying "the decision service cannot see your
data" would be wrong: that text is the input it routes on, and the two gates do not remove
it — they decide which turns produce a request at all. A rejected turn produces none, so
none of its text leaves the host.

**It never receives** the surrounding context: conversation history, long-term memory,
tool output, file contents, credentials or tokens, hostnames and internal addresses,
device identifiers, chat identifiers, or the system prompt. Those inputs are not merely
filtered out — the dossier builder never receives them, so they cannot be transmitted.

**It can still infer** approximate task shape (length bucket, workload and risk flags),
request timing and frequency, and the distribution of decisions it produced. That is not
your data, but it is metadata about your work. Deployments with stricter requirements
should run the router in `off` mode or place the decision service inside a trusted
boundary of their own.

## What is implemented, validated and planned

- **Implemented (v0.2.0)**: the two-gate admission check evaluated before dossier
  construction; the structural `turn_origin` label, supplied by the minimal Hermes
  v0.21.5 integration patch; content-free rejection counters; a platform allowlist whose
  public default is empty; the optional invariant marker detector; and
  `tools/check_turn_origin_patch.py` for integration drift.
- **Validated offline**: `tests/test_turn_boundary.py` — 37 checks in groups A–G (dual gate
  matrix, fail-closed paths, per-turn counting, concurrency isolation, content-free
  telemetry, anomaly detector, provenance carried into the shadow record). The drift
  checker's behavioural proof is offline as well: missing/unknown/non-`user` origin → zero
  calls to the routing service; allow-listed human turn → exactly one.
- **Observed in production, shadow mode only** (sanitised summary, no ids, no timestamps):
  human turn → 1 routing decision; subagent → 0; internal notification → 0; background
  review → 0; missing/unknown origin → 0; concurrent user/background isolation → PASS;
  content-free rejection telemetry → PASS. Production runs in shadow mode and automatic
  switching remains disabled; nothing was observed in auto mode.
- **Planned, not present here**: automatic model switching. It is not implemented in this
  release; a state file that records `auto` is downgraded to `shadow` and audited as
  `auto_not_implemented`. The provenance boundary changes nothing about that.

Nothing above is a guarantee about content that matches no redaction pattern, and none of it
makes redaction a DLP control.

## Limitations

- Pattern-based redaction cannot recognise an arbitrary secret that matches no
  pattern. Treat it as strong hygiene, not as certification.
- Redaction replaces rather than masks, so a decision may see `[REDACTED_IP]` where an
  address would have provided context; that is accepted deliberately.
- The truncation boundary is fixed (1200 characters); a long turn is summarised for
  routing purposes only by its first part.
- The provenance guarantee is only as strong as the integration it rests on: `turn_origin`
  is decided by the patched Hermes core, and the decision is fail-closed by design. If the
  integration is missing or drifts, the effect is inertness — every turn rejected, no
  routing at all — never an unguarded fallback. It also means no observation until the patch
  is re-applied.
- The boundary admits a turn; it does not make the admitted turn private. For an admitted
  human turn the sanitised, truncated current-turn text **is** transmitted to the decision
  service, and pattern-based redaction is not a confidentiality guarantee.
