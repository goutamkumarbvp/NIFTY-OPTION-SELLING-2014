"""API end-to-end (AT-15, AT-16): auth, CSRF, step-up, roles, kill switch, DATA UNAVAILABLE, headers, rate limits."""
import pytest
from fastapi.testclient import TestClient
from harness import paper_app

from amrt.api.server import create_api

pytestmark = [pytest.mark.e2e, pytest.mark.acceptance]
PW = "correct-horse-battery"


@pytest.fixture
def client(monkeypatch, tmp_path):
    app = paper_app(monkeypatch, tmp_path, AMRT_SIMULATED_MARKET="false")      # no market data source at all
    app.auth.create_user("owner", PW, "OWNER")
    app.auth.create_user("viewer", "viewer-password-1", "READ_ONLY")
    with TestClient(create_api(app, start_loops=False)) as c:
        c.app_ref = app
        yield c


def login(c, user="owner", pw=PW):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"X-AMRT-CSRF": r.json()["csrf"]}


def test_auth_required_and_security_headers(client):
    r = client.get("/api/status")
    assert r.status_code == 401
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/health/live").json() == {"ok": True}


def test_disconnected_market_shows_data_unavailable(client):
    login(client)
    c = client.get("/api/market/chain/NIFTY").json()
    assert c["label"] == "DATA UNAVAILABLE" and "rows" not in c and c["feed_error"]
    assert client.get("/api/market/pcr/NIFTY").json() == {"label": "DATA UNAVAILABLE", "series": []}
    f = client.get("/api/market/fii-dii").json()
    assert f["label"] == "DATA UNAVAILABLE" and f["cash"] == {}
    p = client.get("/api/portfolio").json()[0]
    assert p["valuation"]["positions"] == []


def test_csrf_step_up_and_kill_switch(client):
    h = login(client)
    assert client.post("/api/safety/kill", json={"reason": "x"}).status_code == 403
    r = client.post("/api/safety/kill", json={"reason": "drill"}, headers=h)
    assert r.status_code == 200 and client.app_ref.safety.kill_file_engaged()
    r = client.post("/api/safety/kill/release", headers=h)
    assert r.status_code == 409 and r.json()["error"] == "NOT READY"              # verification fails: loops are not running
    client.app_ref.clock.advance(400)
    assert client.post("/api/safety/kill/release", headers=h).status_code == 428  # step-up expired
    assert client.post("/api/auth/step-up", json={"password": "wrong-password"}, headers=h).status_code == 401
    assert client.post("/api/auth/step-up", json={"password": PW}, headers=h).status_code == 200
    assert client.app_ref.safety.kill_file_engaged()


def test_read_only_role_cannot_act_and_denials_are_audited(client):
    h = login(client, "viewer", "viewer-password-1")
    for path, body in (("/api/safety/kill", {"reason": "x"}), ("/api/safety/quick-exit", {"account_id": "PAPER-1", "confirmation": "EXIT"}),
                       ("/api/backups", {}), ("/api/market/fii-dii/import", {"kind": "fii_dii_cash", "content": "x", "source": "s"})):
        assert client.post(path, json=body, headers=h).status_code == 403, path
    assert client.get("/api/status").status_code == 200
    denials = client.get("/api/audit?type_prefix=PERMISSION_DENIED").json()
    assert {d["payload"]["capability"] for d in denials} >= {"ENGAGE_KILL_SWITCH", "MANUAL_QUICK_EXIT", "RUN_BACKUP", "IMPORT_DATA"}


def test_policy_api_validates_against_hard_limits(client):
    h = login(client)
    ok = client.post("/api/policies", json={"kind": "risk", "body": {"account_id": "PAPER-1"}}, headers=h)
    assert ok.status_code == 200 and ok.json()["status"] == "DRAFT"
    bad = client.post("/api/policies", json={"kind": "risk", "body": {"account_id": "PAPER-1", "loss_level1_inr": 60000, "loss_level2_inr": 90000}}, headers=h)
    assert bad.status_code == 422 and bad.json()["error"] == "HARD_LIMIT_VIOLATION"
    act = client.post(f"/api/policies/{ok.json()['policy_id']}/activate", headers=h)
    assert act.status_code == 200
    kinds = {p["kind"] for p in client.get("/api/policies").json()["policies"]}
    assert kinds == {"RISK"}


def test_login_lockout_and_rate_limit(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "owner", "password": "bad-password-x"})
    r = client.post("/api/auth/login", json={"username": "owner", "password": PW})
    assert r.status_code in (401, 403)                                             # locked or rate-limited — never let through
    assert client.app_ref.auth.recent_failures() >= 5


def test_quick_exit_requires_typed_confirmation(client):
    h = login(client)
    r = client.post("/api/safety/quick-exit", json={"account_id": "PAPER-1", "confirmation": "no"}, headers=h)
    assert r.status_code == 403
    r = client.post("/api/safety/quick-exit", json={"account_id": "PAPER-1", "confirmation": "EXIT"}, headers=h)
    assert r.status_code == 200
