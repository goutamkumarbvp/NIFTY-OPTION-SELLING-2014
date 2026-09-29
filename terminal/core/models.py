"""Domain models shared by every service of the terminal."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List

from pydantic import BaseModel, Field


def now_ts() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    MCX = "MCX"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    RISK_REJECTED = "RISK_REJECTED"
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderSource(str, Enum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"
    STRATEGY = "STRATEGY"
    SENTINEL = "SENTINEL"
    KILL_SWITCH = "KILL_SWITCH"


class TerminalMode(str, Enum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class TradingEnv(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class RiskLevel(str, Enum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"
    HALTED = "HALTED"


class Underlying(BaseModel):
    symbol: str
    exchange: Exchange
    name: str
    lot_size: int
    strike_step: float
    tick_size: float = 0.05
    expiry_weekday: int = 1  # 0=Mon ... 6=Sun; NIFTY weekly = Tuesday
    weekly: bool = True
    base_spot: float
    base_vol: float  # annualised
    session_open: str = "09:15"
    session_close: str = "15:30"


class Tick(BaseModel):
    symbol: str
    ltp: float
    ts: float = Field(default_factory=now_ts)
    change_pct: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: int = 0


class Candle(BaseModel):
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class OptionQuote(BaseModel):
    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: OptionType
    ltp: float
    bid: float
    ask: float
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    oi: int
    oi_change: int
    volume: int
    live: bool = False  # True when the price came from the broker feed (model price otherwise)


class ChainRow(BaseModel):
    strike: float
    ce: OptionQuote
    pe: OptionQuote


class OptionChain(BaseModel):
    underlying: str
    exchange: Exchange
    expiry: str
    spot: float
    ts: float
    atm_strike: float
    lot_size: int
    days_to_expiry: float
    rows: List[ChainRow]
    pcr: float
    pcr_volume: float
    max_pain: float
    total_ce_oi: int
    total_pe_oi: int
    iv_atm: float
    expected_move: float  # 1 sigma to expiry in points
    live_rows: int = 0  # rows priced from broker quotes

    def find(self, strike: float, option_type: OptionType) -> OptionQuote | None:
        for r in self.rows:
            if abs(r.strike - strike) < 1e-9:
                return r.ce if option_type == OptionType.CE else r.pe
        return None

    def by_delta(self, option_type: OptionType, target_delta: float) -> OptionQuote | None:
        best, best_err = None, 9e9
        for r in self.rows:
            q = r.ce if option_type == OptionType.CE else r.pe
            err = abs(abs(q.delta) - abs(target_delta))
            if err < best_err:
                best, best_err = q, err
        return best


class Order(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ORD"))
    symbol: str
    underlying: str
    exchange: Exchange
    expiry: str
    strike: float
    option_type: OptionType
    side: Side
    lots: int
    lot_size: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_price: float | None = None
    filled_qty: int = 0
    source: OrderSource = OrderSource.MANUAL
    strategy_run_id: str | None = None
    tag: str = ""
    reason: str = ""
    created_at: float = Field(default_factory=now_ts)
    updated_at: float = Field(default_factory=now_ts)
    broker_order_id: str | None = None
    charges: float = 0.0
    message: str = ""

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


class Position(BaseModel):
    symbol: str
    underlying: str
    exchange: Exchange
    expiry: str
    strike: float
    option_type: OptionType
    lot_size: int
    net_qty: int  # positive long, negative short (in units)
    avg_price: float
    ltp: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    strategy_run_id: str | None = None
    opened_at: float = Field(default_factory=now_ts)

    @property
    def lots(self) -> int:
        return int(abs(self.net_qty) // self.lot_size) if self.lot_size else 0


class Leg(BaseModel):
    option_type: OptionType
    side: Side
    strike: float
    lots: int
    symbol: str = ""
    entry_price: float = 0.0
    ltp: float = 0.0
    expiry: str = ""


class TradePlan(BaseModel):
    id: str = Field(default_factory=lambda: new_id("PLAN"))
    strategy: str
    underlying: str
    exchange: Exchange
    expiry: str
    legs: List[Leg]
    lots: int
    premium_collected: float  # per lot net credit (positive) / debit (negative)
    max_profit: float | None = None
    max_loss: float | None = None
    margin_estimate: float = 0.0
    stop_loss_pct: float = 35.0
    target_pct: float = 50.0
    rationale: List[str] = Field(default_factory=list)
    confidence: float = 0.0
    consensus: float = 0.0
    agent_votes: Dict[str, float] = Field(default_factory=dict)
    created_at: float = Field(default_factory=now_ts)
    status: str = "PROPOSED"  # PROPOSED | APPROVED | REJECTED | EXECUTED | EXPIRED | RISK_REJECTED
    source: OrderSource = OrderSource.AUTO
    breakevens: List[float] = Field(default_factory=list)


class StrategyRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("RUN"))
    strategy: str
    underlying: str
    exchange: Exchange
    expiry: str
    lots: int
    legs: List[Leg]
    premium_collected: float  # total net credit in currency
    stop_loss_pct: float
    target_pct: float
    trailing_lock_pct: float
    status: str = "ACTIVE"  # ACTIVE | EXITING | CLOSED
    entered_at: float = Field(default_factory=now_ts)
    closed_at: float | None = None
    mtm: float = 0.0
    peak_mtm: float = 0.0
    realized_pnl: float = 0.0
    exit_reason: str = ""
    adjustments: int = 0
    source: OrderSource = OrderSource.AUTO
    plan_id: str | None = None
    notes: List[str] = Field(default_factory=list)


class RiskSnapshot(BaseModel):
    level: RiskLevel = RiskLevel.GREEN
    daily_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    max_daily_loss: float = 0.0
    loss_budget_used_pct: float = 0.0
    margin_used: float = 0.0
    margin_available: float = 0.0
    margin_utilisation_pct: float = 0.0
    open_lots: int = 0
    open_positions: int = 0
    portfolio_delta: float = 0.0
    portfolio_gamma: float = 0.0
    portfolio_theta: float = 0.0
    portfolio_vega: float = 0.0
    kill_switch: bool = False
    safety_gate_open: bool = False
    breaches: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    per_market_lots: Dict[str, int] = Field(default_factory=dict)
    trades_today: int = 0
    ts: float = Field(default_factory=now_ts)


class Assessment(BaseModel):
    agent: str
    role: str
    stance: str  # e.g. BULLISH / BEARISH / NEUTRAL / SELL_PREMIUM / AVOID / VETO
    score: float  # -1 (avoid selling / bearish) .. +1 (favourable)
    confidence: float  # 0..1
    findings: List[str] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)
    veto: bool = False
    ts: float = Field(default_factory=now_ts)
    latency_ms: float = 0.0


class CouncilDecision(BaseModel):
    id: str = Field(default_factory=lambda: new_id("CNCL"))
    ts: float = Field(default_factory=now_ts)
    underlying: str
    mode: TerminalMode
    consensus: float
    confidence: float
    decision: str  # ENTER | HOLD | AVOID | VETO | EXIT | PROTECT
    assessments: List[Assessment]
    plan_id: str | None = None
    summary: str = ""


class Alert(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ALT"))
    ts: float = Field(default_factory=now_ts)
    level: str  # INFO | WARNING | CRITICAL
    category: str
    title: str
    body: str = ""
    market: str = ""
    acknowledged: bool = False


class AgentMessage(BaseModel):
    ts: float = Field(default_factory=now_ts)
    agent: str
    level: str = "INFO"
    text: str
    data: Dict[str, Any] = Field(default_factory=dict)
