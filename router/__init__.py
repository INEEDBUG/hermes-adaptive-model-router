"""Public layer of the JEV Shadow Router: a privacy-preserving adaptive routing
layer for Hermes Agent.

Modules
-------
config   environment / `.env` resolution and routing thresholds
redact   deterministic, offline redaction of credential-like content
dossier  minimal per-turn "Routing Dossier" construction
client   TypeSafe JEV client (strictly following the official OpenAPI schema)
shadow   non-blocking shadow evaluation, telemetry and privacy fallback
state    runtime mode resolver (kill switch / fail-safe = off)

Nothing in this package changes which model actually executes a turn.
"""

__version__ = "0.1.0"
