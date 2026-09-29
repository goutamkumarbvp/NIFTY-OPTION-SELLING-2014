"""Angel One adapter tests: pure helpers only (no SDK, no network)."""
import datetime as dt

from terminal.market.angel import choose_index, choose_nearest_future, expiries_for, option_instruments, parse_expiry, parse_market_quote, parse_master_row, response_error, tick_from_smart

MASTER = [
    {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "strike": "-1.000000", "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0.000000"},
    {"token": "99926017", "symbol": "India VIX", "name": "INDIA VIX", "expiry": "", "strike": "-1.000000", "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0.000000"},
    {"token": "43450", "symbol": "NIFTY07OCT2624800CE", "name": "NIFTY", "expiry": "07OCT2026", "strike": "2480000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
    {"token": "43451", "symbol": "NIFTY07OCT2624800PE", "name": "NIFTY", "expiry": "07OCT2026", "strike": "2480000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
    {"token": "43999", "symbol": "NIFTY27OCT2624800CE", "name": "NIFTY", "expiry": "27OCT2026", "strike": "2480000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
    {"token": "250001", "symbol": "CRUDEOIL19OCT26FUT", "name": "CRUDEOIL", "expiry": "19OCT2026", "strike": "-1.000000", "lotsize": "100", "instrumenttype": "FUTCOM", "exch_seg": "MCX", "tick_size": "100.000000"},
    {"token": "250002", "symbol": "CRUDEOIL18NOV26FUT", "name": "CRUDEOIL", "expiry": "18NOV2026", "strike": "-1.000000", "lotsize": "100", "instrumenttype": "FUTCOM", "exch_seg": "MCX", "tick_size": "100.000000"},
]


def test_master_parsing_and_lookups():
    p = parse_master_row(MASTER[2])
    assert p["strike"] == 24800.0 and p["expiry"] == dt.date(2026, 10, 7) and p["lot_size"] == 75 and p["tick_size"] == 0.05
    assert parse_expiry("07OCT2026") == dt.date(2026, 10, 7) and parse_expiry("") is None
    assert choose_index(MASTER, ["Nifty 50"], "NSE")["token"] == "99926000"
    assert choose_index(MASTER, ["India VIX"], "NSE")["token"] == "99926017"
    assert choose_index(MASTER, ["SENSEX"], "BSE") is None
    assert choose_nearest_future(MASTER, "CRUDEOIL", "MCX", dt.date(2026, 9, 29))["token"] == "250001"
    table = option_instruments(MASTER, "NIFTY", "NFO", dt.date(2026, 10, 7))
    assert set(table) == {(24800.0, "CE"), (24800.0, "PE")} and table[(24800.0, "PE")]["token"] == "43451"
    assert expiries_for(MASTER, "NIFTY", "NFO", dt.date(2026, 9, 29)) == [dt.date(2026, 10, 7), dt.date(2026, 10, 27)]


def test_quote_tick_and_error_parsing():
    q = parse_market_quote({"symbolToken": "43450", "tradingSymbol": "NIFTY07OCT2624800CE", "ltp": 131.4, "opnInterest": 1500000, "tradeVolume": 240000, "close": 125, "depth": {"buy": [{"price": 131.2}], "sell": [{"price": 131.6}]}})
    assert q["ltp"] == 131.4 and q["oi"] == 1500000 and q["bid"] == 131.2 and q["ask"] == 131.6 and q["prev_close"] == 125
    t = tick_from_smart({"token": "99926000", "exchange_type": 1, "last_traded_price": 2481250, "closed_price": 2470000, "open_price_of_the_day": 2475000, "high_price_of_the_day": 2485000, "low_price_of_the_day": 2469000, "volume_trade_for_the_day": 0})
    assert t["ltp"] == 24812.5 and t["prev_close"] == 24700.0 and abs(t["change_pct"] - (24812.5 / 24700 - 1) * 100) < 1e-9
    assert tick_from_smart({"token": "x", "last_traded_price": 0}) is None
    assert response_error({"status": False, "message": "Invalid totp", "errorcode": "AB1050"}) == "Invalid totp"
    assert response_error({"status": True, "data": {}}) is None


def test_session_credential_gate(tmp_path):
    from terminal.market.angel import AngelOneSession
    from tests.conftest import make_settings
    sess = AngelOneSession(make_settings(DATA_SOURCE="angel", ANGEL_API_KEY="k"), tmp_path)
    assert sess.missing_credentials() == ["ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    assert sess.status()["provider"] == "angel"
