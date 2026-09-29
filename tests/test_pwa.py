"""PWA: manifest / service worker served at the root scope, icons, push config + subscriptions, alert -> push delivery."""
import json

from httpx import ASGITransport, AsyncClient

from terminal.api.app import create_app


async def test_manifest_service_worker_and_icons(terminal):
    app = create_app(terminal)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/manifest.webmanifest")
        assert r.status_code == 200 and "manifest+json" in r.headers["content-type"]
        m = r.json()
        assert m["display"] == "standalone" and m["start_url"].startswith("/") and any(i["purpose"] == "maskable" for i in m["icons"])
        for icon in m["icons"]:
            ri = await c.get(icon["src"])
            assert ri.status_code == 200 and ri.content[:8] == b"\x89PNG\r\n\x1a\n", icon["src"]
        r = await c.get("/sw.js")
        assert r.status_code == 200 and r.headers["service-worker-allowed"] == "/" and "addEventListener('push'" in r.text
        r = await c.get("/")
        assert 'rel="manifest"' in r.text and 'id="mnav"' in r.text and 'id="install-banner"' in r.text


async def test_push_config_subscribe_and_delivery_hook(terminal, monkeypatch):
    t = terminal
    app = create_app(t)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/push/config")
        d = r.json()["data"]
        assert d["enabled"] is False and d["keys_configured"] is False and d["subscriptions"] == 0
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k1", "auth": "a1"}, "ua": "test"}
        r = await c.post("/api/push/subscribe", json=sub)
        assert r.json()["data"]["subscriptions"] == 1
        r = await c.post("/api/push/subscribe", json=sub)  # same endpoint replaces, never duplicates
        assert r.json()["data"]["subscriptions"] == 1
        assert t.push.subscriptions()[0]["user"] == "operator" and any(a["event"] == "PUSH_SUBSCRIBED" for a in t.audit.tail(10))
        # without keys nothing is sent (and nothing crashes)
        r = await c.post("/api/push/test")
        assert r.json()["data"] == {"sent": 0, "failed": 0, "skipped": 1}
        # with keys: alerts of WARNING/CRITICAL level are pushed to every subscription; a 410 drops the subscription
        t.push.public_key, t.push.private_key, t.push._lib = "pub", "priv", True
        sent = []

        class Resp:
            status_code = 410

        class Gone(Exception):
            response = Resp()

        def fake_send(sub, payload):
            sent.append((sub["endpoint"], json.loads(payload)))
            if sub["endpoint"].endswith("dead"):
                raise Gone("gone")
        monkeypatch.setattr(t.push, "_send_one", fake_send)
        t.push.subscribe({"endpoint": "https://push.example/dead", "keys": {}}, "x")
        await t.alerts.emit("CRITICAL", "guardian", "Guardian: FEED_DOWN — permission needed", "body", dedupe_seconds=0)
        import asyncio
        await asyncio.sleep(0.3)
        assert {e for e, _ in sent} == {"https://push.example/abc", "https://push.example/dead"}
        payload = next(p for e, p in sent if e.endswith("abc"))
        assert payload["level"] == "CRITICAL" and payload["view"] == "approvals" and payload["title"].startswith("Guardian")
        assert [s["endpoint"] for s in t.push.subscriptions()] == ["https://push.example/abc"], "410 subscriptions are pruned"
        assert t.push.sent == 1 and t.push.failed == 1
        r = await c.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/abc"})
        assert r.json()["data"]["subscriptions"] == 0
        assert "push" in t.snapshot()


def test_vapid_generator_produces_keys():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "scripts/generate_vapid.py"], capture_output=True, text=True, check=True).stdout
    assert "VAPID_PUBLIC_KEY=" in out and "VAPID_PRIVATE_KEY=" in out and len(out.split("VAPID_PUBLIC_KEY=")[1].split()[0]) > 80
