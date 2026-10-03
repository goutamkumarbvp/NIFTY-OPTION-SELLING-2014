import pytest
from harness import live_app

from amrt.core.enums import Side


@pytest.mark.unit
async def test_live_rig_fill(monkeypatch, tmp_path):
    rig = live_app(monkeypatch, tmp_path)
    await rig.ready()
    res = await rig.app.pipeline.submit(rig.intent(side=Side.BUY))
    assert res["decision"]["approved"], res["decision"]
    assert res["order"]["state"] == "FILLED"
