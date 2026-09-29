import pytest

from terminal.core.models import OptionType, Side
from terminal.market.chain import OptionChainBuilder
from terminal.market.universe import Universe
from terminal.strategy.library import SPECS, build_legs, estimate_margin, net_credit_per_lot, payoff_profile


@pytest.fixture
def chain(tmp_path):
    u = Universe(tmp_path, ["NSE"]).get("NIFTY")
    return u, OptionChainBuilder(seed=2).build(u, 24800, 13.5, "2026-10-06")


@pytest.mark.parametrize("key", list(SPECS))
def test_every_strategy_builds(chain, key):
    u, ch = chain
    legs = build_legs(key, ch, 1)
    assert legs and all(l.symbol for l in legs)
    prof = payoff_profile(legs, ch.spot, u.lot_size)
    assert prof["max_profit"] > 0
    assert prof["undefined_risk"] == (not SPECS[key].defined_risk)
    assert estimate_margin(legs, ch.spot, u.lot_size, 1) > 0


def test_iron_condor_has_wings_and_credit(chain):
    u, ch = chain
    legs = build_legs("iron_condor", ch, 1)
    sells = [l for l in legs if l.side == Side.SELL]
    buys = [l for l in legs if l.side == Side.BUY]
    assert len(sells) == 2 and len(buys) == 2
    ce_sell = next(l for l in sells if l.option_type == OptionType.CE)
    ce_buy = next(l for l in buys if l.option_type == OptionType.CE)
    assert ce_buy.strike > ce_sell.strike
    assert net_credit_per_lot(legs, u.lot_size) > 0
    prof = payoff_profile(legs, ch.spot, u.lot_size)
    assert prof["max_loss"] < 0 and len(prof["breakevens"]) == 2


def test_straddle_breakevens_bracket_spot(chain):
    u, ch = chain
    legs = build_legs("short_straddle", ch, 1)
    prof = payoff_profile(legs, ch.spot, u.lot_size)
    assert prof["breakevens"][0] < ch.spot < prof["breakevens"][-1]
