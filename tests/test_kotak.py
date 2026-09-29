"""Kotak Neo adapter tests: pure parsers and feed normalisation (no SDK, no network)."""
import datetime as dt
from types import SimpleNamespace

from terminal.core.models import Tick
from terminal.market.kotak import choose_index_token, choose_nearest_future, parse_chain_rows, parse_expiry, parse_scrip_row, response_error, rows_of, tick_from_message


def test_parse_expiry_variants():
    assert parse_expiry("07-Oct-2026") == dt.date(2026, 10, 7)
    assert parse_expiry("2026-10-07") == dt.date(2026, 10, 7)
    assert parse_expiry("07OCT2026") == dt.date(2026, 10, 7)
    assert parse_expiry("1791395400") == dt.date(2026, 10, 7)  # epoch seconds (IST)
    assert parse_expiry("garbage") is None


def test_scrip_row_and_index_choice():
    rows = [{"pSymbol": "26000", "pTrdSymbol": "Nifty 50", "pSymbolName": "Nifty 50", "pExchSeg": "nse_cm"}, {"pSymbol": "26009", "pTrdSymbol": "Nifty Bank", "pSymbolName": "Nifty Bank", "pExchSeg": "nse_cm"}]
    hit = choose_index_token(rows, ["Nifty 50", "NIFTY"])
    assert hit["token"] == "26000" and hit["segment"] == "nse_cm"
    p = parse_scrip_row({"pSymbol": "51234", "pTrdSymbol": "NIFTY26OCT0724800CE", "pOptionType": "CE", "dStrikePrice": "2480000", "pExpiryDate": "07-Oct-2026", "lLotSize": "75", "pInstType": "OPTIDX"})
    assert p["strike"] == 24800 and p["option_type"] == "CE" and p["lot_size"] == 75 and p["expiry"] == dt.date(2026, 10, 7)


def test_nearest_future_for_mcx():
    today = dt.date(2026, 9, 29)
    rows = [{"pSymbol": "1", "pTrdSymbol": "CRUDEOIL26OCTFUT", "pExpiryDate": "19-Oct-2026", "pInstType": "FUTCOM", "pOptionType": "XX"},
            {"pSymbol": "2", "pTrdSymbol": "CRUDEOIL26NOVFUT", "pExpiryDate": "18-Nov-2026", "pInstType": "FUTCOM", "pOptionType": "XX"},
            {"pSymbol": "3", "pTrdSymbol": "CRUDEOIL26OCT5600CE", "pExpiryDate": "15-Oct-2026", "pInstType": "OPTFUT", "pOptionType": "CE", "dStrikePrice": "5600"}]
    assert choose_nearest_future(rows, today)["token"] == "1"


def test_chain_parsing_flat_and_nested():
    flat = {"data": [{"strikePrice": 24800, "optionType": "CE", "ltp": 130.5, "openInterest": 1200000, "impliedVolatility": 12.4, "tradingSymbol": "NIFTY26OCT0724800CE"},
                     {"strikePrice": 24800, "optionType": "PE", "lastPrice": "118.2", "oi": "900000"}]}
    q = parse_chain_rows(rows_of(flat))
    assert q[(24800.0, "CE")]["ltp"] == 130.5 and q[(24800.0, "CE")]["oi"] == 1200000 and q[(24800.0, "PE")]["ltp"] == 118.2
    nested = [{"strike": "24850", "CE": {"ltp": 100, "oi": 10}, "PE": {"ltp": 140, "oi": 20}}]
    q2 = parse_chain_rows(nested)
    assert q2[(24850.0, "CE")]["ltp"] == 100 and q2[(24850.0, "PE")]["oi"] == 20


def test_response_error_detection():
    assert response_error({"stat": "Not_Ok", "errMsg": "Invalid TOTP"}) == "Invalid TOTP"
    assert response_error({"data": []}) is None
    assert response_error({"code": "401", "message": "expired"}) == "expired"


def test_tick_from_sfeed_messages():
    idx = SimpleNamespace(instrument_token="26000", exchange_segment="nse_cm", last_traded_price=24812.5, close_price=24700.0, open_price=24750.0, high_price=24850.0, low_price=24690.0, net_change_percent=0.455)
    f = tick_from_message(idx)
    assert f["ltp"] == 24812.5 and f["prev_close"] == 24700.0 and abs(f["change_pct"] - 0.455) < 1e-9
    scrip = SimpleNamespace(instrument_token="1", exchange_segment="mcx_fo", last_traded_price=5640.0, close_price=5600.0, volume_traded_today=12345, open_interest=9999)
    f2 = tick_from_message(scrip)
    assert f2["volume"] == 12345 and f2["oi"] == 9999 and abs(f2["change_pct"] - (5640 / 5600 - 1) * 100) < 1e-9
    assert tick_from_message(SimpleNamespace(instrument_token="x", last_traded_price=0)) is None
    Tick(symbol="NIFTY", ltp=f["ltp"], change_pct=f["change_pct"])


def test_chain_builder_overlays_broker_quotes(tmp_path):
    from terminal.market.chain import OptionChainBuilder
    from terminal.market.universe import Universe
    u = Universe(tmp_path, ["NSE"]).get("NIFTY")
    b = OptionChainBuilder(seed=5)
    sym = b.option_symbol("NIFTY", "2026-10-06", 24800, "CE")
    assert sym == "NIFTY06OCT2624800CE"
    b.apply_broker_quotes({sym: {"ltp": 250.0, "oi": 4200000, "volume": 100, "oi_change": 5000}})
    ch = b.build(u, 24800, 13.0, "2026-10-06")
    q = next(r.ce for r in ch.rows if r.strike == 24800)
    assert q.ltp == 250.0 and q.oi == 4200000 and q.iv > 0  # IV solved from the live price
