
import pytest
from httpx import ASGITransport, AsyncClient

from terminal.api.app import create_app
from tests.conftest import make_settings, make_terminal, wait_live


@pytest.fixture
def app_and_terminal():
    t = make_terminal(make_settings(), seed=11)
    return create_app(t), t


async def test_api_end_to_end(app_and_terminal):
    app, t = app_and_terminal
    async with app.router.lifespan_context(app):
        await wait_live(t)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/health")
            assert r.status_code == 200 and r.json()["data"]["engine_running"]
            r = await c.get("/api/state")
            assert r.json()["data"]["mode"] == "MANUAL"
            r = await c.get("/api/market/chain?underlying=NIFTY")
            chain = r.json()["data"]["chain"]
            assert len(chain["rows"]) >= 51
            r = await c.post("/api/strategy/preview", json={"strategy": "iron_condor", "underlying": "NIFTY", "lots": 1})
            assert r.status_code == 200 and r.json()["data"]["payoff"]["max_loss"] < 0
            r = await c.post("/api/strategy/deploy", json={"strategy": "iron_condor", "underlying": "NIFTY", "lots": 1})
            assert r.status_code == 200
            run_id = r.json()["data"]["id"]
            r = await c.get("/api/positions")
            assert len(r.json()["data"]["positions"]) == 4
            r = await c.post(f"/api/strategy/exit/{run_id}")
            assert r.json()["data"]["status"] == "CLOSED"
            r = await c.get("/api/reports/performance")
            assert r.json()["data"]["trades"] == 4
            r = await c.post("/api/mode", json={"mode": "AUTO"})
            assert r.json()["data"]["mode"] == "AUTO"
            r = await c.post("/api/safety/kill")
            assert r.json()["data"]["snapshot"]["kill_switch"] is True
            r = await c.post("/api/mode", json={"mode": "AUTO"})
            assert r.status_code == 409
            r = await c.get("/api/system/audit")
            assert r.json()["data"]["count"] > 5
            r = await c.post("/api/reports/backtest", json={"strategy": "short_strangle", "underlying": "NIFTY", "days": 20})
            assert r.json()["data"]["days"] > 0
            r = await c.get("/api/reports/export.csv")
            assert r.headers["content-type"].startswith("text/csv")


async def test_auth_roles_enforced():
    t = make_terminal(make_settings(BIND_HOST="0.0.0.0", TERMINAL_USERS="admin:secret:admin,view:pw:viewer,trd:pw:trader"), seed=3)
    app = create_app(t)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/api/state")).status_code == 401
            assert (await c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})).status_code == 401
            viewer = (await c.post("/api/auth/login", json={"username": "view", "password": "pw"})).json()["data"]["token"]
            h = {"Authorization": f"Bearer {viewer}"}
            assert (await c.get("/api/state", headers=h)).status_code == 200
            assert (await c.post("/api/safety/kill", headers=h)).status_code == 403
            assert (await c.post("/api/mode", json={"mode": "AUTO"}, headers=h)).status_code == 403
            trader = (await c.post("/api/auth/login", json={"username": "trd", "password": "pw"})).json()["data"]["token"]
            assert (await c.post("/api/mode", json={"mode": "AUTO"}, headers={"Authorization": f"Bearer {trader}"})).status_code == 403
            admin = (await c.post("/api/auth/login", json={"username": "admin", "password": "secret"})).json()["data"]["token"]
            assert (await c.post("/api/safety/gate", json={"open": False}, headers={"Authorization": f"Bearer {admin}"})).status_code == 200
