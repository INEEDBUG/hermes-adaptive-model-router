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

The routing call sends a **Routing Dossier**: the sanitised, truncated text of the
current turn (after deterministic redaction) plus boolean task/risk flags. State it
plainly — that sanitised text *is* transmitted; it is the input the decision service
routes on.

It does **not** send conversation history, long-term memory, tool output, file
contents, credentials, hostnames or infrastructure details. Those inputs are not merely
filtered: the dossier builder never receives them.

Redaction is a finite, documented pattern set: it is strong hygiene, **not** a
confidentiality or DLP guarantee, and content that matches no pattern travels with the
sanitised text. If you need a harder boundary, disable routing entirely — write
`mode: off` with `python3 tools/init_state.py --mode off`, or create the `KILL`
sentinel (see `docs/failover-and-kill-switch.md`) so no third-party call is made at all.

## Human-turn provenance boundary (v0.2.0)

The router only observes human-origin turns. Internal, subagent, background and continuation turns are rejected before Routing Dossier construction.

Two structural conditions are evaluated before a Routing Dossier is built: the turn's
`platform` must be in `JEV_ALLOWED_PLATFORMS`, and its `turn_origin` must be `user`. The
second is authoritative, because internal notifications, background-review forks, compaction
continuations and subagent turns can inherit the platform label of the session they came from
— a platform allowlist on its own does not describe *who* sent the turn. A payload with a
missing, empty or unrecognised origin is rejected; it is never treated as `user`.

Turns that are rejected are counted locally in a content-free counter file (`date`,
`platform`, `turn_origin`, `reason`, `count`); the write path has no message parameter, so no
turn content can reach it. If the provenance label disappears — for instance after a Hermes
upgrade replaces the patched files — the router stops observing rather than observing
everything. `tools/check_turn_origin_patch.py` verifies the integration and exits 3 on drift.

### Note on v0.1.x

v0.1.x had no structural provenance boundary. It could therefore send auxiliary, subagent,
CLI one-shot, cron and background-review turns to the shadow routing service — an overly
inclusive observation surface, not a credential disclosure: no credential, memory, tool
output or conversation history was ever part of a dossier, and routing remained advisory with
automatic switching disabled. v0.2.0 adds the `turn_origin` label and rejects non-human turns
before the dossier is constructed. v0.1.x users should upgrade; the earlier releases are left
in place unchanged and are not rewritten.

## Failure behaviour

Routing is fail-open by design: timeouts, HTTP errors, malformed responses and an
unreachable service all leave the agent turn untouched. The runtime mode resolver
fails safe as well — any unreadable, corrupt or missing state resolves to `off`.

## Reporting a vulnerability

Open a private security advisory, or an issue that describes the problem without
including secrets, live telemetry or personal data. Please do not attach real logs;
reduce them to a minimal synthetic reproduction first.
