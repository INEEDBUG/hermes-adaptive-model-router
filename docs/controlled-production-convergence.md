# Controlled production convergence

Procedure for replacing a live deployment's router code with the canonical line **without
losing evidence, without broadening behaviour, and with one restart per direction**. It applies
to a deployment that currently runs an uncommitted variant; it is written so that it can also be
read as a template for any future code convergence.

Preparation is separated from execution on purpose: a prep round is read-only and produces a
reviewable packet; execution happens in a separately authorised window, because it restarts the
process that would otherwise be supervising it.

## 1. Freeze one candidate

Convergence pins exactly one revision and treats it as immutable:

* a single commit id is designated the **runtime candidate**, and stays frozen even if later
  documentation-only commits land on the branch;
* the candidate is only unfrozen by a new runtime-code change, which requires a fresh rehearsal;
* the candidate is exported into a **generation directory** — an immutable copy, not a checkout
  (`git archive <sha>` of the runtime subset, so no `.git` directory is created);
* the generation directory carries a `manifest.json` with the file list and their hashes, and is
  made read-only afterwards;
* a later fetch, pull or checkout anywhere else cannot silently change what the deployment runs.

The runtime root therefore points at the generation directory, not at a working copy:

```
<deployment-root>/generations/<generation>/
    manifest.json      # candidate sha, generation, tag, file hashes
    router/            # the canonical package
```

The plugin directory holds the plugin manifest and module; the importable package comes from the
runtime root. After convergence exactly **one** canonical package must be importable, and exactly
**one** plugin registration must be enabled.

If a release tag is used, it is created *before* deployment, for auditability — so a telemetry
record can be traced to an immutable artefact — never for marketing. The generation name should
embed both the tag and the pinned commit, and must satisfy the generation constraint
(`[a-z0-9._-]`, ≤ 64 characters).

## 2. Make configuration explicit

The canonical line never assumes what routes exist. A convergence must therefore state the
deployment's route set explicitly rather than inheriting a hard-coded default, so that the
request contract and cohort comparability survive the change.

Keys a convergence may add or modify — and nothing else:

| key | meaning |
|---|---|
| runtime root | the immutable generation directory, written **before** the installer runs, because the installer only appends and leaves an existing value alone |
| available routes | the deployment's explicit route set |
| skip-counter path | the single existing counter series |
| deployment generation | the marker described below |

Everything not in that set must be verified unchanged — thresholds, timeout, allowed platform,
mode seed, and the provider credential — by comparing hashes or classifications, never by copying
values into a document. The approval flag for automatic mode must remain absent.

## 3. Generation marker

Each convergence introduces a new deployment generation token. Records written before the marker
existed are reported as `legacy_unversioned` and are never rewritten. Because
`would_execute` is derived from configured availability in the canonical line but was a
deployment-level assumption before it, a mixed record set must not publish a merged figure: the
statistics report per-generation values, and a single-policy view is selected explicitly.

Rotation rule: a new generation per convergence, or per change that can alter routing behaviour.
Never reuse a generation for a different candidate.

## 4. Counter continuity

Rejections are evidence, so the existing counter series is continued, not replaced:

* the override points at the **existing** file, so no second, empty series is created;
* record the total before the window and require the total after to be greater than or equal to it;
* forbidden: clearing the file, renaming the existing history, rewriting old day keys, converting
  historical day keys from a local to a UTC basis, or copying the old file to a canonical filename
  and then writing both;
* the day basis of newly written keys changes with the canonical implementation; one migration
  boundary may therefore split a calendar day across two keys, and totals stay complete.

## 5. Append-only evidence and rollback

Telemetry records, counters and audit evidence are append-only. A rollback restores **code**, not
history:

| asset class | examples | rollback behaviour |
|---|---|---|
| code assets | plugin, runtime tree, environment, plugin registration | restore |
| state assets | mode file, stop sentinel, tripped state | restore only if unexpectedly modified — a correct convergence does not touch them |
| append-only assets | telemetry, counters, audit evidence | **never** restored to an older snapshot |

`APPEND_ONLY_ASSETS_ROLLBACK_POLICY = NEVER_RESTORE_TO_OLDER_SNAPSHOT`. A read-only snapshot may
be taken before the window for comparison; it is never written back, because doing so would erase
newer truth and destroy the audit trail.

Rollback takes one restart, and whether that restart is authorised must be stated explicitly in
the authorisation for the window. A rollback that follows a failed *static* check is immediate; a
rollback following a failed natural canary is adjudicated from the record.

## 6. Quiescence before the restart

The validated model is `HEARTBEAT_EDGE_PLUS_STABLE_WINDOW`:

1. wait for one real advance of the runtime state file's mtime (bounded timeout);
2. then require a **continuous** stable window of ≥ 20 s, sampled at roughly 250 ms, with: pid
   stable, process epoch stable, healthy, platform connected, active agents 0, active model
   requests 0, IO status OK, counter errors 0, unparented 0, all io-quiescence fields present, no
   drain marker, and no visible human or agent work.

Any activity resets the stable timer. Never drain and never cancel work. A rule of the form
"state file younger than a fixed number of seconds" is **not** used: it is unreachable for an idle
gateway and was replaced for that reason.

## 7. Restart discipline

Exactly one official restart per direction, issued through the supported command. No signal
fallback, no service-manager fallback, no container recreate, no second restart — a second
restart destroys the causal link between the plan and the observed state.

Because the restart ends the process handling the executing turn, execution is driven by a
detached executor that starts only after that turn ends. It fails closed, records each phase
atomically, and records old and new pid, process epoch, candidate sha, runtime manifest and an
environment semantic diff — never a secret value.

## 8. Acceptance

**Static acceptance** (after the restart, without any synthetic request):

* new pid differs from the old pid, and the process epoch matches the new pid;
* gateway healthy, messaging platform reconnected, same container, container restart count delta 0;
* mode unchanged, automatic mode not reachable and not enabled, stop-sentinel semantics correct;
* all io-quiescence fields present and status OK, active model requests 0, counter errors 0,
  unparented 0;
* exactly one enabled plugin registration; installed plugin and runtime hashes equal the frozen
  manifest; the runtime root resolves to the immutable generation directory;
* route set, counter path and generation marker exactly as planned; thresholds, timeout, allowed
  platform and credential unchanged (compared by hash); the turn-origin drift checker passes.

**Natural human canary**: the next real human turn must produce exactly one decision and exactly
one new record, carrying the expected generation marker, platform, `turn_origin = user` and the
configured route set, with no duplicate per turn, no admitted non-user request, unchanged
execution model, automatic mode still disabled, and no counter regression. No synthetic request
is ever made to produce the canary; rejected classes continue to produce no decision and stay
counted.

State machine: `STATIC_SUCCESS_CANARY_PENDING` → `CONVERGENCE_VALIDATED` (both stages passed) or
→ rollback.

## 9. Rollback triggers

Immediate, during static acceptance: restart failure; gateway unhealthy; platform fails to
reconnect; wrong plugin count; wrong runtime hash; wrong environment semantic diff; mode not the
expected value; automatic mode reachable or enabled; stop sentinel bypassed; checker failure;
container restart count changed; counter file missing or reset; any other unexpected drift.

During the natural canary: zero decision for an admitted human turn; more than one decision for
the same turn; missing or wrong generation marker; wrong route set; a non-user request admitted;
an error regression attributable to the new runtime.

## 10. Non-goals

Convergence does not enable automatic mode, does not change thresholds, does not perform live
provider or routing requests, does not synthesise a canary, does not publish telemetry, and does
not alter append-only evidence. Automatic mode remains disabled until its own gate — sufficient
real-traffic evidence and a concurrency check — is satisfied.
