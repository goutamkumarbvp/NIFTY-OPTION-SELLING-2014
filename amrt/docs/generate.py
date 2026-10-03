"""Regenerate the code-derived reference docs: python docs/generate.py (run from amrt/)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from amrt.brokers.registry import PROFILES  # noqa: E402
from amrt.security.identity import EXCLUSIVE, PERMISSION_MATRIX, Capability  # noqa: E402

D = Path(__file__).resolve().parent

kinds = list(PERMISSION_MATRIX)
lines = ["# Permission matrix (generated from `amrt/security/identity.py`)", "",
         "Every component runs as its own principal. `require(principal, capability)` is called before every privileged action; denials are written to the audit log.", "",
         "| Capability | " + " | ".join(k.value for k in kinds) + " |", "|---|" + "---|" * len(kinds)]
for cap in Capability:
    lines.append(f"| {cap.value} | " + " | ".join("✔" if cap in PERMISSION_MATRIX[k] else "" for k in kinds) + " |")
lines += ["", "## Exclusive capabilities (validated at startup by `validate_matrix()`)", ""]
lines += [f"* **{c.value}** — only {', '.join(k.value for k in holders)}" for c, holders in EXCLUSIVE.items()]
(D / "PERMISSION_MATRIX.md").write_text("\n".join(lines) + "\n")

out = ["# Broker capability matrix (generated from `amrt/brokers/*.py`)", "",
       "Status meanings: **VERIFIED** = exercised against the live broker API with recorded evidence; **PARTIAL** = implemented against the official SDK and "
       "contract-tested (every SDK call binds to the installed SDK's real signature) but not exercised live from this build environment; "
       "**UNAVAILABLE** = the broker does not offer it officially; **BLOCKED** = not used by policy.", "",
       "Live verification status: **BLOCKED in the build environment** — broker APIs are not reachable from it, so no feature is marked VERIFIED.", ""]
for p in PROFILES.values():
    out += [f"## {p.display} — overall {p.overall_status.value}", "", f"* SDK: `{p.sdk_package}` (tested {p.sdk_version_tested}); API {p.api_version}",
            f"* Official docs: {p.official_docs}", f"* Auth: {p.auth}", f"* Order tag field: {p.tag_field} (max {p.tag_max_len})", "",
            "| Feature | Status | Notes |", "|---|---|---|"]
    out += [f"| {c.feature} | {c.status.value} | {c.notes} |" for c in p.capabilities]
    out.append("")
(D / "BROKER_CAPABILITY_MATRIX.md").write_text("\n".join(out) + "\n")

src = (D.parent / "amrt" / "risk" / "kernel.py").read_text()
rules = sorted(set(re.findall(r'add\("(RK-\d+b?)", "([A-Z_]+)"', src)))
(D / "RISK_KERNEL_RULES.md").write_text("# Risk Kernel rules (generated from `amrt/risk/kernel.py`)\n\n| Rule | Name |\n|---|---|\n"
                                        + "\n".join(f"| {r} | {n} |" for r, n in rules) + "\n")
print(f"wrote PERMISSION_MATRIX.md ({len(list(Capability))} capabilities), BROKER_CAPABILITY_MATRIX.md ({len(PROFILES)} brokers), RISK_KERNEL_RULES.md ({len(rules)} rules)")
