# Security policy

## What this repository contains

Published source code, documentation and synthetic examples only. It contains **no**
credentials, no captured production telemetry, no session database, no transcript,
no host inventory and no recorded user messages. Every example payload is
hand-written; no real routing decision is reproduced verbatim.

## Credential handling

- Credentials are read from the process environment or from a local `.env` file
  whose path is configurable. They are never written to telemetry: the shadow log
  records the routing outcome, not the request.
- Authorization headers are never logged, and the redaction layer never echoes the
  value it removes.
- `.env`, `state/`, `logs/`, `*.db` and `*.jsonl` are excluded by `.gitignore`; if
  you fork this project, keep them excluded.

## What leaves the host, and what does not

The routing call sends a **Routing Dossier**: the current turn after deterministic
redaction, truncated to a fixed budget, plus boolean task/risk flags. It does **not**
send conversation history, long-term memory, tool output, file contents, credentials,
hostnames or infrastructure details.

Redaction is best-effort pattern matching, not a formal guarantee. If you need a
harder boundary, run the router with `ROUTER_MODE=off` (or create the `KILL`
sentinel) so no third-party call is made at all.

## Failure behaviour

Routing is fail-open by design: timeouts, HTTP errors, malformed responses and an
unreachable service all leave the agent turn untouched. The runtime mode resolver
fails safe as well — any unreadable, corrupt or missing state resolves to `off`.

## Reporting a vulnerability

Open a private security advisory, or an issue that describes the problem without
including secrets, live telemetry or personal data. Please do not attach real logs;
reduce them to a minimal synthetic reproduction first.
