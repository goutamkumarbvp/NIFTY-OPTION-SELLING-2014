"""Telegram command bot: operate the terminal from a phone.

Long-polls getUpdates when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set and
TELEGRAM_COMMANDS_ENABLED is true. Only messages from the configured chat id
are honoured. Commands: /status /pnl /positions /runs /pause /resume /kill
/gate open|close /mode manual|auto /help
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict

import httpx

from terminal.core.models import TerminalMode

log = logging.getLogger("terminal.telegram")


class TelegramCommands:
    def __init__(self, terminal) -> None:
        self.t = terminal
        s = terminal.settings
        self.token = s.telegram_bot_token
        self.chat_id = str(s.telegram_chat_id)
        self.enabled = bool(self.token and self.chat_id and s.telegram_commands_enabled)
        self.offset: int | None = None
        self.handled = 0
        self.rejected = 0
        self.last_error = ""
        self.last_ts = 0.0

    def status(self) -> dict:
        return {"enabled": self.enabled, "handled": self.handled, "rejected": self.rejected, "last_error": self.last_error, "last_ts": self.last_ts}

    async def run(self) -> None:
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        async with httpx.AsyncClient(timeout=35) as client:
            while not self.t._stop.is_set():
                try:
                    params: Dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
                    if self.offset is not None:
                        params["offset"] = self.offset
                    r = await client.get(url, params=params)
                    r.raise_for_status()
                    for upd in r.json().get("result", []):
                        self.offset = int(upd["update_id"]) + 1
                        msg = upd.get("message") or {}
                        text = str(msg.get("text") or "").strip()
                        chat = str((msg.get("chat") or {}).get("id") or "")
                        if not text:
                            continue
                        if chat != self.chat_id:
                            self.rejected += 1
                            continue
                        reply = await self.handle(text, actor=f"telegram:{(msg.get('from') or {}).get('username') or chat}")
                        await client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": reply})
                        self.handled += 1
                        self.last_ts = time.time()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                    await asyncio.sleep(10)

    async def handle(self, text: str, actor: str = "telegram") -> str:
        """Pure command dispatcher (unit-tested without network)."""
        t = self.t
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].lower() if len(parts) > 1 else ""
        r = t.risk.snapshot
        if cmd in ("/status", "/start"):
            return (f"{t.mode.value}/{t.env.value} · risk {r.level.value} · gate {'OPEN' if r.safety_gate_open else 'closed'} · kill {'ON' if r.kill_switch else 'off'} · "
                    f"{'PAUSED' if t.paused else 'running'} · feed {'ok' if t.feed.is_fresh(t.settings.feed_stale_seconds) else 'STALE'}\nP&L ₹{r.daily_pnl:,.0f} · {r.open_lots} lots · margin {r.margin_utilisation_pct}%")
        if cmd == "/pnl":
            p = t.pnl_summary()
            return f"Daily ₹{p['daily']:,.0f} (realised ₹{p['realized']:,.0f}, unrealised ₹{p['unrealized']:,.0f}, charges ₹{p['charges']:,.0f}) · budget used {r.loss_budget_used_pct}%"
        if cmd == "/positions":
            ps = t.positions.open_positions()
            return "No open positions" if not ps else "\n".join(f"{p.symbol} {p.net_qty:+d} @ {p.avg_price} → {p.ltp} ₹{p.unrealized_pnl:,.0f}" for p in ps[:20])
        if cmd == "/runs":
            rs = t.strategies.active_runs()
            return "No active runs" if not rs else "\n".join(f"{x.strategy} {x.underlying} {x.lots}L MTM ₹{x.mtm:,.0f} (SL {x.stop_loss_pct}% / T {x.target_pct}%)" for x in rs)
        if cmd == "/pause":
            t.paused = True
            t.audit.record("ENGINE_PAUSE", {}, actor)
            return "Engine paused (exits still run)."
        if cmd == "/resume":
            if t.risk.kill_switch:
                return "Kill switch is engaged; reset it from the dashboard first."
            t.paused = False
            t.risk.halted_reason = ""
            t.audit.record("ENGINE_RESUME", {}, actor)
            return "Engine resumed."
        if cmd == "/kill":
            if arg != "confirm":
                return "Send '/kill confirm' to flatten everything, close the gate and switch to MANUAL."
            await t.risk.kill(actor)
            return "KILL SWITCH engaged: all positions flattened, gate closed, MANUAL mode."
        if cmd == "/gate":
            if arg not in ("open", "close"):
                return "Usage: /gate open | /gate close"
            t.risk.set_gate(arg == "open", actor)
            return f"Safety gate {'OPEN' if arg == 'open' else 'CLOSED'}."
        if cmd == "/mode":
            if arg not in ("manual", "auto"):
                return "Usage: /mode manual | /mode auto"
            await t.set_mode(TerminalMode(arg.upper()), actor, reason="telegram")
            return f"Mode → {t.mode.value}."
        if cmd == "/heal":
            g = t.guardian
            if arg in ("approve", "reject") and len(parts) > 2:
                inc_id = parts[2]
                try:
                    inc = await g.approve(inc_id, actor) if arg == "approve" else g.reject(inc_id, actor, "telegram")
                except (KeyError, ValueError) as exc:
                    return f"Guardian: {exc}"
                return f"Guardian {inc.symptom}: {inc.status}" + (f" — {inc.result}" if inc.result else "")
            pend = g.pending()
            if not pend:
                return f"Guardian: no remedies awaiting permission · {g.scans} scans · {g.healed} healed · policy {g.auto_apply}"
            return "Guardian remedies awaiting your permission:\n" + "\n".join(f"{i.id} · {i.symptom} → {i.remedy_label} [{i.risk}]\n  /heal approve {i.id}  |  /heal reject {i.id}" for i in pend[:8])
        if cmd == "/ask" and len(parts) > 1:
            res = await t.copilot.chat(" ".join(parts[1:]), actor)
            return res["answer"][:3500]
        return "Commands: /status /pnl /positions /runs /pause /resume /kill confirm /gate open|close /mode manual|auto /heal [approve|reject <id>] /ask <question>"
