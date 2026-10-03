# AI Market Risk Terminal (AMRT) 1.0.0

Human-governed, multi-broker options risk platform for NIFTY, SENSEX and MCX options:
advisory AI agents → owner or pre-approved automation policy → deterministic Risk Kernel → isolated Execution Gateway,
watched by an independent Reliability Control Plane. It is not an autonomous trader and promises no profit and no
maximum loss.

**Status: NOT READY for live trading** (live broker verification blocked in the build environment) — see
[docs/READINESS_REPORT.md](docs/READINESS_REPORT.md). Live order flow is disabled by default.

## Quick start (paper)

```bash
cd amrt
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # Windows: copy .env.example .env
python -m amrt                                       # add AMRT_SIMULATED_MARKET=true in .env for a demo
```

Open http://127.0.0.1:8700 and enter the one-time setup code printed in the console.

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, diagrams, module and agent matrix, modes, order lifecycle, kill switch, recovery, reconciliation, versioning |
| [RISK_AND_ANALYTICS.md](docs/RISK_AND_ANALYTICS.md) | Risk Kernel, thresholds and denominators, Path A/B, PCR/OI/FII-DII/backtest methodology, Master aggregation |
| [REQUIREMENTS_AND_THREAT_MODEL.md](docs/REQUIREMENTS_AND_THREAT_MODEL.md) | requirements, safety contracts, STRIDE, data-source inventory, API verification status |
| [OPERATIONS.md](docs/OPERATIONS.md) | setup, Docker, tests, backup/restore/rollback, live activation review |
| [READINESS_REPORT.md](docs/READINESS_REPORT.md) | evidence, phase reports, AT-01 … AT-16, defects fixed, blockers |
| [PERMISSION_MATRIX.md](docs/PERMISSION_MATRIX.md) · [BROKER_CAPABILITY_MATRIX.md](docs/BROKER_CAPABILITY_MATRIX.md) · [RISK_KERNEL_RULES.md](docs/RISK_KERNEL_RULES.md) | generated from code (`python docs/generate.py`) |
