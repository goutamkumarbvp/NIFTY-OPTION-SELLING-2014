"""Runtime configuration (environment / .env driven).

Every safety relevant flag defaults to the safest value:
* TERMINAL_MODE = MANUAL   (agents advise, humans approve)
* TRADING_ENV   = PAPER    (simulated fills, no broker orders)
* LIVE_TRADING  = false    (hard interlock, must be true AND LIVE_ORDERS_ENABLED)
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- server -----------------------------------------------------------
    bind_host: str = Field("127.0.0.1", alias="BIND_HOST")
    port: int = Field(8600, alias="PORT")
    api_auth_token: str = Field("", alias="API_AUTH_TOKEN")
    users: str = Field("", alias="TERMINAL_USERS")  # "name:password:role,name2:password:role"
    runtime_dir: Path = Field(ROOT / "runtime", alias="RUNTIME_DIR")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # --- modes / interlocks ----------------------------------------------
    terminal_mode: str = Field("MANUAL", alias="TERMINAL_MODE")  # MANUAL | AUTO
    trading_env: str = Field("PAPER", alias="TRADING_ENV")  # PAPER | LIVE
    live_trading: bool = Field(False, alias="LIVE_TRADING")
    live_orders_enabled: bool = Field(False, alias="LIVE_ORDERS_ENABLED")
    safety_gate_open_on_start: bool = Field(False, alias="SAFETY_GATE_OPEN_ON_START")
    protect_in_manual: bool = Field(True, alias="PROTECT_IN_MANUAL")  # sentinel may flatten in MANUAL
    manual_confirmation_for_agent_orders: bool = Field(True, alias="MANUAL_CONFIRMATION_FOR_AGENT_ORDERS")

    # --- market data -------------------------------------------------------
    data_source: str = Field("simulated", alias="DATA_SOURCE")  # simulated | kotak
    markets: str = Field("NSE,BSE,MCX", alias="MARKETS")
    sim_speed: float = Field(1.0, alias="SIM_SPEED")  # simulation time multiplier
    sim_always_open: bool = Field(True, alias="SIM_ALWAYS_OPEN")  # keep simulated market open 24x7
    tick_interval_seconds: float = Field(1.0, alias="TICK_INTERVAL_SECONDS")
    feed_stale_seconds: float = Field(8.0, alias="FEED_STALE_SECONDS")

    # --- broker ------------------------------------------------------------
    broker: str = Field("paper", alias="BROKER")  # paper | kotak | zerodha
    neo_consumer_key: str = Field("", alias="NEO_CONSUMER_KEY")
    neo_mobile_number: str = Field("", alias="NEO_MOBILE_NUMBER")
    neo_ucc: str = Field("", alias="NEO_UCC")
    neo_mpin: str = Field("", alias="NEO_MPIN")
    neo_totp_secret: str = Field("", alias="NEO_TOTP_SECRET")
    neo_environment: str = Field("prod", alias="NEO_ENVIRONMENT")
    kotak_chain_poll_seconds: float = Field(4.0, alias="KOTAK_CHAIN_POLL_SECONDS")
    kotak_chain_underlyings: str = Field("", alias="KOTAK_CHAIN_UNDERLYINGS")  # blank = council focus list
    zerodha_api_key: str = Field("", alias="ZERODHA_API_KEY")
    zerodha_access_token: str = Field("", alias="ZERODHA_ACCESS_TOKEN")

    # --- capital & risk ----------------------------------------------------
    capital: float = Field(1_000_000.0, alias="CAPITAL")
    max_daily_loss: float = Field(30_000.0, alias="MAX_DAILY_LOSS")
    max_daily_loss_pct: float = Field(3.0, alias="MAX_DAILY_LOSS_PCT")
    daily_profit_target: float = Field(0.0, alias="DAILY_PROFIT_TARGET")  # 0 = disabled
    max_lots_per_order: int = Field(10, alias="MAX_LOTS_PER_ORDER")
    max_open_lots: int = Field(40, alias="MAX_OPEN_LOTS")
    max_open_lots_per_market: int = Field(20, alias="MAX_OPEN_LOTS_PER_MARKET")
    max_open_positions: int = Field(12, alias="MAX_OPEN_POSITIONS")
    max_margin_utilisation_pct: float = Field(70.0, alias="MAX_MARGIN_UTILISATION_PCT")
    per_trade_stop_loss_pct: float = Field(35.0, alias="PER_TRADE_STOP_LOSS_PCT")  # % of premium collected
    per_trade_target_pct: float = Field(50.0, alias="PER_TRADE_TARGET_PCT")
    trailing_lock_pct: float = Field(25.0, alias="TRAILING_LOCK_PCT")
    portfolio_delta_limit: float = Field(150.0, alias="PORTFOLIO_DELTA_LIMIT")  # in lots-equivalent delta units
    portfolio_vega_limit: float = Field(25_000.0, alias="PORTFOLIO_VEGA_LIMIT")
    black_swan_sigma: float = Field(4.0, alias="BLACK_SWAN_SIGMA")
    black_swan_window_minutes: int = Field(5, alias="BLACK_SWAN_WINDOW_MINUTES")
    vix_spike_pct: float = Field(15.0, alias="VIX_SPIKE_PCT")
    naked_short_allowed: bool = Field(True, alias="NAKED_SHORT_ALLOWED")

    # --- strategy schedule -----------------------------------------------
    entry_window_start: str = Field("09:20", alias="ENTRY_WINDOW_START")
    entry_window_end: str = Field("14:30", alias="ENTRY_WINDOW_END")
    square_off_time: str = Field("15:12", alias="SQUARE_OFF_TIME")
    mcx_square_off_time: str = Field("23:15", alias="MCX_SQUARE_OFF_TIME")
    default_lots: int = Field(1, alias="DEFAULT_LOTS")

    # --- agents ------------------------------------------------------------
    agent_cycle_seconds: float = Field(10.0, alias="AGENT_CYCLE_SECONDS")
    auto_min_consensus: float = Field(0.62, alias="AUTO_MIN_CONSENSUS")
    auto_min_confidence: float = Field(0.55, alias="AUTO_MIN_CONFIDENCE")
    auto_max_trades_per_day: int = Field(6, alias="AUTO_MAX_TRADES_PER_DAY")
    llm_enabled: bool = Field(False, alias="LLM_ENABLED")
    llm_provider: str = Field("anthropic", alias="LLM_PROVIDER")
    llm_model: str = Field("claude-opus-5-5", alias="LLM_MODEL")
    llm_api_key: str = Field("", alias="LLM_API_KEY")
    llm_timeout_seconds: float = Field(25.0, alias="LLM_TIMEOUT_SECONDS")

    # --- notifications -----------------------------------------------------
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="TELEGRAM_CHAT_ID")
    smtp_host: str = Field("", alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: str = Field("", alias="SMTP_USER")
    smtp_password: str = Field("", alias="SMTP_PASSWORD")
    alert_email_to: str = Field("", alias="ALERT_EMAIL_TO")

    @field_validator("terminal_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in {"MANUAL", "AUTO"}:
            raise ValueError("TERMINAL_MODE must be MANUAL or AUTO")
        return v

    @field_validator("trading_env")
    @classmethod
    def _env(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in {"PAPER", "LIVE"}:
            raise ValueError("TRADING_ENV must be PAPER or LIVE")
        return v

    @property
    def market_list(self) -> List[str]:
        return [m.strip().upper() for m in self.markets.split(",") if m.strip()]

    @property
    def live_allowed(self) -> bool:
        """LIVE order flow requires *all three* interlocks."""
        return self.trading_env == "LIVE" and self.live_trading and self.live_orders_enabled

    @property
    def is_loopback(self) -> bool:
        return self.bind_host in {"127.0.0.1", "localhost", "::1"}

    def user_table(self) -> List[dict]:
        out = []
        for chunk in self.users.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(":")
            if len(parts) < 2:
                continue
            name, pwd = parts[0], parts[1]
            role = parts[2] if len(parts) > 2 else "trader"
            out.append({"username": name, "password": pwd, "role": role.lower()})
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.runtime_dir.mkdir(parents=True, exist_ok=True)
    return s


def reset_settings_cache() -> None:
    get_settings.cache_clear()
