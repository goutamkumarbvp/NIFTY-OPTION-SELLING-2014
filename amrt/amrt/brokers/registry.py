"""BrokerManager: builds broker sessions (credentials), read-only facades and — only in a
LIVE_CAPABLE deployment — live order venues for the Execution Gateway.

Constructed under the Execution Gateway's principal (HOLD_BROKER_CREDENTIALS).
Agents, the API layer and the reliability plane never receive a session.
"""
from __future__ import annotations

import logging

from amrt.brokers.angel import PROFILE as ANGEL
from amrt.brokers.angel import AngelSession
from amrt.brokers.base import BrokerSession, LiveBrokerVenue, MarketDataReader
from amrt.brokers.groww import PROFILE as GROWW
from amrt.brokers.groww import GrowwSession
from amrt.brokers.kotak import PROFILE as KOTAK
from amrt.brokers.kotak import KotakSession
from amrt.brokers.upstox import PROFILE as UPSTOX
from amrt.brokers.upstox import UpstoxSession
from amrt.brokers.zerodha import PROFILE as ZERODHA
from amrt.brokers.zerodha import ZerodhaSession
from amrt.core.errors import PaperIsolationViolation
from amrt.security.identity import Capability, Principal, require

log = logging.getLogger("amrt.brokers")

SESSIONS: dict[str, type[BrokerSession]] = {"kotak": KotakSession, "zerodha": ZerodhaSession, "angel": AngelSession, "upstox": UpstoxSession, "groww": GrowwSession}
PROFILES = {"kotak": KOTAK, "zerodha": ZERODHA, "angel": ANGEL, "upstox": UPSTOX, "groww": GROWW}


def capability_matrix() -> list[dict]:
    return [p.model_dump(mode="json") for p in PROFILES.values()]


class BrokerManager:
    def __init__(self, principal: Principal, settings, clock, sdk_factories: dict | None = None) -> None:
        require(principal, Capability.HOLD_BROKER_CREDENTIALS, "construct broker manager")
        self.principal = principal
        self.settings = settings
        self.clock = clock
        self.sessions: dict[str, BrokerSession] = {}
        for name, cls in SESSIONS.items():
            sess = cls(settings, clock, sdk_factory=(sdk_factories or {}).get(name))
            if sess.credentials_present():
                self.sessions[name] = sess

    def reader(self, name: str) -> MarketDataReader | None:
        s = self.sessions.get(name)
        if s is None:
            return None
        r = MarketDataReader(s)
        r.bind_auth(lambda: s.authenticated)
        return r

    def account_id(self, name: str) -> str:
        return f"{name.upper()}-LIVE"

    def live_venue(self, name: str, gateway_verifier) -> LiveBrokerVenue:
        if not self.settings.live_capable:
            raise PaperIsolationViolation("live venues are only built in a LIVE_CAPABLE deployment with live orders enabled")
        return LiveBrokerVenue(self.principal, self.sessions[name], self.account_id(name), gateway_verifier, self.clock)

    def status(self) -> list[dict]:
        out = []
        for name, prof in PROFILES.items():
            s = self.sessions.get(name)
            out.append({"broker": name, "display": prof.display, "configured": s is not None, "overall_status": prof.overall_status.value,
                        **(s.status() if s else {"authenticated": False, "credentials_present": False})})
        return out
