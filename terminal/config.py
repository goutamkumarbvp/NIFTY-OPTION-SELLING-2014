"""Runtime configuration (environment / .env driven).

Every safety relevant flag defaults to the safest value:
* TERMINAL_MODE = MANUAL   (agents advise, humans approve)
* TRADING_ENV   = PAPER    (simulated fills, no broker orders)
* LIVE_TRADING  = false    (hard interlock, must be true AND LIVE_ORDERS_ENABLED)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import AliasChoices, Field, field_validator
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
    log_format: str = Field("text", alias="LOG_FORMAT")  # text | json

    # --- modes / interlocks ----------------------------------------------
    terminal_mode: str = Field("MANUAL", alias="TERMINAL_MODE")  # MANUAL | AUTO
    trading_env: str = Field("PAPER", alias="TRADING_ENV")  # PAPER | LIVE
    live_trading: bool = Field(False, alias="LIVE_TRADING")
    live_orders_enabled: bool = Field(False, alias="LIVE_ORDERS_ENABLED")
    safety_gate_open_on_start: bool = Field(False, alias="SAFETY_GATE_OPEN_ON_START")
    protect_in_manual: bool = Field(True, alias="PROTECT_IN_MANUAL")  # sentinel may flatten in MANUAL
    manual_confirmation_for_agent_orders: bool = Field(True, alias="MANUAL_CONFIRMATION_FOR_AGENT_ORDERS")

    # --- market data (live only) -------------------------------------------
    data_source: str = Field("kotak", alias="DATA_SOURCE")  # kotak | zerodha | angel
    markets: str = Field("NSE,BSE,MCX", alias="MARKETS")
    feed_stale_seconds: float = Field(8.0, alias="FEED_STALE_SECONDS")
    candle_seconds: int = Field(60, alias="CANDLE_SECONDS")  # indicator candle size; ticks are consumed at full resolution regardless
    tick_eval_min_interval_ms: int = Field(100, alias="TICK_EVAL_MIN_INTERVAL_MS")  # stop-loss re-evaluation throttle per symbol

    # --- broker ------------------------------------------------------------
    broker: str = Field("paper", alias="BROKER")  # paper | kotak | zerodha | angel
    neo_consumer_key: str = Field("", alias="NEO_CONSUMER_KEY")
    neo_mobile_number: str = Field("", alias="NEO_MOBILE_NUMBER")
    neo_ucc: str = Field("", alias="NEO_UCC")
    neo_mpin: str = Field("", alias="NEO_MPIN")
    neo_totp_secret: str = Field("", alias="NEO_TOTP_SECRET")
    neo_environment: str = Field("prod", alias="NEO_ENVIRONMENT")
    zerodha_api_key: str = Field("", alias="ZERODHA_API_KEY")
    zerodha_api_secret: str = Field("", alias="ZERODHA_API_SECRET")
    zerodha_access_token: str = Field("", alias="ZERODHA_ACCESS_TOKEN")
    zerodha_request_token: str = Field("", alias="ZERODHA_REQUEST_TOKEN")
    angel_api_key: str = Field("", alias="ANGEL_API_KEY")
    angel_client_code: str = Field("", alias="ANGEL_CLIENT_CODE")
    angel_pin: str = Field("", alias="ANGEL_PIN")
    angel_totp_secret: str = Field("", alias="ANGEL_TOTP_SECRET")
    live_chain_poll_seconds: float = Field(4.0, alias="LIVE_CHAIN_POLL_SECONDS")
    live_chain_underlyings: str = Field("", alias="LIVE_CHAIN_UNDERLYINGS")  # blank = council focus list

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
    terminal_start_time: str = Field("09:00", validation_alias=AliasChoices("TERMINAL_START_TIME", "ENTRY_START", "START_TIME"))  # operating window (IST)
    terminal_end_time: str = Field("23:30", validation_alias=AliasChoices("TERMINAL_END_TIME", "EXIT_TIME", "END_TIME"))  # everything still open is squared off here
    exit_retry_seconds: float = Field(10.0, alias="EXIT_RETRY_SECONDS")     # re-send an unfilled square-off every N seconds
    exit_max_attempts: int = Field(60, alias="EXIT_MAX_ATTEMPTS")
    reconcile_seconds: float = Field(60.0, alias="RECONCILE_SECONDS")       # broker position / margin reconciliation interval
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
    copilot_max_turns: int = Field(6, alias="COPILOT_MAX_TURNS")  # tool-call rounds per question
    decision_score_minutes: int = Field(30, alias="DECISION_SCORE_MINUTES")  # horizon for council evaluation

    # --- notifications -----------------------------------------------------
    # --- institutional controls ----------------------------------------------
    dq_max_underlying_jump_pct: float = Field(8.0, alias="DQ_MAX_UNDERLYING_JUMP_PCT")   # tick outlier gate (held until confirmed)
    dq_max_option_jump_pct: float = Field(60.0, alias="DQ_MAX_OPTION_JUMP_PCT")
    dq_stale_timestamp_seconds: float = Field(30.0, alias="DQ_STALE_TIMESTAMP_SECONDS")
    tick_recording: bool = Field(True, alias="TICK_RECORDING")                          # runtime/ticks/YYYY-MM-DD.jsonl.gz journal
    slo_tick_to_mark_ms: float = Field(50.0, alias="SLO_TICK_TO_MARK_MS")
    slo_quote_to_eval_ms: float = Field(150.0, alias="SLO_QUOTE_TO_EVAL_MS")
    slo_order_fill_ms: float = Field(3000.0, alias="SLO_ORDER_FILL_MS")
    slo_council_cycle_ms: float = Field(8000.0, alias="SLO_COUNCIL_CYCLE_MS")
    backup_dir: str = Field("", alias="BACKUP_DIR")                                       # blank = RUNTIME_DIR/backups
    backup_keep: int = Field(14, alias="BACKUP_KEEP")
    backup_interval_minutes: float = Field(30.0, alias="BACKUP_INTERVAL_MINUTES")        # 0 = only at end of day
    four_eyes_required: bool | None = Field(None, alias="FOUR_EYES_REQUIRED")          # default: on when TRADING_ENV=LIVE
    session_ttl_hours: float = Field(12.0, alias="SESSION_TTL_HOURS")
    login_lockout_attempts: int = Field(5, alias="LOGIN_LOCKOUT_ATTEMPTS")
    login_lockout_minutes: float = Field(15.0, alias="LOGIN_LOCKOUT_MINUTES")

    # --- guardian (self-monitoring / self-healing; asks for permission by default) ---
    guardian_enabled: bool = Field(True, alias="GUARDIAN_ENABLED")
    guardian_interval_seconds: float = Field(5.0, alias="GUARDIAN_INTERVAL_SECONDS")
    guardian_auto_apply: str = Field("none", alias="GUARDIAN_AUTO_APPLY")  # none | low | medium | all  (flatten always needs approval)
    guardian_approval_ttl_seconds: float = Field(900.0, alias="GUARDIAN_APPROVAL_TTL_SECONDS")
    guardian_llm_diagnosis: bool = Field(True, alias="GUARDIAN_LLM_DIAGNOSIS")  # Claude root-cause note when LLM_ENABLED
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="TELEGRAM_CHAT_ID")
    telegram_commands_enabled: bool = Field(True, alias="TELEGRAM_COMMANDS_ENABLED")
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

    @field_validator("data_source")
    @classmethod
    def _source(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"kotak", "zerodha", "angel"}:
            raise ValueError("DATA_SOURCE must be kotak, zerodha or angel (the terminal is live-data only)")
        return v

    @field_validator("guardian_auto_apply")
    @classmethod
    def _guardian_policy(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"none", "low", "medium", "all"}:
            raise ValueError("GUARDIAN_AUTO_APPLY must be none, low, medium or all")
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
