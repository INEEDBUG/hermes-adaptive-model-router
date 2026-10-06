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
# to cause (the manifest said 0.3.0 while this module still said 0.1.0). The repository does carry a
# tag-per-release convention (v0.1.0 .. v0.2.0). 0.3.0 is the generation deployed to production and
# validated by a natural human canary, and it stays untagged because no release is published for
# it; ``tools/check_outcome_hook_contract.py`` fails the gate when the manifest and this constant
# diverge again.
__version__ = "0.3.0"
