# Daily verification ledger

This file records the repository's scheduled offline verification runs.

The daily workflow runs the same privacy-preserving, credential-free checks used by CI:
router tests, kill-switch/state tests, human-turn provenance boundary tests, secret-scanner
controls, installer tests, state-initialiser dry run, and the tree+history secret scan.

A row is written once per Beijing calendar day. A green GitHub contribution therefore
represents an actual repository verification run rather than an empty heartbeat commit.

| Beijing date | Result | Tested commit | Checks |
|---|---|---|---|
