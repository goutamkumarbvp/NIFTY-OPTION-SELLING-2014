"""Security (AT-05, AT-16): credential isolation, redaction, no live venues in PAPER_ONLY, static checks."""
import re
from pathlib import Path

import pytest
from harness import env, paper_app

from amrt.brokers.registry import BrokerManager
from amrt.config import Settings
from amrt.core.errors import PaperIsolationViolation, PermissionDenied
from amrt.security.identity import Principal, PrincipalKind

pytestmark = [pytest.mark.security, pytest.mark.acceptance]
ROOT = Path(__file__).resolve().parents[2] / "amrt"


def test_only_the_gateway_can_hold_broker_credentials(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, NEO_CONSUMER_KEY="ck", NEO_MOBILE_NUMBER="+919999999999", NEO_UCC="U1", NEO_MPIN="1234", NEO_TOTP_SECRET="JBSWY3DPEHPK3PXP")
    s = Settings()
    for k in (PrincipalKind.MASTER_AGENT, PrincipalKind.SPECIALIST_AGENT, PrincipalKind.RELIABILITY_PLANE, PrincipalKind.OWNER, PrincipalKind.MODE_CONTROLLER):
        with pytest.raises(PermissionDenied):
            BrokerManager(Principal.of(k, "x"), s, None)
    bm = BrokerManager(Principal.of(PrincipalKind.EXECUTION_GATEWAY, "gw"), s, None)
    assert "kotak" in bm.sessions
    with pytest.raises(PaperIsolationViolation):
        bm.live_venue("kotak", None)                                   # PAPER_ONLY deployment never builds a live order client
    reader = bm.reader("kotak")
    assert not any(hasattr(reader, a) for a in ("_place", "_cancel", "place_order", "settings", "_client"))


def test_paper_app_has_no_live_route_even_with_credentials(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path, NEO_CONSUMER_KEY="ck", NEO_MOBILE_NUMBER="+919999999999", NEO_UCC="U1", NEO_MPIN="1234",
                    NEO_TOTP_SECRET="JBSWY3DPEHPK3PXP")
    assert app.accounts.live() == [] and {v.kind for v in app.gateway.venues.values()} == {"PAPER"}
    pub = str(app.settings.public_view())
    assert "JBSWY3DPEHPK3PXP" not in pub and "1234" not in pub.replace("12345", "")
    with pytest.raises(PaperIsolationViolation):
        app.gateway.register_venue("X", type("V", (), {"kind": "LIVE"})())


def test_paper_venue_has_no_broker_imports():
    import ast
    tree = ast.parse((ROOT / "execution" / "paper.py").read_text())
    mods = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in mods if m.startswith(("amrt.brokers", "neo_api_client", "kiteconnect", "SmartApi", "upstox_client", "growwapi", "httpx"))]


def test_static_hygiene():
    bad = []
    for f in ROOT.rglob("*.py"):
        t = f.read_text()
        if re.search(r"\beval\(|\bexec\(|pickle\.loads|shell=True|verify=False", t):
            bad.append(f.name)
        if re.search(r"(api_key|secret|password|mpin)\s*=\s*['\"][A-Za-z0-9]{12,}['\"]", t, re.I):
            bad.append(f"{f.name}: hardcoded secret?")
    assert bad == []


def test_env_file_never_committed():
    repo = ROOT.parent.parent
    gi = (repo / ".gitignore").read_text() if (repo / ".gitignore").exists() else ""
    assert ".env" in gi
