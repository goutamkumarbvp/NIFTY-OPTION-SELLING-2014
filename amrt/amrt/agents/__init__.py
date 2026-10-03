"""Advisory agents. Nothing in this package can place, modify or cancel an order."""
from amrt.agents.base import Agent, AgentContext, AgentOutput, ProposedAction, ProposedLeg
from amrt.agents.master import MasterAgent
from amrt.agents.specialists import SPECIALISTS
from amrt.agents.strategy import QuantitativeStrategyAgent
from amrt.agents.verification import IndependentVerificationAgent


def build_master(health=None, precheck=None, timeout_s: float = 2.0) -> MasterAgent:
    return MasterAgent([cls() for cls in SPECIALISTS], QuantitativeStrategyAgent(), IndependentVerificationAgent(), health=health,
                       precheck=precheck, timeout_s=timeout_s)


__all__ = ["Agent", "AgentContext", "AgentOutput", "IndependentVerificationAgent", "MasterAgent", "ProposedAction", "ProposedLeg",
           "QuantitativeStrategyAgent", "SPECIALISTS", "build_master"]
