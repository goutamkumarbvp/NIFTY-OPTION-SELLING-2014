"""Settings. Loaded once from environment / .env; hard limits are immutable for the process lifetime.

Live order flow needs BOTH deployment switches (AMRT_ENVIRONMENT=LIVE_CAPABLE and
AMRT_LIVE_ORDERS_ENABLED=true) *and* a deliberate, authenticated owner mode
transition at runtime. Nothing in this file can activate live trading by itself.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from amrt.core.enums import Environment


class HardLimits(BaseModel):
    """Absolute ceilings. Owner-approved risk policies are validated against these and can never exceed them.
    Changing them requires editing the deployment configuration and restarting (an audited deployment change)."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_loss_threshold_inr: float = 50_000.0        # no policy may set a loss trigger above this
    max_margin_risk_pct: float = 5.0
    max_lots_per_order: int = 20
    max_open_lots: int = 60
    max_gross_exposure_inr: float = 50_000_000.0
    max_margin_utilisation_pct: float = 90.0
    max_orders_per_minute: int = 60
    allow_naked_short: bool = True
    max_data_age_ms: int = 5_000                     # policies may only be *stricter*
    max_clock_drift_ms: int = 2_000
    max_price_collar_pct: float = 10.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    # --- deployment ---------------------------------------------------------
    environment: Environment = Field(Environment.PAPER_ONLY, alias="AMRT_ENVIRONMENT")
    live_orders_enabled: bool = Field(False, alias="AMRT_LIVE_ORDERS_ENABLED")
    runtime_dir: Path = Field(Path("runtime"), alias="AMRT_RUNTIME_DIR")
    database_url: str = Field("", alias="AMRT_DATABASE_URL")          # blank = sqlite in runtime dir
    redis_url: str = Field("", alias="AMRT_REDIS_URL")                # blank = in-process cache
    bind_host: str = Field("127.0.0.1", alias="AMRT_BIND_HOST")
    port: int = Field(8700, alias="AMRT_PORT")
    log_format: str = Field("text", alias="AMRT_LOG_FORMAT")
    allowed_origins: str = Field("", alias="AMRT_ALLOWED_ORIGINS")
    session_ttl_minutes: int = Field(480, alias="AMRT_SESSION_TTL_MINUTES")
    step_up_ttl_seconds: int = Field(300, alias="AMRT_STEP_UP_TTL_SECONDS")
    login_lockout_attempts: int = Field(5, alias="AMRT_LOGIN_LOCKOUT_ATTEMPTS")
    login_lockout_minutes: int = Field(15, alias="AMRT_LOGIN_LOCKOUT_MINUTES")
    owner_bootstrap_password: str = Field("", alias="AMRT_OWNER_BOOTSTRAP_PASSWORD")

    # --- market data --------------------------------------------------------
    data_broker: str = Field("", alias="AMRT_DATA_BROKER")             # kotak | zerodha | angel | upstox | groww | "" (none)
    underlyings: str = Field("NIFTY,SENSEX,CRUDEOIL", alias="AMRT_UNDERLYINGS")
    chain_strikes_each_side: int = Field(10, alias="AMRT_CHAIN_STRIKES_EACH_SIDE")
    chain_poll_seconds: float = Field(5.0, alias="AMRT_CHAIN_POLL_SECONDS")
    replay_file: str = Field("", alias="AMRT_REPLAY_FILE")             # labelled HISTORICAL REPLAY when set
    simulated_market: bool = Field(False, alias="AMRT_SIMULATED_MARKET")  # SIMULATED data for PAPER_ONLY demos; refused when LIVE_CAPABLE
    events_calendar_file: str = Field("", alias="AMRT_EVENTS_CALENDAR_FILE")

    # --- loops ---------------------------------------------------------------
    risk_monitor_seconds: float = Field(1.0, alias="AMRT_RISK_MONITOR_SECONDS")
    safety_monitor_seconds: float = Field(1.0, alias="AMRT_SAFETY_MONITOR_SECONDS")
    reconcile_seconds: float = Field(10.0, alias="AMRT_RECONCILE_SECONDS")
    agent_cycle_seconds: float = Field(30.0, alias="AMRT_AGENT_CYCLE_SECONDS")
    supervisor_seconds: float = Field(2.0, alias="AMRT_SUPERVISOR_SECONDS")
    watchdog_stall_seconds: float = Field(15.0, alias="AMRT_WATCHDOG_STALL_SECONDS")
    order_ack_timeout_seconds: float = Field(8.0, alias="AMRT_ORDER_ACK_TIMEOUT_SECONDS")
    unknown_not_found_grace_seconds: float = Field(60.0, alias="AMRT_UNKNOWN_GRACE_SECONDS")
    backup_interval_minutes: float = Field(60.0, alias="AMRT_BACKUP_INTERVAL_MINUTES")
    backup_keep: int = Field(24, alias="AMRT_BACKUP_KEEP")

    # --- broker credentials (never logged; registered with the redactor) ---
    kotak_consumer_key: str = Field("", alias="NEO_CONSUMER_KEY")
    kotak_mobile: str = Field("", alias="NEO_MOBILE_NUMBER")
    kotak_ucc: str = Field("", alias="NEO_UCC")
    kotak_mpin: str = Field("", alias="NEO_MPIN")
    kotak_totp_secret: str = Field("", alias="NEO_TOTP_SECRET")
    zerodha_api_key: str = Field("", alias="ZERODHA_API_KEY")
    zerodha_api_secret: str = Field("", alias="ZERODHA_API_SECRET")
    zerodha_access_token: str = Field("", alias="ZERODHA_ACCESS_TOKEN")
    angel_api_key: str = Field("", alias="ANGEL_API_KEY")
    angel_client_code: str = Field("", alias="ANGEL_CLIENT_CODE")
    angel_pin: str = Field("", alias="ANGEL_PIN")
    angel_totp_secret: str = Field("", alias="ANGEL_TOTP_SECRET")
    upstox_access_token: str = Field("", alias="UPSTOX_ACCESS_TOKEN")
    groww_api_key: str = Field("", alias="GROWW_API_KEY")
    groww_api_secret: str = Field("", alias="GROWW_API_SECRET")
    groww_access_token: str = Field("", alias="GROWW_ACCESS_TOKEN")

    # --- paper ----------------------------------------------------------------
    paper_capital_inr: float = Field(1_000_000.0, alias="AMRT_PAPER_CAPITAL")
    paper_slippage_bps: float = Field(5.0, alias="AMRT_PAPER_SLIPPAGE_BPS")

    # --- optional LLM narrative (never decides) ------------------------------
    llm_enabled: bool = Field(False, alias="AMRT_LLM_ENABLED")
    llm_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    llm_model: str = Field("claude-opus-5-5", alias="AMRT_LLM_MODEL")

    # --- alerts ---------------------------------------------------------------
    telegram_bot_token: str = Field("", alias="AMRT_TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="AMRT_TELEGRAM_CHAT_ID")

    # --- hard limits (deployment-time only) --------------------------------
    hard_max_loss_threshold_inr: float = Field(50_000.0, alias="AMRT_HARD_MAX_LOSS_THRESHOLD_INR")
    hard_max_margin_risk_pct: float = Field(5.0, alias="AMRT_HARD_MAX_MARGIN_RISK_PCT")
    hard_max_lots_per_order: int = Field(20, alias="AMRT_HARD_MAX_LOTS_PER_ORDER")
    hard_max_open_lots: int = Field(60, alias="AMRT_HARD_MAX_OPEN_LOTS")
    hard_max_gross_exposure_inr: float = Field(50_000_000.0, alias="AMRT_HARD_MAX_GROSS_EXPOSURE_INR")
    hard_max_margin_utilisation_pct: float = Field(90.0, alias="AMRT_HARD_MAX_MARGIN_UTILISATION_PCT")
    hard_max_orders_per_minute: int = Field(60, alias="AMRT_HARD_MAX_ORDERS_PER_MINUTE")
    hard_allow_naked_short: bool = Field(True, alias="AMRT_HARD_ALLOW_NAKED_SHORT")

    @field_validator("data_broker")
    @classmethod
    def _broker(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in {"", "kotak", "zerodha", "angel", "upstox", "groww"}:
            raise ValueError("AMRT_DATA_BROKER must be kotak, zerodha, angel, upstox, groww or blank")
        return v

    @property
    def live_capable(self) -> bool:
        return self.environment == Environment.LIVE_CAPABLE and self.live_orders_enabled

    @property
    def underlying_list(self) -> list[str]:
        return [u.strip().upper() for u in self.underlyings.split(",") if u.strip()]

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.runtime_dir / 'amrt.sqlite3').resolve()}"

    def hard_limits(self) -> HardLimits:
        return HardLimits(max_loss_threshold_inr=self.hard_max_loss_threshold_inr, max_margin_risk_pct=self.hard_max_margin_risk_pct,
                          max_lots_per_order=self.hard_max_lots_per_order, max_open_lots=self.hard_max_open_lots,
                          max_gross_exposure_inr=self.hard_max_gross_exposure_inr, max_margin_utilisation_pct=self.hard_max_margin_utilisation_pct,
                          max_orders_per_minute=self.hard_max_orders_per_minute, allow_naked_short=self.hard_allow_naked_short)

    def secret_values(self) -> list[str]:
        return [self.kotak_consumer_key, self.kotak_mobile, self.kotak_mpin, self.kotak_totp_secret, self.kotak_ucc, self.zerodha_api_key,
                self.zerodha_api_secret, self.zerodha_access_token, self.angel_api_key, self.angel_pin, self.angel_totp_secret,
                self.upstox_access_token, self.groww_api_key, self.groww_api_secret, self.groww_access_token, self.llm_api_key,
                self.telegram_bot_token, self.owner_bootstrap_password]

    def public_view(self) -> dict:
        from amrt.security.redact import REDACTOR
        d = self.model_dump(mode="json")
        return REDACTOR.value(d)
