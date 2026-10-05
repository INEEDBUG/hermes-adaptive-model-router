#!/usr/bin/env python3
"""Privacy-safe outcome-signal audit for the JEV shadow router — read only, offline.

What this tool is, and what it deliberately is not
--------------------------------------------------
It answers one question honestly: **which execution-outcome signals can be attributed to a
single human turn today, and with what quality?** It is a joinability and attribution audit,
not a quality analyser, and it refuses to pretend otherwise:

* a session-level or session×model aggregate is never replicated onto every turn of that
  session, and never divided by a turn count to look like a per-turn value
  (``SESSION_AGGREGATE_REPLICATION_FORBIDDEN``);
* the only session aggregates that may be reported as a per-turn value are those of a session
  that contains exactly one admitted human turn inside the observation window, and that
  subset is flagged as biased rather than typical;
* it produces no routing-correctness label and no single quality score. Signals exposed here
  are features, and the hard ones are structural failure markers only;
* it reads no message body, no tool argument and no tool result. Only roles, timestamps,
  status enums, counters and token totals are touched;
* it writes nothing except an optional aggregate JSON into a path the caller names, and it
  refuses a destination inside the deployment.

The distinction that matters for every later phase: a default model that struggled is **not**
evidence that the other model would have done better. That question needs a counterfactual,
which this tool cannot produce and does not claim.
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
CANONICAL = "canonical-dff8b11"
LEGACY = "legacy_unversioned"


def _load_sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hash_id(value) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:12]


def session_key(rec: dict) -> str:
    """``turn_id`` is ``<session>:<turn>[:<attempt>]``; the session is the leading component."""
    return str(rec.get("turn_id") or rec.get("session_id") or "").split(":")[0]


# ------------------------------------------------------------------ state DB (read only)
class StateDB:
    """Read-only structural access to the Hermes state database. No content is selected."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.ok = path.exists()
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True) if self.ok else None

    def columns(self, table: str) -> set:
        if not self.ok:
            return set()
        try:
            return {c[1] for c in self.con.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    def scalar(self, sql: str, default=None):
        if not self.ok:
            return default
        try:
            row = self.con.execute(sql).fetchone()
            return row[0] if row else default
        except sqlite3.Error:
            return default

    def rows(self, sql: str):
        if not self.ok:
            return []
        try:
            return self.con.execute(sql).fetchall()
        except sqlite3.Error:
            return []

    def close(self):
        if self.con is not None:
            self.con.close()


def signal_matrix(db: StateDB, cg_rows: list) -> list:
    """Granularity matrix, taken from the real schema rather than assumed."""
    def has(t, c):
        return c in db.columns(t)

    def populated(t, c):
        return (db.scalar(f"select count(*) from {t} where {c} is not null") or 0) if has(t, c) else 0

    def entry(signal, source, grain, ts, turn_key, session_key_, cumulative, mutable, privacy,
              reliability, available, note=''):
        return {"signal": signal, "source": source, "grain": grain, "available_now": available,
                "timestamp_available": ts, "turn_join_key_available": turn_key,
                "session_join_key_available": session_key_, "cumulative_or_event": cumulative,
                "mutable_or_append_only": mutable, "privacy_risk": privacy,
                "reliability": reliability, "note": note}

    cg_total = len(cg_rows)
    cg_ctx = sum(1 for r in cg_rows if r.get("estimated_context_tokens") is not None)
    cg_lat = sum(1 for r in cg_rows if r.get("first_token_latency_ms") is not None)
    return [
        entry("input_tokens", "sessions + session_model_usage", "PER_SESSION / PER_SESSION_MODEL",
              "no", "no", "yes (session_id)", "CUMULATIVE", "MUTABLE",
              "LOW", "HIGH", "YES" if populated("sessions", "input_tokens") else "NO",
              "totals only; not a per-request context snapshot"),
        entry("output_tokens", "sessions + session_model_usage", "PER_SESSION / PER_SESSION_MODEL",
              "no", "no", "yes", "CUMULATIVE", "MUTABLE", "LOW", "HIGH",
              "YES" if populated("sessions", "output_tokens") else "NO", ""),
        entry("cache_read_tokens", "sessions + session_model_usage", "PER_SESSION / PER_SESSION_MODEL",
              "no", "no", "yes", "CUMULATIVE", "MUTABLE", "LOW", "HIGH",
              "YES" if populated("session_model_usage", "cache_read_tokens") else "NO", ""),
        entry("cache_write_tokens", "sessions + session_model_usage", "PER_SESSION / PER_SESSION_MODEL",
              "no", "no", "yes", "CUMULATIVE", "MUTABLE", "LOW", "HIGH",
              "YES" if populated("session_model_usage", "cache_write_tokens") else "NO", ""),
        entry("cost", "sessions / session_model_usage", "PER_SESSION / PER_SESSION_MODEL", "no", "no",
              "yes", "CUMULATIVE", "MUTABLE", "LOW", "MEDIUM",
              "YES" if (has("sessions", "estimated_cost_usd") or has("sessions", "actual_cost_usd")) else "NO",
              "pricing may be estimated (cost_status/cost_source distinguish)"),
        entry("api_call_count", "sessions / session_model_usage", "PER_SESSION / PER_SESSION_MODEL",
              "no", "no", "yes", "CUMULATIVE", "MUTABLE", "LOW", "HIGH",
              "YES" if populated("sessions", "api_call_count") else "NO",
              "turn-scoped API call counts exist only through the request hook, not in the DB"),
        entry("tool_call_count", "sessions", "PER_SESSION", "no", "no", "yes", "CUMULATIVE",
              "MUTABLE", "LOW", "HIGH", "YES" if populated("sessions", "tool_call_count") else "NO",
              "per-message tool rows exist and can be windowed, but they carry tool names/args"),
        entry("rewind_count", "sessions", "PER_SESSION", "no", "no", "yes", "CUMULATIVE", "MUTABLE",
              "MEDIUM", "MEDIUM", "YES" if populated("sessions", "rewind_count") else "NO",
              "rare; a strong negative signal when it appears"),
        entry("end_reason", "sessions", "PER_SESSION", "no", "no", "yes", "EVENT (session terminal)",
              "MUTABLE", "LOW", "MEDIUM", "YES" if populated("sessions", "end_reason") else "NO",
              "session-level lifecycle reason; not a per-turn verdict"),
        entry("message timestamps", "messages", "PER_MESSAGE", "yes", "no (inferred by window)",
              "yes", "EVENT", "APPEND_ONLY", "LOW", "HIGH",
              "YES" if populated("messages", "timestamp") else "NO",
              "the only structural per-turn timeline that exists historically"),
        entry("finish_reason", "messages", "PER_MESSAGE", "yes (with the row)",
              "no (inferred by window)", "yes", "EVENT", "APPEND_ONLY", "LOW", "MEDIUM",
              "YES" if populated("messages", "finish_reason") else "NO",
              "stop marks a completed assistant turn; incomplete/length mark truncation"),
        entry("runtime failure", "sessions (handoff_error, compression_failure_error)", "PER_SESSION",
              "no", "no", "yes", "EVENT", "MUTABLE", "LOW", "MEDIUM",
              "PARTIAL", "recorded at session level; a turn cannot claim it alone"),
        entry("cancellation", "none per turn", "NOT_PER_TURN", "no", "no", "no", "-", "-", "LOW",
              "LOW", "NO", "only via the interruption hook prospectively, never historically"),
        entry("handoff failure", "sessions.handoff_error", "PER_SESSION", "no", "no", "yes", "EVENT",
              "MUTABLE", "LOW", "MEDIUM", "PARTIAL", "zero rows in this deployment"),
        entry("compression failure", "sessions.compression_failure_error", "PER_SESSION", "no", "no",
              "yes", "EVENT", "MUTABLE", "LOW", "MEDIUM", "PARTIAL", ""),
        entry("request context estimate", "context-guard telemetry", "PER_REQUEST", "yes", "no (time window only)",
              "no", "EVENT", "APPEND_ONLY", "LOW", "LOW",
              "PARTIAL" if cg_ctx else "NO",
              f"{cg_ctx}/{cg_total} requests carry an estimate; the method is an anchor estimate and "
              "the unknown flag is set on many rows"),
        entry("provider failure attribution", "context-guard telemetry", "PER_REQUEST", "yes",
              "no (time window only)", "no", "EVENT", "APPEND_ONLY", "LOW", "LOW", "PARTIAL",
              "present as an enum but not populated with provider-confirmed values here"),
        entry("executing-model latency", "context-guard telemetry (first_token_latency_ms)",
              "PER_REQUEST", "yes", "no", "no", "EVENT", "APPEND_ONLY", "LOW", "LOW",
              "NO" if not cg_lat else "PARTIAL", f"{cg_lat}/{cg_total} rows populated"),
        entry("user follow-up timing", "messages (derived)", "PER_MESSAGE (derived)", "yes",
              "no (derived)", "yes", "EVENT", "APPEND_ONLY", "MEDIUM", "MEDIUM", "YES",
              "derivable now; cannot distinguish dissatisfaction from normal flow"),
    ]


# ------------------------------------------------------------------ turn boundary
def turn_boundary(db: StateDB, sessions: dict) -> dict:
    feishu = {s for s, src in sessions.items() if src == "feishu"}
    seq = collections.defaultdict(list)
    for sid, role, ts, fr in db.rows(
            "select session_id, role, timestamp, finish_reason from messages "
            "where role in ('user','assistant') and timestamp is not null order by session_id, timestamp"):
        if sid in feishu:
            seq[sid].append((role, float(ts), fr))
    users = stops = paired = 0
    gaps = []
    sessions_with_completion = 0
    for sid, rows in seq.items():
        u = [t for r, t, f in rows if r == "user"]
        s = [t for r, t, f in rows if r == "assistant" and f == "stop"]
        a = [t for r, t, f in rows if r == "assistant"]
        users += len(u)
        stops += len(s)
        sessions_with_completion += 1 if s else 0
        for i, ts in enumerate(u):
            nxt = u[i + 1] if i + 1 < len(u) else float("inf")
            if any(ts <= x < nxt for x in s):
                paired += 1
            prior = [x for x in a if x <= ts]
            if prior:
                gap = ts - max(prior)
                if gap >= 0:
                    gaps.append(gap)
    gaps.sort()

    def q(p):
        return round(gaps[min(len(gaps) - 1, int(len(gaps) * p))], 1) if gaps else None

    return {
        "TURN_START_DETECTABLE": "YES",
        "TURN_START_BASIS": "user-role message rows carry a timestamp; no explicit turn identifier exists",
        "TURN_END_DETECTABLE": "PARTIAL",
        "TURN_END_BASIS": ("an assistant row whose finish_reason is 'stop' marks a completed turn, while "
                           "'tool_calls' marks an intermediate step; interrupted and abnormally exited turns "
                           "leave no completion marker at all"),
        "NEXT_USER_DETECTABLE": "YES",
        "NEXT_USER_BASIS": "the next user-role row in the same session, or the session ending",
        "TURN_BOUNDARY_RELIABILITY": "MEDIUM",
        "TURN_BOUNDARY_EVIDENCE": {
            "user_rows_considered": users,
            "stop_marked_completions": stops,
            "completion_per_user_row": round(stops / users, 3) if users else None,
            "user_rows_with_a_completion_in_their_window": paired,
            "window_paired_share": round(paired / users, 3) if users else None,
            "sessions_with_at_least_one_completion": sessions_with_completion,
            "reason_not_high": ("completions are not 1:1 with user rows (mid-turn steering messages are also "
                               "user rows) and an explicit per-turn identifier is absent, so a turn is "
                               "inferred from ordering rather than recorded"),
        },
        "FOLLOWUP_GAP_DISTRIBUTION_S": {"n": len(gaps), "p10": q(0.10), "p50": q(0.50), "p90": q(0.90),
                                        "unit": "seconds", "status": "EXPERIMENTAL",
                                        "no_threshold_is_applied": True},
    }


# ------------------------------------------------------------------ join coverage
def join_coverage(records: list, sources: dict, shadow_stats, generation: str) -> dict:
    rows = [r for r in records
            if (r.get("deployment_generation") or shadow_stats.GENERATION_LEGACY) == generation
            and r.get("platform") == "feishu" and r.get("turn_origin") == "user"
            and not shadow_stats.classify(r, sources).startswith("excluded")]
    by_session = collections.defaultdict(list)
    for r in rows:
        by_session[session_key(r)].append(r)
    single = [s for s, v in by_session.items() if len(v) == 1]
    multi = [s for s, v in by_session.items() if len(v) > 1]
    single_turns = sum(len(by_session[s]) for s in single)
    multi_turns = sum(len(by_session[s]) for s in multi)
    return {
        "GENERATION": generation,
        "TOTAL_HUMAN_TURNS": len(rows),
        "SESSIONS": len(by_session),
        "SINGLE_TURN_SESSIONS": len(single),
        "SINGLE_TURN_SESSION_TURNS": single_turns,
        "MULTI_TURN_SESSIONS": len(multi),
        "MULTI_TURN_SESSION_TURNS": multi_turns,
        "MAX_TURNS_IN_ONE_SESSION": max((len(v) for v in by_session.values()), default=0),
        "PER_SIGNAL": {
            "session_and_session_model_aggregates (tokens, cache, cost, tool_call_count, rewind_count)": {
                "EXACTLY_JOINABLE_TURNS": single_turns,
                "PARTIALLY_JOINABLE_TURNS": 0,
                "UNJOINABLE_TURNS": multi_turns,
                "rule": "one admitted human turn in the session inside the window",
            },
            "session_terminal_fields (end_reason, runtime failure flags)": {
                "EXACTLY_JOINABLE_TURNS": single_turns,
                "PARTIALLY_JOINABLE_TURNS": multi_turns,
                "UNJOINABLE_TURNS": 0,
                "rule": "exact on a single-turn session; on a multi-turn session it describes the session, "
                        "and at best the last turn",
            },
            "message_level_structural (timestamps, finish_reason, follow-up gap)": {
                "EXACTLY_JOINABLE_TURNS": len(rows),
                "PARTIALLY_JOINABLE_TURNS": 0,
                "UNJOINABLE_TURNS": 0,
                "rule": "inferred from role ordering + timestamps, never from a turn identifier",
            },
            "per_request_telemetry_without_a_turn_key (context estimate, latency, failure attribution)": {
                "EXACTLY_JOINABLE_TURNS": 0,
                "PARTIALLY_JOINABLE_TURNS": len(rows),
                "UNJOINABLE_TURNS": 0,
                "rule": "joinable by time window and model only; never claimed as exact",
            },
            "per_turn_token_and_cache_values": {
                "EXACTLY_JOINABLE_TURNS": single_turns,
                "PARTIALLY_JOINABLE_TURNS": 0,
                "UNJOINABLE_TURNS": multi_turns,
                "rule": "session totals only; replication onto sibling turns is forbidden",
            },
        },
    }


# ------------------------------------------------------------------ structure
def struggle_structure(coverage_canonical: dict, rel: list) -> dict:
    return {
        "HARD_STRUGGLE_SIGNAL_AVAILABLE": "PARTIAL",
        "HARD_STRUGGLE_DEFINITION": ("composed only of explicit failure-class structural events: a truncation "
                                     "or incomplete completion marker, a recorded runtime failure, a handoff or "
                                     "compression failure, or a terminal status that names failure"),
        "HARD_CANDIDATES": [
            {"signal": "EXPLICIT_RUNTIME_FAILURE", "available_now": "PARTIAL", "per_turn_exact": "NO",
             "false_positive_risk": "LOW", "false_negative_risk": "HIGH",
             "why": "recorded per session, so a turn cannot claim it alone"},
            {"signal": "EXCEPTION_TERMINATION", "available_now": "NO", "per_turn_exact": "NO",
             "false_positive_risk": "LOW", "false_negative_risk": "HIGH",
             "why": "no per-turn exception record exists historically"},
            {"signal": "CANCELLATION", "available_now": "NO", "per_turn_exact": "NO",
             "false_positive_risk": "LOW", "false_negative_risk": "HIGH",
             "why": "interruption is only observable through a live hook, never from the store"},
            {"signal": "ABNORMAL_END_REASON", "available_now": "PARTIAL", "per_turn_exact": "NO",
             "false_positive_risk": "MEDIUM", "false_negative_risk": "MEDIUM",
             "why": "session-level; the observed values are lifecycle reasons, not failures"},
            {"signal": "COMPRESSION_FAILURE", "available_now": "PARTIAL", "per_turn_exact": "NO",
             "false_positive_risk": "LOW", "false_negative_risk": "HIGH", "why": "session-level flag"},
            {"signal": "HANDOFF_FAILURE", "available_now": "PARTIAL", "per_turn_exact": "NO",
             "false_positive_risk": "LOW", "false_negative_risk": "HIGH", "why": "session-level flag"},
            {"signal": "TRUNCATED_COMPLETION (assistant finish_reason incomplete/length)",
             "available_now": "YES", "per_turn_exact": "PARTIAL", "false_positive_risk": "LOW",
             "false_negative_risk": "MEDIUM",
             "why": "per-message marker, joined to a turn by ordering; rare but clean"},
        ],
        "SOFT_STRUGGLE_FEATURES": [
            {"signal": "tool_call_count", "available_now": "YES", "grain": "PER_SESSION (count) / "
             "PER_MESSAGE (tool rows)", "exact_per_turn": "NO",
             "expected_direction": "more calls for the same task is consistent with struggle",
             "ambiguity": "a task may simply need many tools", "false_positive_risk": "HIGH"},
            {"signal": "turn_duration", "available_now": "PARTIAL (derived)", "grain": "PER_MESSAGE (derived)",
             "exact_per_turn": "PARTIAL", "expected_direction": "longer is consistent with struggle",
             "ambiguity": "long turns are also normal for large tasks", "false_positive_risk": "HIGH"},
            {"signal": "rewind", "available_now": "PARTIAL", "grain": "PER_SESSION", "exact_per_turn": "NO",
             "expected_direction": "a rewind is a strong negative hint",
             "ambiguity": "may reflect the user changing their mind", "false_positive_risk": "MEDIUM"},
            {"signal": "user follow-up gap", "available_now": "YES", "grain": "PER_MESSAGE (derived)",
             "exact_per_turn": "PARTIAL", "expected_direction": "a very short gap is consistent with an "
             "unsatisfactory answer", "ambiguity": "normal conversation is also fast", "false_positive_risk": "HIGH"},
            {"signal": "input/output token volume", "available_now": "YES", "grain": "PER_SESSION",
             "exact_per_turn": "NO", "expected_direction": "unclear by itself",
             "ambiguity": "depends entirely on the task", "false_positive_risk": "HIGH"},
            {"signal": "cache reuse ratio", "available_now": "PARTIAL", "grain": "PER_SESSION_MODEL",
             "exact_per_turn": "NO", "expected_direction": "not a struggle signal; it prices a model switch",
             "ambiguity": "n/a", "false_positive_risk": "MEDIUM"},
            {"signal": "API call pressure", "available_now": "PARTIAL", "grain": "PER_SESSION",
             "exact_per_turn": "NO", "expected_direction": "more calls per turn is consistent with struggle",
             "ambiguity": "loop count also reflects the task shape", "false_positive_risk": "HIGH"},
            {"signal": "cost", "available_now": "YES", "grain": "PER_SESSION / PER_SESSION_MODEL",
             "exact_per_turn": "NO", "expected_direction": "a cost outcome, not a struggle signal",
             "ambiguity": "n/a", "false_positive_risk": "MEDIUM"},
        ],
        "DEFAULT_MODEL_STRUGGLE_VECTOR_V1": {
            "hard_failure": "enum or null — PARTIAL (truncation marker joinable; runtime failure session-level only)",
            "turn_duration_ms": "number or null — PARTIAL (derived from message timestamps)",
            "tool_call_count": "number or null — SESSION grain only (EXACT_PER_TURN: NO)",
            "rewind_delta": "number or null — SESSION grain only (EXACT_PER_TURN: NO)",
            "followup_gap_ms": "number or null — PARTIAL (derived, must be measured at the next turn)",
            "input_tokens": "number or null — SESSION grain only (EXACT_PER_TURN: NO)",
            "output_tokens": "number or null — SESSION grain only (EXACT_PER_TURN: NO)",
            "cache_read_tokens": "number or null — SESSION grain only (EXACT_PER_TURN: NO)",
            "cache_write_tokens": "number or null — SESSION grain only (EXACT_PER_TURN: NO)",
            "cost": "number or null — SESSION grain only (EXACT_PER_TURN: NO)",
            "attribution_quality": "EXACT | PARTIAL | UNAVAILABLE",
            "note": "no prompt, response, tool argument, tool result, memory or dossier field may be added; "
                    "the vector describes execution structure, never content",
        },
        "DEFAULT_MODEL_STRUGGLE_SIGNAL_AVAILABLE": "PARTIAL",
        "DEFAULT_MODEL_STRUGGLE_SIGNAL_REASON": ("exact per-turn values do not exist for the token, cache, cost "
                                                 "and tool aggregates, so this round only supports features with "
                                                 "an explicit attribution quality"),
        "ROUTING_CORRECTNESS_GROUND_TRUTH_AVAILABLE": "NO",
        "COUNTERFACTUAL_MIMO_OUTCOME_AVAILABLE": "NO",
        "GROUND_TRUTH_BOUNDARY": ("an observed struggle on a low-confidence recommendation can at most support "
                                 "'low confidence correlates with default-model struggle'; it can never support "
                                 "'the other model would have solved it'"),
        "SINGLE_TURN_SESSION_SUBSET": {
            "canonical_turns": coverage_canonical.get("SINGLE_TURN_SESSION_TURNS"),
            "selection_bias": "YES",
            "bias_reason": "a session with one admitted turn is not representative of Hermes traffic",
            "use": "exploratory only",
        },
        "SESSION_AGGREGATE_REPLICATION_FORBIDDEN": "YES",
    }


# ------------------------------------------------------------------ cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="privacy-safe outcome-signal audit (read only)")
    ap.add_argument("--generation", default="all", help="deployment generation, or 'all'")
    ap.add_argument("--log-dir", default=os.environ.get("JEV_LOG_DIR")
                    or str(pathlib.Path(os.environ.get("HERMES_HOME") or "/opt/data") / "logs" / "router"))
    ap.add_argument("--state-db", default=str(pathlib.Path(os.environ.get("HERMES_STATE_DB")
                    or (pathlib.Path(os.environ.get("HERMES_HOME") or "/opt/data") / "state.db"))))
    ap.add_argument("--context-guard-telemetry", default=str(pathlib.Path(os.environ.get("HERMES_HOME")
                    or "/opt/data") / "logs" / "context-guard" / "telemetry.jsonl"))
    ap.add_argument("--json", default=None, help="write the aggregate here (never inside the deployment)")
    args = ap.parse_args(argv)

    try:
        shadow_stats = _load_sibling("os_shadow_stats", "shadow_stats.py")
    except Exception as exc:
        print(f"cannot load shadow_stats.py: {exc}", file=sys.stderr)
        return 2

    records = []
    for p in sorted(pathlib.Path(args.log_dir).glob("shadow-*.jsonl")):
        for line in p.read_text(errors="replace").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    db = StateDB(pathlib.Path(args.state_db))
    if not db.ok:
        print("state database not found; this audit needs it for grain and boundary evidence",
              file=sys.stderr)
        return 2
    sources = {str(i): (s or "") for i, s in db.rows("select id, source from sessions")}
    cg_path = pathlib.Path(args.context_guard_telemetry)
    cg_rows = []
    if cg_path.exists():
        for line in cg_path.read_text(errors="replace").splitlines():
            if line.strip():
                try:
                    cg_rows.append(json.loads(line))
                except Exception:
                    pass

    gens = collections.Counter(r.get("deployment_generation") or shadow_stats.GENERATION_LEGACY
                               for r in records)
    out = {
        "RECORDS_TOTAL": len(records),
        "GENERATION_DISTRIBUTION": dict(gens),
        "INPUTS": {"telemetry": "shadow jsonl (read only)", "state_db": "read-only structural columns",
                   "context_guard_telemetry": "aggregate coverage counts only"},
        "CONTENT_READ": "NO",
        "SIGNAL_GRANULARITY_MATRIX": signal_matrix(db, cg_rows),
        "TURN_BOUNDARY": turn_boundary(db, sources),
        "PER_TURN_EXACTNESS": {
            "INPUT_TOKENS_PER_TURN_EXACT": "NO",
            "OUTPUT_TOKENS_PER_TURN_EXACT": "NO",
            "CACHE_READ_PER_TURN_EXACT": "NO",
            "CACHE_WRITE_PER_TURN_EXACT": "NO",
            "COST_PER_TURN_EXACT": "NO",
            "PROMPT_CACHE_HIT_PER_TURN": "NO",
            "reason": "these exist as session and session×model totals with time windows; nothing records a "
                      "per-request delta against a turn identifier, and first_seen/last_seen do not say which "
                      "turn a token belongs to",
            "single_turn_session_exception": "SINGLE_TURN_SESSION_EXACT_JOIN = YES",
        },
        "CACHE_SWITCH_COST_MEASURABLE_NOW": "PARTIAL",
        "CACHE_REUSE_CONTEXT": {
            "grain": "PER_SESSION_MODEL",
            "fields": ["cache_read_tokens", "input_tokens", "cache_write_tokens", "api_call_count",
                       "first_seen", "last_seen"],
            "usable_for": "context only: a session-window reuse ratio per model",
            "not_usable_for": "a per-turn counterfactual cost of switching model",
        },
        "REGISTRY": {},
        "RELIABILITY_NOTES": [
            "session and session×model aggregates are cumulative and updated in place, so they are not "
            "reconstructible per turn after the fact",
        ],
    }
    gens_to_do = [CANONICAL, LEGACY] if args.generation == "all" else [args.generation]
    coverage = {}
    for g in gens_to_do:
        if g in gens:
            coverage[g] = join_coverage(records, sources, shadow_stats, g)
    out["HISTORICAL_OUTCOME_JOIN_COVERAGE"] = coverage
    canon_cov = coverage.get(CANONICAL, {})
    out["STRUGGLE_STRUCTURE"] = struggle_structure(canon_cov, [])
    out["RETROSPECTIVE_STRUGGLE_ANALYSIS"] = (
        "NOT_POSSIBLE" if not canon_cov.get("TOTAL_HUMAN_TURNS") else "DESCRIPTIVE_ONLY")
    out["RETROSPECTIVE_NOTE"] = ("exact per-turn aggregates are unavailable for multi-turn sessions, and the "
                                 "single-turn subset is tiny and biased, so no incidence or median difference "
                                 "may be reported as evidence")
    out["PROSPECTIVE_OUTCOME_TELEMETRY_REQUIRED"] = "YES"
    out["HOOK_AUDIT"] = hook_audit()
    out["RELIABLE_POST_TURN_HOOK_AVAILABLE"] = "PARTIAL"
    out["ROUTING_QUALITY_INTEGRATION"] = ("optional: pass this aggregate to "
                                          "tools/routing_quality.py --outcome-aggregate to see a correlation "
                                          "view; the routing-quality output is unchanged without it")
    out["CORRELATION_NOT_CAUSATION"] = "YES"
    db.close()

    payload = json.dumps(out, indent=2, sort_keys=True, default=str)
    if args.json:
        # The rule states the hazard instead of banning a whole directory tree: never write inside
        # the telemetry or log directory, never touch the state database or the plugin tree, and
        # never overwrite a file that already exists. A fresh aggregate under the caller's own
        # report directory is theirs to place.
        dest = pathlib.Path(args.json).resolve()
        home = pathlib.Path(os.environ.get("HERMES_HOME") or "/opt/data").resolve()
        forbidden = [pathlib.Path(args.log_dir).resolve(), home / "logs", home / "plugins",
                     pathlib.Path(args.state_db).resolve()]
        inside = [f for f in forbidden[:3] if f == dest or f in dest.parents]
        if inside or dest == forbidden[3] or dest.exists():
            reason = "inside the deployment" if inside or dest == forbidden[3] else "would overwrite an existing file"
            print(f"refusing to write {reason}: {dest}", file=sys.stderr)
            return 4
        dest.write_text(payload + "\n")
        print(f"aggregate written to {dest}")
    else:
        print(payload)
    return 0


def hook_audit() -> dict:
    """Lifecycle hooks available to a plugin, from the read-only source audit."""
    return {
        "RELIABLE_POST_TURN_HOOK_AVAILABLE": "PARTIAL",
        "HOOKS": [
            {"hook": "post_llm_call", "exists": "YES", "when_fires": "once per turn, after the tool loop",
             "can_join_turn": "YES (turn_id, session_id, task_id)", "content_free_metrics":
             "NO payload metrics (payload carries user/assistant text, which a recorder must ignore)",
             "risk": "skipped on some abnormal early exits; carrying content means a recorder must be "
                     "explicitly content-blind"},
            {"hook": "post_api_request", "exists": "YES",
             "when_fires": "once per physical provider request, turn-scoped",
             "can_join_turn": "YES (turn_id, api_call_count, api_request_id)",
             "content_free_metrics": "YES (usage token buckets incl. cache read/write, finish_reason, "
                                     "api_duration, response model)",
             "risk": "high volume; a recorder must aggregate rather than persist per event"},
            {"hook": "api_request_error", "exists": "YES", "when_fires": "once per failed request",
             "can_join_turn": "YES (turn_id, api_call_count)",
             "content_free_metrics": "YES (status_code, error type, retry_count, retryable, duration)",
             "risk": "error bodies may be unredacted; a recorder must store enums only"},
            {"hook": "pre_api_request", "exists": "YES", "when_fires": "once per request, before sending",
             "can_join_turn": "YES",
             "content_free_metrics": "YES (message_count, tool_count, approx_input_tokens, request_char_count)",
             "risk": "an estimate, not a measurement"},
            {"hook": "agent_loop_stopped", "exists": "YES", "when_fires": "when a running turn is interrupted",
             "can_join_turn": "PARTIAL (session_key + platform + reason)",
             "content_free_metrics": "YES (reason, invalidation_reason)", "risk": "interruption only"},
            {"hook": "on_session_end", "exists": "DOCUMENTED",
             "when_fires": "documented as a turn/session terminal event carrying completed/failed/interrupted "
                           "and turn_exit_reason",
             "can_join_turn": "YES (turn_id) per the documented payload",
             "content_free_metrics": "YES", "risk": "the plugin-visible fire site could not be confirmed from "
                                                   "the read-only source audit, so it is reported as documented "
                                                   "rather than verified"},
            {"hook": "context engine on_turn_complete", "exists": "YES",
             "when_fires": "per turn, from the finalization seam",
             "can_join_turn": "YES (turn_id, task_id, api_call_count, interrupted, failed, turn_exit_reason)",
             "content_free_metrics": "YES (usage shape)",
             "risk": "internal extension point (engine override only), and abnormal early returns skip it"},
        ],
        "CONCLUSION": "no single hook is guaranteed for every turn; a prospective recorder should combine the "
                      "per-request hooks with a turn-terminal hook and must tolerate missing terminals",
    }


if __name__ == "__main__":
    raise SystemExit(main())
