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

# Version of record. The deployment-relevant manifest (``plugin/plugin.yaml``) carries the same
# number: two independently bumped version strings for one project is the drift this constant used
# to cause (the manifest said 0.3.0 while this module still said 0.1.0). No git tag exists, because
# no release policy has been established; ``tools/check_outcome_hook_contract.py`` fails the gate
# when the two numbers diverge again.
__version__ = "0.3.0"
