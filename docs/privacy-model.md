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

## Deterministic redaction

Redaction runs locally, before the dossier leaves the process. It uses no model and
no network, so it cannot itself leak anything.

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
- The repository ships synthetic examples only. No production telemetry, session
  database or log is part of this project.

## What the routing service can still infer

Minimising data does not make inference impossible. A routing service still sees:

- approximate task shape (length bucket, boolean workload flags, risk flags);
- request timing and frequency;
- the decision distribution it produced.

It cannot see your data, but it can see *when* you work and *what class* of work you
do. Deployments with stricter requirements should run with the router in `off` mode
or place the decision service in a trusted boundary of their own.

## Limitations

- Pattern-based redaction cannot recognise an arbitrary secret that matches no
  pattern. Treat it as strong hygiene, not as certification.
- Redaction replaces rather than masks, so a decision may see `[REDACTED_IP]` where an
  address would have provided context; that is accepted deliberately.
- The truncation boundary is fixed (1200 characters); a long turn is summarised for
  routing purposes only by its first part.
