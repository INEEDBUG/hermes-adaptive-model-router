#!/usr/bin/env python3
"""Routing-quality aggregates for the JEV shadow router — read only, offline, generation aware.

What this answers
-----------------
The shadow telemetry records what the routing service recommended (``route``) and what the
router policy would have executed (``would_execute``). Those are two different things, and
the gap between them is the policy's own decision, not the service's:

* ``DIRECT_JEV_ROUTE``    — the route the JEV service recommended for the turn
* ``POLICY_WOULD_EXECUTE`` — the route the router policy would have used, given its
  confidence and margin gates and the routes this deployment declares as available

Override reasons are **not** inferred from field names. The frozen policy functions
(``router.shadow._preferred_route`` / ``_executable_route``) are imported from a runtime root
and replayed on each recorded decision, with the thresholds the caller states. The replay is
also checked against what the deployment actually recorded (``POLICY_REPLAY_FIDELITY``), so a
taxonomy that no longer matches production shows up instead of being trusted.

Generation separation is mandatory
----------------------------------
Legacy and canonical records disagree on the meaning of ``would_execute`` and on how route
availability is resolved, so routing-policy figures are always reported for exactly one
deployment generation. ``--generation all`` yields record counts only and refuses to emit a
routing figure.

This tool writes nothing. It never opens the production database for writing, never touches a
counter file, and refuses a ``--json`` destination inside the deployment's own directories.
No prompt, dossier, turn identifier, session identifier or tool argument is ever printed;
correlations are hashed and only aggregates leave the process.
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
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FEATURES = ["coding", "debugging", "research", "tool_use", "shell", "long_context",
            "destructive_action", "production_change"]
CONF_THRESHOLDS = [0.55, 0.60, 0.65, 0.70, 0.75]
MARGIN_THRESHOLDS = [0.10, 0.15, 0.20, 0.30]
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


# --------------------------------------------------------------------------- helpers
def _load_sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_records(logdir: pathlib.Path):
    recs = []
    for p in sorted(logdir.glob("shadow-*.jsonl")):
        for line in p.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            r["_day"] = p.name.replace("shadow-", "").replace(".jsonl", "")
            recs.append(r)
    return recs


def quantiles(values, qs=QUANTILES):
    xs = sorted(float(v) for v in values)
    if not xs:
        return {f"p{int(q*100)}": None for q in qs}
    if len(xs) == 1:
        return {f"p{int(q*100)}": round(xs[0], 4) for q in qs}
    out = {}
    for q in qs:
        k = (len(xs) - 1) * q
        lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
        out[f"p{int(q*100)}"] = round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 4)
    return out


def margin_of(rec):
    a, b = rec.get("p_deepseek"), rec.get("p_mimo")
    return None if a is None or b is None else round(abs(float(a) - float(b)), 4)


def decision_of(rec):
    """Rebuild the decision dict the policy saw, from content-free recorded fields."""
    choice = rec.get("route")
    if isinstance(choice, str) and choice.startswith("ERROR:"):
        choice = None
    probs = {}
    if rec.get("p_deepseek") is not None:
        probs["deepseek_flash"] = rec["p_deepseek"]
    if rec.get("p_mimo") is not None:
        probs["mimo_pro"] = rec["p_mimo"]
    return {"ok": bool(rec.get("success")), "confidence": rec.get("confidence") or 0.0,
            "probabilities": probs, "choice": choice, "error": rec.get("error")}


def _hash_id(value) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:12]


def session_key(rec: dict) -> str:
    """The session a record belongs to. ``turn_id`` is ``<session>:<turn>[:<attempt>]``; the
    session is everything before the first separator, so consecutive turns of one conversation
    are recognised as consecutive (the same rule the telemetry reader uses)."""
    tid = str(rec.get("turn_id") or rec.get("session_id") or "")
    return tid.split(":")[0]


# --------------------------------------------------------------------------- policy replay
class Policy:
    """The frozen router policy, imported from a runtime root and replayed."""

    def __init__(self, runtime_root: pathlib.Path, conf: float, margin: float,
                 available: tuple | None):
        sys.path.insert(0, str(runtime_root))
        package_dir = runtime_root / "router"
        pkg_spec = importlib.util.spec_from_file_location(
            "rq_router", package_dir / "__init__.py", submodule_search_locations=[str(package_dir)])
        spec = importlib.util.spec_from_file_location("rq_router.shadow", package_dir / "shadow.py")
        if pkg_spec is None or spec is None or pkg_spec.loader is None or spec.loader is None:
            raise RuntimeError("policy module not found")
        pkg = importlib.util.module_from_spec(pkg_spec)
        shadow = importlib.util.module_from_spec(spec)
        sys.modules["rq_router"] = pkg
        pkg_spec.loader.exec_module(pkg)
        sys.modules["rq_router.shadow"] = shadow
        spec.loader.exec_module(shadow)
        self.shadow = shadow
        self.config = shadow.config
        self.conf_default = conf
        self.margin_default = margin
        self.available = available
        self.source_digest = hashlib.sha256(
            (runtime_root / "router" / "shadow.py").read_bytes()).hexdigest()[:16]

    def replay(self, decision, conf=None, margin=None, available=None):
        conf = self.conf_default if conf is None else conf
        margin = self.margin_default if margin is None else margin
        if available is not None:
            avail = tuple(available)
        elif self.available is not None:
            avail = tuple(self.available)
        else:
            avail = tuple(self.shadow.config.available_routes())
        oc, om = self.config.min_confidence, self.config.min_margin
        oa = self.shadow.config.available_routes
        self.config.min_confidence = lambda: conf
        self.config.min_margin = lambda: margin
        self.shadow.config.available_routes = lambda: tuple(avail)
        try:
            preferred = self.shadow._preferred_route(decision)
            return preferred, self.shadow._executable_route(preferred)
        finally:
            self.config.min_confidence, self.config.min_margin = oc, om
            self.shadow.config.available_routes = oa


def override_reason(decision, preferred, policy, *, conf_threshold, margin_threshold):
    """Classify DIRECT != POLICY through the canonical code path, never through a name guess."""
    if not decision.get("ok"):
        return "FAIL_CLOSED_ESCALATION" if policy != decision.get("choice") else "FAIL_CLOSED"
    probs = sorted((decision.get("probabilities") or {}).values(), reverse=True)
    p1 = probs[0] if probs else 0.0
    p2 = probs[1] if len(probs) > 1 else 0.0
    low_conf = (decision.get("confidence") or 0.0) < conf_threshold
    low_margin = (p1 - p2) < margin_threshold
    unavailable = preferred != policy
    if low_conf and low_margin:
        base = "CONFIDENCE_AND_MARGIN_ESCALATION"
    elif low_conf:
        base = "LOW_CONFIDENCE_ESCALATION"
    elif low_margin:
        base = "LOW_MARGIN_ESCALATION"
    else:
        base = "ROUTE_UNAVAILABLE"
    return "ROUTE_UNAVAILABLE_AFTER_ESCALATION" if (unavailable and base != "ROUTE_UNAVAILABLE") else base


def reason_class(reason: str) -> str:
    if reason.startswith("LOW_CONFIDENCE_ESCALATION"):
        return "LOW_CONFIDENCE_ONLY"
    if reason.startswith("LOW_MARGIN_ESCALATION"):
        return "LOW_MARGIN_ONLY"
    if reason.startswith("CONFIDENCE_AND_MARGIN"):
        return "BOTH"
    return reason


# --------------------------------------------------------------------------- analysis
def cohort(recs, generation, sources, shadow_stats):
    """Canonical cohort: one generation, real human turns only."""
    out = []
    for r in recs:
        if (r.get("deployment_generation") or shadow_stats.GENERATION_LEGACY) != generation:
            continue
        if r.get("platform") != "feishu" or r.get("turn_origin") != "user":
            continue
        if shadow_stats.classify(r, sources).startswith("excluded"):
            continue
        out.append(r)
    return out


def analyse(rows, policy, conf_threshold, margin_threshold, generation, with_sensitivity,
            with_features, with_longitudinal):
    res = {"GENERATION": generation}
    succ = [r for r in rows if r.get("success")]
    timeouts = [r for r in rows if not r.get("success")
                and "timeout" in str(r.get("error") or "").lower()]
    errs = [r for r in rows if not r.get("success") and r not in timeouts]
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]

    res.update({
        "HUMAN_TURNS": len(rows),
        "SAMPLE_DAYS": len({r["_day"] for r in rows}),
        "DAYS": sorted({r["_day"] for r in rows}),
        "DAY_CONCENTRATION": (round(max(collections.Counter(r["_day"] for r in rows).values()) / len(rows), 4)
                              if rows else None),
        "SUCCESSFUL_JEV_DECISIONS": len(succ),
        "JEV_TIMEOUTS": len(timeouts),
        "JEV_ERRORS": len(errs),
        "JEV_LATENCY_MS": {"n": len(lat), "mean": round(statistics.fmean(lat), 1) if lat else None,
                           **quantiles(lat)},
        "JEV_RESOLVED_VERSION": dict(collections.Counter(r.get("jev_model") for r in rows)),
    })

    direct = collections.Counter(r.get("route") for r in succ)
    policy_dist = collections.Counter(r.get("would_execute") for r in succ)
    res["DIRECT_ROUTE_DISTRIBUTION"] = {(k or "null"): v for k, v in direct.items()}
    res["POLICY_ROUTE_DISTRIBUTION"] = {(k or "null"): v for k, v in policy_dist.items()}
    matrix = collections.Counter((r.get("route") or "null", r.get("would_execute") or "null") for r in succ)
    res["DIRECT_TO_POLICY_MATRIX"] = {f"{a} -> {b}": v for (a, b), v in sorted(matrix.items())}

    reasons = collections.Counter()
    escalation = collections.Counter()
    overrides = replay_matches = replayed = 0
    for r in succ:
        d = decision_of(r)
        preferred, policy_route = policy.replay(d)
        replayed += 1
        if (policy_route or None) == (r.get("would_execute") or None):
            replay_matches += 1
        if (r.get("route") or None) != (policy_route or None):
            overrides += 1
            reason = override_reason(d, preferred, policy_route,
                                     conf_threshold=conf_threshold, margin_threshold=margin_threshold)
            reasons[reason] += 1
            if r.get("route") == "deepseek_flash" and policy_route == "mimo_pro":
                escalation[reason_class(reason)] += 1
    res["POLICY_OVERRIDE_COUNT"] = overrides
    res["POLICY_OVERRIDE_RATE"] = round(overrides / len(succ), 4) if succ else None
    res["OVERRIDE_REASON_COUNTS"] = dict(reasons)
    res["DEEPSEEK_TO_MIMO_OVERRIDE"] = {"total": sum(escalation.values()), "breakdown": dict(escalation)}
    res["POLICY_REPLAY_FIDELITY"] = {
        "replayed": replayed,
        "matches_recorded_would_execute": replay_matches,
        "fidelity": round(replay_matches / replayed, 4) if replayed else None,
        "note": "below 1.0 means the replay (thresholds / availability / code) differs from what "
                "produced the records; treat the override taxonomy as approximate then",
    }

    confs = [r["confidence"] for r in succ if r.get("confidence") is not None]
    margins = [m for m in (margin_of(r) for r in succ) if m is not None]
    res["CONFIDENCE_DISTRIBUTION"] = {"n": len(confs), **quantiles(confs)}
    res["MARGIN_DISTRIBUTION"] = {"n": len(margins), **quantiles(margins)}
    if confs:
        res["CONFIDENCE_VALUES"] = sorted(round(float(c), 4) for c in confs)
    res["CONFIDENCE_BINS"] = bin_counts(confs, [(None, 0.50), (0.50, 0.60), (0.60, 0.65), (0.65, 0.75),
                                                (0.75, 0.90), (0.90, None)],
                                        ["<0.50", "0.50-<0.60", "0.60-<0.65", "0.65-<0.75", "0.75-<0.90", ">=0.90"])
    res["MARGIN_BINS"] = bin_counts(margins, [(None, 0.10), (0.10, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, None)],
                                    ["<0.10", "0.10-<0.15", "0.15-<0.30", "0.30-<0.50", ">=0.50"])
    res["NEAR_THRESHOLD_DEFINITION"] = (f"successful decisions with |confidence-{conf_threshold}| < 0.05 "
                                        f"OR |margin-{margin_threshold}| < 0.05")
    res["NEAR_THRESHOLD_COUNT"] = sum(
        1 for r in succ
        if (r.get("confidence") is not None
            and round(abs(float(r["confidence"]) - conf_threshold), 6) < 0.05)
        or (margin_of(r) is not None
            and round(abs(margin_of(r) - margin_threshold), 6) < 0.05))

    if with_sensitivity:
        res["POLICY_SENSITIVITY_TABLE"] = sensitivity(policy, succ)

    if with_features:
        res["TASK_FEATURE_BREAKDOWN"] = feature_breakdown(succ)
        res["TASK_FEATURE_MULTILABEL"] = True

    if with_longitudinal:
        res.update(longitudinal(rows))

    res["QUALITY_GROUND_TRUTH_AVAILABLE"] = "NO"
    res["QUALITY_GROUND_TRUTH_NOTE"] = ("confidence, margin, task features and would_execute are model "
                                        "signals, not correctness labels; the telemetry carries no outcome "
                                        "that says whether the chosen route actually performed better")
    return res


def bin_counts(values, edges, labels):
    c = collections.Counter()
    for v in values:
        v = float(v)
        for (lo, hi), lab in zip(edges, labels):
            if (lo is None or v >= lo) and (hi is None or v < hi):
                c[lab] += 1
                break
    return {lab: c.get(lab, 0) for lab in labels}


def sensitivity(policy, succ):
    table = []
    for ct in CONF_THRESHOLDS:
        for mt in MARGIN_THRESHOLDS:
            dist = collections.Counter()
            ov = esc = 0
            for r in succ:
                _, pol = policy.replay(decision_of(r), conf=ct, margin=mt)
                dist[pol or "null"] += 1
                if (pol or None) != (r.get("route") or None):
                    ov += 1
                if r.get("route") == "deepseek_flash" and pol == "mimo_pro":
                    esc += 1
            n = len(succ) or 1
            table.append({"confidence_threshold": ct, "margin_threshold": mt,
                          "deepseek_share": round(dist.get("deepseek_flash", 0) / n, 4),
                          "mimo_share": round(dist.get("mimo_pro", 0) / n, 4),
                          "null_share": round(dist.get("null", 0) / n, 4),
                          "override_rate": round(ov / n, 4),
                          "deepseek_to_mimo_escalation_rate": round(esc / n, 4)})
    return table


def feature_breakdown(succ):
    out = {}
    for f in FEATURES:
        sub = [r for r in succ if r.get(f)]
        if not sub:
            out[f] = {"sample_count": 0}
            continue
        dd = collections.Counter(r.get("would_execute") or "null" for r in sub)
        dr = collections.Counter(r.get("route") or "null" for r in sub)
        n = len(sub)
        out[f] = {"sample_count": n,
                  "direct_share_deepseek": round(dr.get("deepseek_flash", 0) / n, 4),
                  "direct_share_mimo": round(dr.get("mimo_pro", 0) / n, 4),
                  "policy_share_deepseek": round(dd.get("deepseek_flash", 0) / n, 4),
                  "policy_share_mimo": round(dd.get("mimo_pro", 0) / n, 4),
                  "policy_share_null": round(dd.get("null", 0) / n, 4),
                  "override_rate": round(sum(1 for r in sub if (r.get("route") or None) != (r.get("would_execute") or None)) / n, 4),
                  "median_confidence": quantiles([r["confidence"] for r in sub if r.get("confidence") is not None])["p50"],
                  "median_margin": quantiles([m for m in (margin_of(r) for r in sub) if m is not None])["p50"],
                  "timeout_rate": round(sum(1 for r in sub if not r.get("success")) / n, 4)}
    return out


def longitudinal(rows):
    """Per-session content-free aggregates. Identifiers are hashed and never emitted."""
    sess = collections.defaultdict(list)
    for r in rows:
        sess[_hash_id(session_key(r))].append(r)
    sizes = collections.Counter(len(v) for v in sess.values())
    trans = collections.Counter()
    conf_d, margin_d = [], []
    flips = pairs = 0
    for _, rs in sess.items():
        rs = sorted(rs, key=lambda r: str(r.get("timestamp") or ""))
        for a, b in zip(rs, rs[1:]):
            pairs += 1
            pa, pb = a.get("would_execute") or "null", b.get("would_execute") or "null"
            trans[f"{pa} -> {pb}"] += 1
            if pa != pb:
                flips += 1
            if a.get("confidence") is not None and b.get("confidence") is not None:
                conf_d.append(abs(float(a["confidence"]) - float(b["confidence"])))
            ma, mb = margin_of(a), margin_of(b)
            if ma is not None and mb is not None:
                margin_d.append(abs(ma - mb))
    return {
        "SESSIONS_OBSERVED": len(sess),
        "TURNS_PER_SESSION_DISTRIBUTION": {str(k): v for k, v in sorted(sizes.items())},
        "CONSECUTIVE_TURN_PAIRS": pairs,
        "POLICY_ROUTE_TRANSITION_MATRIX": dict(trans),
        "POLICY_ROUTE_FLIP_RATE": round(flips / pairs, 4) if pairs else None,
        "CONFIDENCE_VOLATILITY_MEDIAN": quantiles(conf_d)["p50"],
        "MARGIN_VOLATILITY_MEDIAN": quantiles(margin_d)["p50"],
        "LONGITUDINAL_SAMPLE": "INSUFFICIENT" if (len(sess) < 5 or max(sizes.values(), default=0) < 3) else "AVAILABLE",
        "LONGITUDINAL_NOTE": "never mixes generations into a sequence",
    }


# --------------------------------------------------------------------------- signals
def signal_inventory(db_path: pathlib.Path):
    """Probe which outcome/context/cache signals already exist (schema level, read only)."""
    import sqlite3
    res = {"STATE_DB_PRESENT": db_path.exists(), "SIGNALS": []}
    cols = {}
    if db_path.exists():
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for t in ("sessions", "session_model_usage", "messages"):
                cols[t] = {c[1] for c in con.execute(f"PRAGMA table_info({t})")}
            populated = {}
            for t, col, sql in (("session_model_usage", "cache_read_tokens",
                                 "select count(*) from session_model_usage where cache_read_tokens > 0"),
                                ("sessions", "input_tokens", "select count(*) from sessions where input_tokens > 0"),
                                ("messages", "token_count", "select count(*) from messages where token_count is not null")):
                try:
                    populated[f"{t}.{col}"] = con.execute(sql).fetchone()[0]
                except Exception:
                    populated[f"{t}.{col}"] = None
            res["POPULATED_ROWS"] = populated
        finally:
            con.close()

    def has(table, col):
        return col in cols.get(table, set())

    def add(name, avail, source, joinable, privacy, reliability, note=""):
        res["SIGNALS"].append({"signal": name, "available": avail, "source_class": source,
                               "joinable_to_turn": joinable, "privacy_risk": privacy,
                               "reliability": reliability, "note": note})

    add("context_size_tokens", "PARTIAL" if has("session_model_usage", "input_tokens") else "NO",
        "usage table (per session x model)", "PARTIAL", "LOW", "MEDIUM",
        "session/model totals, not a per-request context snapshot")
    add("input_tokens", "YES" if has("sessions", "input_tokens") else "NO", "sessions table",
        "PARTIAL", "LOW", "HIGH", "")
    add("output_tokens", "YES" if has("sessions", "output_tokens") else "NO", "sessions table",
        "PARTIAL", "LOW", "HIGH", "")
    add("cache_read_tokens", "YES" if has("session_model_usage", "cache_read_tokens") else "NO",
        "usage table (per session x model)", "PARTIAL", "LOW", "HIGH", "")
    add("cache_write_tokens", "YES" if has("session_model_usage", "cache_write_tokens") else "NO",
        "usage table (per session x model)", "PARTIAL", "LOW", "HIGH", "")
    add("prompt_cache_hit_miss", "PARTIAL", "derived from cache_read vs input tokens", "PARTIAL",
        "LOW", "MEDIUM", "ratio only; no explicit hit/miss flag exists")
    add("tool_call_count", "YES" if has("sessions", "tool_call_count") else "NO", "sessions table",
        "PARTIAL", "LOW", "HIGH", "")
    add("tool_error_count", "NO", "-", "NO", "LOW", "LOW",
        "tool results are message content; counting errors would require reading content")
    add("model_retry_count", "PARTIAL" if has("sessions", "api_call_count") else "NO",
        "api_call_count vs turn count", "PARTIAL", "LOW", "LOW", "")
    add("provider_retry_count", "NO", "-", "NO", "LOW", "LOW", "")
    add("turn_duration", "PARTIAL" if has("messages", "timestamp") else "NO", "message timestamps",
        "YES", "LOW", "MEDIUM", "")
    add("model_latency", "NO", "not recorded for the executing model", "NO", "LOW", "LOW",
        "the router records JEV service latency, not provider latency")
    add("final_status", "YES" if has("sessions", "end_reason") else "NO", "sessions table",
        "YES", "LOW", "HIGH", "")
    add("cancellation", "PARTIAL", "end_reason / message disposition", "PARTIAL", "LOW", "MEDIUM", "")
    add("exception", "PARTIAL", "handoff_error, compression_failure_error", "PARTIAL", "LOW", "MEDIUM", "")
    add("user_followup_timing", "YES" if has("messages", "timestamp") else "NO",
        "derived from message timestamps", "YES", "MEDIUM", "MEDIUM",
        "timing only, no content; a short gap is an ambiguous dissatisfaction proxy")
    add("per_message_token_count", "NO" if not has("messages", "token_count") else "PARTIAL",
        "messages table", "PARTIAL", "LOW", "LOW",
        "column exists but is not populated in this deployment")
    add("conversation_rewind", "PARTIAL" if has("sessions", "rewind_count") else "NO",
        "sessions table", "YES", "MEDIUM", "MEDIUM", "a rewind is a strong negative signal when present")

    res["REAL_CONTEXT_SIZE_SIGNAL_AVAILABLE"] = "PARTIAL"
    res["REAL_CONTEXT_SIZE_SIGNAL_NOTE"] = ("available as session/model token totals (input + cache read) and "
                                            "message timestamps, but there is no per-request context snapshot, "
                                            "so the router's dossier_token_estimate must not be read as the "
                                            "conversation context size")
    res["PROMPT_CACHE_SIGNAL_AVAILABLE"] = "PARTIAL"
    res["PROMPT_CACHE_SIGNAL_NOTE"] = ("cache_read_tokens / cache_write_tokens exist per session and per "
                                       "session x model with first_seen/last_seen windows, so a cache reuse "
                                       "ratio can be derived per window; there is no explicit per-turn hit/miss "
                                       "flag")
    res["OUTCOME_PROXY_CANDIDATES"] = [
        {"signal": "abnormal session end", "available_now": "YES", "semantic_meaning":
         "the session ended for a reason other than a clean finish",
         "false_positive_risk": "MEDIUM", "privacy_risk": "LOW", "use_for_quality_label": "EXPERIMENTAL"},
        {"signal": "handoff / compression failure recorded", "available_now": "YES",
         "semantic_meaning": "a runtime failure surfaced during the session",
         "false_positive_risk": "LOW", "privacy_risk": "LOW", "use_for_quality_label": "EXPERIMENTAL"},
        {"signal": "user follow-up gap after an assistant turn", "available_now": "YES",
         "semantic_meaning": "how quickly the user came back; a very short gap can mean the previous "
                             "answer did not land", "false_positive_risk": "HIGH", "privacy_risk": "MEDIUM",
         "use_for_quality_label": "EXPERIMENTAL"},
        {"signal": "tool call pressure per turn", "available_now": "PARTIAL",
         "semantic_meaning": "more tool calls for the same task can mean the model struggled",
         "false_positive_risk": "HIGH", "privacy_risk": "LOW", "use_for_quality_label": "EXPERIMENTAL"},
        {"signal": "provider retries per turn", "available_now": "PARTIAL",
         "semantic_meaning": "transport instability rather than answer quality",
         "false_positive_risk": "MEDIUM", "privacy_risk": "LOW", "use_for_quality_label": "NO"},
        {"signal": "cache reuse ratio drop", "available_now": "PARTIAL",
         "semantic_meaning": "switching models invalidates the prompt cache, so escalation has a cost that "
                             "this ratio can price", "false_positive_risk": "MEDIUM", "privacy_risk": "LOW",
         "use_for_quality_label": "NO"},
        {"signal": "conversation rewind", "available_now": "PARTIAL",
         "semantic_meaning": "the user undid the conversation", "false_positive_risk": "MEDIUM",
         "privacy_risk": "MEDIUM", "use_for_quality_label": "EXPERIMENTAL"},
        {"signal": "task-completion verdict", "available_now": "NO",
         "semantic_meaning": "whether the chosen model actually solved the task",
         "false_positive_risk": "LOW", "privacy_risk": "LOW", "use_for_quality_label": "NO"},
    ]
    return res


# --------------------------------------------------------------------------- cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JEV routing-quality aggregates (read only)")
    ap.add_argument("--generation", default=None,
                    help="deployment generation to analyse, or 'all' for record counts only")
    ap.add_argument("--log-dir", default=os.environ.get("JEV_LOG_DIR")
                    or str(pathlib.Path(os.environ.get("HERMES_HOME") or "/opt/data") / "logs" / "router"))
    ap.add_argument("--runtime-root", default=os.environ.get("JEV_ROUTER_ROOT")
                    or str(pathlib.Path(__file__).resolve().parents[1]))
    ap.add_argument("--confidence-threshold", type=float,
                    default=float(os.environ.get("JEV_MIN_CONFIDENCE") or 0.65))
    ap.add_argument("--margin-threshold", type=float,
                    default=float(os.environ.get("JEV_MIN_MARGIN") or 0.15))
    ap.add_argument("--available-routes", default=os.environ.get("JEV_AVAILABLE_ROUTES") or "")
    ap.add_argument("--sensitivity", action="store_true")
    ap.add_argument("--features", action="store_true")
    ap.add_argument("--longitudinal", action="store_true")
    ap.add_argument("--signals", action="store_true")
    ap.add_argument("--state-db", default=str(pathlib.Path(os.environ.get("HERMES_HOME") or "/opt/data") / "state.db"))
    ap.add_argument("--contamination-ledger", default=None)
    ap.add_argument("--json", default=None, help="write the aggregate JSON here (never inside the deployment)")
    args = ap.parse_args(argv)

    try:
        shadow_stats = _load_sibling("rq_shadow_stats", "shadow_stats.py")
    except Exception as exc:                       # pragma: no cover
        print(f"cannot load shadow_stats.py: {exc}", file=sys.stderr)
        return 2

    logdir = pathlib.Path(args.log_dir)
    recs = load_records(logdir)
    if not recs:
        print(f"no shadow telemetry found under {logdir.name}/", file=sys.stderr)
        return 2
    gens = collections.Counter(r.get("deployment_generation") or shadow_stats.GENERATION_LEGACY for r in recs)
    sources = shadow_stats.session_sources()

    out = {"RECORDS_TOTAL": len(recs), "GENERATION_DISTRIBUTION": dict(gens),
           "THRESHOLDS_IN_EFFECT": {"confidence": args.confidence_threshold,
                                    "margin": args.margin_threshold,
                                    "available_routes": [x for x in args.available_routes.replace(",", " ").split() if x]}}

    if args.generation in (None, "all"):
        out["GENERATION_SEPARATION_REQUIRED"] = True
        out["ROUTING_FIGURES"] = ("WITHHELD: routing-policy figures are only comparable inside one deployment "
                                 "generation; pass --generation <name>")
        if args.generation is None and len(gens) == 1:
            args.generation = next(iter(gens))
            out["ROUTING_FIGURES"] = f"single generation present; analysed {args.generation}"
        if args.generation == "all":
            print(json.dumps(out, indent=2))
            return 0

    if args.generation not in gens:
        print(f"generation {args.generation!r} not present; found {sorted(gens)}", file=sys.stderr)
        return 2

    rows = cohort(recs, args.generation, sources, shadow_stats)
    if not rows:
        print(f"no real human turns for generation {args.generation}", file=sys.stderr)
        return 2

    available = tuple(x for x in args.available_routes.replace(",", " ").split() if x) or None
    try:
        policy = Policy(pathlib.Path(args.runtime_root), args.confidence_threshold,
                        args.margin_threshold, available)
    except Exception as exc:
        print(f"cannot replay the router policy: {exc}", file=sys.stderr)
        return 3
    out["POLICY_SOURCE"] = {"class": "immutable runtime root" if "generations" in str(args.runtime_root)
                            else "repository checkout",
                            "shadow_module_digest_16": policy.source_digest}
    out["COHORT"] = analyse(rows, policy, args.confidence_threshold, args.margin_threshold,
                            args.generation, args.sensitivity, args.features, args.longitudinal)

    if args.contamination_ledger:
        try:
            led = json.loads(pathlib.Path(args.contamination_ledger).read_text())
            out["COUNTER_CONTAMINATION"] = {
                "status": "KNOWN_SYNTHETIC_COUNTER_CONTAMINATION",
                "event_type": led.get("event_type"),
                "affected_source": led.get("affected_source"),
                "shadow_telemetry_affected": led.get("shadow_telemetry_affected"),
                "confidence": led.get("confidence"),
                "window_classification": led.get("window_classification"),
                "RECONSTRUCTED_SYNTHETIC_COUNTER_DELTA_PER_RUN": led.get("reconstruction", {}).get(
                    "synthetic_delta_per_run"),
                "REAL_REJECTION_TOTAL": "NOT_DERIVABLE (no subtraction of a synthetic delta from raw totals)",
            }
        except Exception as exc:
            out["COUNTER_CONTAMINATION"] = {"status": "LEDGER_UNREADABLE", "error": str(exc)}
    if args.signals:
        out["OUTCOME_SIGNAL_INVENTORY"] = signal_inventory(pathlib.Path(args.state_db))

    payload = json.dumps(out, indent=2, sort_keys=True, default=str)
    if args.json:
        dest = pathlib.Path(args.json).resolve()
        forbidden = [pathlib.Path(args.log_dir).resolve(), pathlib.Path(args.runtime_root).resolve(),
                     pathlib.Path(os.environ.get("HERMES_HOME") or "/opt/data").resolve()]
        for f in forbidden:
            if f == dest or f in dest.parents:
                print(f"refusing to write inside the deployment: {dest}", file=sys.stderr)
                return 4
        dest.write_text(payload + "\n")
        print(f"aggregate written to {dest}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
