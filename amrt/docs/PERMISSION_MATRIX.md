# Permission matrix (generated from `amrt/security/identity.py`)

Every component runs as its own principal. `require(principal, capability)` is called before every privileged action; denials are written to the audit log.

| Capability | OWNER | OPERATOR | READ_ONLY | SPECIALIST_AGENT | STRATEGY_AGENT | MASTER_AGENT | RISK_KERNEL | PORTFOLIO_RISK_MONITOR | SAFETY_MONITOR | MODE_CONTROLLER | PROTECTIVE_WORKFLOW | EXECUTION_GATEWAY | RECONCILER | RELIABILITY_PLANE | WATCHDOG | MONITORING | DEPLOYMENT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| READ_MARKET_DATA | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |  |
| READ_PORTFOLIO | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |  |
| READ_HEALTH | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |  |
| READ_AUDIT | ✔ | ✔ | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| PUBLISH_ADVICE |  |  |  | ✔ | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |
| PROPOSE_DECISION |  |  |  |  |  | ✔ |  |  |  |  |  |  |  |  |  |  |  |
| PROPOSE_STRATEGY_ACTION |  |  |  |  | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |
| APPROVE_ACTION | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| CREATE_MANUAL_INTENT | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| CHANGE_MODE | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| CHANGE_RISK_POLICY | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| APPROVE_AUTOMATION_POLICY | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| RELEASE_FREEZE | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| RELEASE_KILL_SWITCH | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| UNQUARANTINE | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| MANAGE_USERS | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| MANUAL_QUICK_EXIT | ✔ | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| ACK_INCIDENT | ✔ | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| RUN_BACKUP | ✔ | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| IMPORT_DATA | ✔ | ✔ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| ENGAGE_KILL_SWITCH | ✔ | ✔ |  |  |  |  | ✔ | ✔ | ✔ |  |  |  |  |  | ✔ |  |  |
| FREEZE_NEW_RISK | ✔ | ✔ |  |  |  |  | ✔ | ✔ | ✔ |  |  |  | ✔ | ✔ | ✔ |  |  |
| REQUEST_PROTECTIVE_ACTION |  |  |  |  |  |  |  | ✔ |  |  |  |  |  |  |  |  |  |
| EVALUATE_RISK |  |  |  |  |  |  | ✔ |  |  |  |  |  |  |  |  |  |  |
| SIGN_RISK_DECISION |  |  |  |  |  |  | ✔ |  |  |  |  |  |  |  |  |  |  |
| REQUEST_EXECUTION |  |  |  |  |  |  |  |  |  | ✔ | ✔ |  |  |  |  |  |  |
| SUBMIT_LIVE_ORDER |  |  |  |  |  |  |  |  |  |  |  | ✔ |  |  |  |  |  |
| CANCEL_LIVE_ORDER |  |  |  |  |  |  |  |  |  |  |  | ✔ |  |  |  |  |  |
| SUBMIT_PAPER_ORDER |  |  |  |  |  |  |  |  |  |  |  | ✔ |  |  |  |  |  |
| RECONCILE |  |  |  |  |  |  |  |  |  |  |  |  | ✔ |  |  |  |  |
| HOLD_BROKER_CREDENTIALS |  |  |  |  |  |  |  |  |  |  |  | ✔ |  |  |  |  |  |
| RECOVERY_ALLOWLIST |  |  |  |  |  |  |  |  |  |  |  |  |  | ✔ |  |  |  |
| QUARANTINE_COMPONENT |  |  |  |  |  |  |  |  |  |  |  |  |  | ✔ |  |  |  |
| CREATE_INCIDENT |  |  |  |  |  |  |  | ✔ | ✔ |  |  |  | ✔ | ✔ | ✔ |  |  |

## Exclusive capabilities (validated at startup by `validate_matrix()`)

* **SUBMIT_LIVE_ORDER** — only EXECUTION_GATEWAY
* **CANCEL_LIVE_ORDER** — only EXECUTION_GATEWAY
* **HOLD_BROKER_CREDENTIALS** — only EXECUTION_GATEWAY
* **SIGN_RISK_DECISION** — only RISK_KERNEL
* **CHANGE_MODE** — only OWNER
* **CHANGE_RISK_POLICY** — only OWNER
* **APPROVE_AUTOMATION_POLICY** — only OWNER
* **RELEASE_FREEZE** — only OWNER
* **RELEASE_KILL_SWITCH** — only OWNER
* **UNQUARANTINE** — only OWNER
* **MANAGE_USERS** — only OWNER
