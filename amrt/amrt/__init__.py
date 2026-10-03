"""AI Market Risk Terminal (AMRT).

A human-governed, multi-broker options risk platform. Four independently
permissioned components carry authority:

* Master AI Agent        — coordinates specialist advice, proposes only.
* Deterministic Risk Kernel — evaluates every proposed action, signs decisions.
* Execution Gateway      — the only component that can submit live orders.
* Reliability Control Plane — monitors and repairs infrastructure inside an allowlist.
"""
__version__ = "1.0.0"
PRODUCT = "AI Market Risk Terminal"
