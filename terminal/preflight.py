"""Startup preflight: prints exactly which interlocks and providers are active
and refuses obviously unsafe or inconsistent configurations."""
from __future__ import annotations

from typing import List, Tuple

from terminal.config import Settings


def check(s: Settings) -> Tuple[List[str], List[str]]:
    """Returns (info lines, fatal errors)."""
    info: List[str] = []
    fatal: List[str] = []
    info.append(f"mode={s.terminal_mode}  env={s.trading_env}  data={s.data_source}  broker={s.broker}  markets={','.join(s.market_list)}")
    info.append(f"operating window {s.terminal_start_time}-{s.terminal_end_time} IST · entries {s.entry_window_start}-{s.entry_window_end} · square-off {s.square_off_time} (MCX {s.mcx_square_off_time})")
    info.append(f"risk: max daily loss ₹{s.max_daily_loss:,.0f} · max lots/order {s.max_lots_per_order} · max open lots {s.max_open_lots} · margin cap {s.max_margin_utilisation_pct}% · exit retry {s.exit_retry_seconds:.0f}s")
    interlocks = {"TRADING_ENV=LIVE": s.trading_env == "LIVE", "LIVE_TRADING": s.live_trading, "LIVE_ORDERS_ENABLED": s.live_orders_enabled, "BROKER live": s.broker.lower() in ("kotak", "zerodha", "angel")}
    on = [k for k, v in interlocks.items() if v]
    if s.live_allowed and s.broker.lower() in ("kotak", "zerodha", "angel"):
        info.append("*** LIVE ORDER FLOW ENABLED *** all interlocks on: " + ", ".join(on))
    else:
        info.append("live order flow OFF (paper broker) — interlocks on: " + (", ".join(on) or "none"))
    providers = {s.data_source.lower()} | ({s.broker.lower()} if s.trading_env == "LIVE" else set())
    providers -= {"paper"}
    if len(providers) > 1:
        fatal.append(f"DATA_SOURCE and BROKER name different live providers: {sorted(providers)}")
    for prov in providers:
        if prov == "kotak":
            missing = [k for k, v in {"NEO_CONSUMER_KEY": s.neo_consumer_key, "NEO_MOBILE_NUMBER": s.neo_mobile_number, "NEO_UCC": s.neo_ucc, "NEO_MPIN": s.neo_mpin, "NEO_TOTP_SECRET": s.neo_totp_secret}.items() if not v]
        elif prov == "zerodha":
            missing = [k for k, v in {"ZERODHA_API_KEY": s.zerodha_api_key}.items() if not v]
        elif prov == "angel":
            missing = [k for k, v in {"ANGEL_API_KEY": s.angel_api_key, "ANGEL_CLIENT_CODE": s.angel_client_code, "ANGEL_PIN": s.angel_pin, "ANGEL_TOTP_SECRET": s.angel_totp_secret}.items() if not v]
        else:
            fatal.append(f"unknown live provider {prov}")
            continue
        if missing:
            fatal.append(f"provider {prov}: missing " + ", ".join(missing) + " — the terminal is live-data only and cannot start without them")
        else:
            info.append(f"provider {prov}: credentials present · live ticks at broker resolution (every update processed, no sampling)")
    if not s.is_loopback and not (s.api_auth_token or s.users):
        fatal.append("non-loopback BIND_HOST requires API_AUTH_TOKEN or TERMINAL_USERS")
    if s.live_allowed and s.terminal_mode == "AUTO" and s.safety_gate_open_on_start:
        info.append("WARNING: LIVE + AUTO + gate open on start — the council can trade real money immediately")
    if s.llm_enabled and not s.llm_api_key:
        info.append("LLM_ENABLED but LLM_API_KEY empty — copilot/narrator will run in deterministic mode")
    return info, fatal


def run(s: Settings, print_fn=print) -> None:
    info, fatal = check(s)
    print_fn("── preflight ──────────────────────────────────────────────")
    for line in info:
        print_fn("  " + line)
    for line in fatal:
        print_fn("  FATAL: " + line)
    print_fn("───────────────────────────────────────────────────────────")
    if fatal:
        raise SystemExit(2)
