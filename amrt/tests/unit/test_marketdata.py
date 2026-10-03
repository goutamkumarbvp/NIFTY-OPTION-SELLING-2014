"""Market data hub: validation, sequence handling, freshness, provenance labels, DATA UNAVAILABLE."""
import pytest
from harness import clock_at

from amrt.core.enums import DataLabel
from amrt.core.errors import DataUnavailable
from amrt.marketdata.hub import MarketDataHub
from amrt.marketdata.instruments import InstrumentMaster, spec
from amrt.marketdata.models import Quote

pytestmark = pytest.mark.unit
K = "NSE:NIFTY:2026-10-06:25000:CE"


def q(clock, **kw):
    base = dict(instrument_key=K, ltp=100.0, bid=99.5, ask=100.5, recv_ts=clock.ts(), source="kotak", label=DataLabel.LIVE_UNVERIFIED)
    base.update(kw)
    return Quote(**base)


def test_rejects_bad_quotes_and_sequence_faults():
    c = clock_at()
    h = MarketDataHub(c, max_drift_ms=2000)
    src = h.register_source("kotak", live=True)
    assert not h.ingest(q(c, bid=101, ask=100))                                    # crossed book
    assert not h.ingest(q(c, exchange_ts=c.ts() + 10))                              # from the future
    assert h.ingest(q(c, seq=1)) and not h.ingest(q(c, seq=1)) and not h.ingest(q(c, seq=0))
    assert h.ingest(q(c, seq=5)) and src.gaps == 1
    assert {"CROSSED_BOOK", "TIMESTAMP_IN_FUTURE", "DUPLICATE", "OUT_OF_ORDER"} <= set(h.rejections)
    with pytest.raises(ValueError):
        q(c, ltp=0)


def test_labels_and_staleness():
    c = clock_at()
    h = MarketDataHub(c)
    src = h.register_source("kotak", live=True)
    h.ingest(q(c, exchange_ts=c.ts() - 0.1))
    assert h.freshness(K, 3000).label == DataLabel.LIVE_UNVERIFIED            # session not authenticated
    src.authenticated = src.connected = True
    assert h.freshness(K, 3000).label == DataLabel.LIVE_VERIFIED
    c.advance(5)
    f = h.freshness(K, 3000)
    assert not f.fresh and f.label == DataLabel.UNAVAILABLE
    with pytest.raises(DataUnavailable):
        h.quote(K, 3000)
    with pytest.raises(DataUnavailable):
        h.quote("NSE:NIFTY:2026-10-06:99999:CE", 3000)


def test_replay_and_simulated_keep_their_labels():
    c = clock_at()
    h = MarketDataHub(c)
    h.register_source("replay", live=False)
    h.ingest(q(c, source="replay", label=DataLabel.HISTORICAL_REPLAY))
    assert h.freshness(K, 3000).label == DataLabel.HISTORICAL_REPLAY
    h.ingest(q(c, instrument_key="NSE:NIFTY:2026-10-06:25100:CE", source="simulator", label=DataLabel.SIMULATED))
    assert h.freshness("NSE:NIFTY:2026-10-06:25100:CE", 3000).label == DataLabel.SIMULATED


def test_instrument_master_lot_sizes_and_unknowns():
    assert spec("NIFTY").lot_size_on(__import__("datetime").date(2025, 1, 1)) == 75
    m = InstrumentMaster()
    inst = m.option("NIFTY", __import__("datetime").date(2026, 10, 6), 25000, "CE")
    assert inst.key == K and inst.lot_size == 65
    with pytest.raises(KeyError):
        spec("NOTANINDEX")
    with pytest.raises(KeyError):
        m.get("NSE:NIFTY:2026-10-06:1:CE")
