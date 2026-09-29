"""Zerodha Kite adapter tests: pure helpers only (no SDK, no network)."""
import datetime as dt

from terminal.market.zerodha import (choose_index, choose_nearest_future, expiries_for, option_instruments, parse_instrument, parse_quote, tick_from_kite)

INSTRUMENTS = [
    {"instrument_token": 256265, "tradingsymbol": "NIFTY 50", "name": "NIFTY 50", "exchange": "NSE", "segment": "INDICES", "instrument_type": "EQ", "expiry": "", "strike": 0, "lot_size": 0},
    {"instrument_token": 264969, "tradingsymbol": "INDIA VIX", "name": "INDIA VIX", "exchange": "NSE", "segment": "INDICES", "instrument_type": "EQ", "expiry": "", "strike": 0, "lot_size": 0},
    {"instrument_token": 1001, "tradingsymbol": "NIFTY26O0724800CE", "name": "NIFTY", "exchange": "NFO", "segment": "NFO-OPT", "instrument_type": "CE", "expiry": dt.date(2026, 10, 7), "strike": 24800.0, "lot_size": 75},
    {"instrument_token": 1002, "tradingsymbol": "NIFTY26O0724800PE", "name": "NIFTY", "exchange": "NFO", "segment": "NFO-OPT", "instrument_type": "PE", "expiry": "2026-10-07", "strike": 24800.0, "lot_size": 75},
    {"instrument_token": 1003, "tradingsymbol": "NIFTY26OCT24800CE", "name": "NIFTY", "exchange": "NFO", "segment": "NFO-OPT", "instrument_type": "CE", "expiry": "2026-10-27", "strike": 24800.0, "lot_size": 75},
    {"instrument_token": 2001, "tradingsymbol": "CRUDEOIL26OCTFUT", "name": "CRUDEOIL", "exchange": "MCX", "segment": "MCX-FUT", "instrument_type": "FUT", "expiry": "2026-10-19", "strike": 0, "lot_size": 100},
    {"instrument_token": 2002, "tradingsymbol": "CRUDEOIL26NOVFUT", "name": "CRUDEOIL", "exchange": "MCX", "segment": "MCX-FUT", "instrument_type": "FUT", "expiry": "2026-11-18", "strike": 0, "lot_size": 100},
]


def test_index_and_future_resolution():
    assert choose_index(INSTRUMENTS, ["NIFTY 50"])["token"] == 256265
    assert choose_index(INSTRUMENTS, ["INDIA VIX"])["exchange"] == "NSE"
    assert choose_index(INSTRUMENTS, ["SENSEX"]) is None
    assert choose_nearest_future(INSTRUMENTS, "CRUDEOIL", dt.date(2026, 9, 29))["token"] == 2001
    assert choose_nearest_future(INSTRUMENTS, "CRUDEOIL", dt.date(2026, 10, 20))["token"] == 2002


def test_option_lookup_and_expiries():
    table = option_instruments(INSTRUMENTS, "NIFTY", dt.date(2026, 10, 7))
    assert set(table) == {(24800.0, "CE"), (24800.0, "PE")}
    assert table[(24800.0, "CE")]["tradingsymbol"] == "NIFTY26O0724800CE" and table[(24800.0, "PE")]["lot_size"] == 75
    assert expiries_for(INSTRUMENTS, "NIFTY", dt.date(2026, 9, 29)) == [dt.date(2026, 10, 7), dt.date(2026, 10, 27)]
    assert parse_instrument(INSTRUMENTS[2])["expiry"] == dt.date(2026, 10, 7)


def test_quote_and_tick_parsing():
    q = parse_quote({"instrument_token": 1001, "last_price": 131.4, "oi": 1500000, "volume": 240000, "depth": {"buy": [{"price": 131.2}], "sell": [{"price": 131.6}]}, "ohlc": {"open": 120, "high": 140, "low": 118, "close": 125}})
    assert q["ltp"] == 131.4 and q["oi"] == 1500000 and q["bid"] == 131.2 and q["ask"] == 131.6 and q["prev_close"] == 125
    t = tick_from_kite({"instrument_token": 256265, "last_price": 24812.5, "ohlc": {"open": 24750, "high": 24850, "low": 24690, "close": 24700}, "change": 0.455})
    assert t["token"] == 256265 and t["prev_close"] == 24700 and abs(t["change_pct"] - 0.455) < 1e-9
    t2 = tick_from_kite({"instrument_token": 2001, "last_price": 5640.0, "ohlc": {"close": 5600.0}, "volume_traded": 321, "oi": 55})
    assert t2["volume"] == 321 and t2["oi"] == 55 and abs(t2["change_pct"] - (5640 / 5600 - 1) * 100) < 1e-9
    assert tick_from_kite({"instrument_token": 1, "last_price": 0}) is None


def test_session_credential_gates(tmp_path):
    from terminal.market.zerodha import ZerodhaSession
    from tests.conftest import make_settings
    s = make_settings(DATA_SOURCE="zerodha", ZERODHA_API_KEY="k")
    sess = ZerodhaSession(s, tmp_path)
    assert any("ACCESS_TOKEN" in m for m in sess.missing_credentials())
    sess._save_token("abc", "AB1234")
    assert sess._saved_token() == "abc" and sess.missing_credentials() == []
    assert sess.status()["provider"] == "zerodha" and sess.status()["needs_daily_login"] is True
