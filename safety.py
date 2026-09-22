"""Non-negotiable policy checks. Model output cannot override these rules."""
from dataclasses import dataclass
import re

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    escalate: bool = False

RULES = {
    "graded_work": re.compile(r"\b(submit|complete|answer|do)\b.{0,80}\b(homework|assignment|quiz|exam|canvas|vhl|graded)\b", re.I | re.S),
    "money": re.compile(r"\b(buy|purchase|pay|checkout|subscribe|spend|book|order)\b", re.I),
    "impersonation": re.compile(r"\b(as royce|pretend (?:to be|you are) royce|from royce)\b", re.I),
    "account": re.compile(r"\b(change|reset|remove|add|update)\b.{0,50}\b(password|account|security|billing|settings|permission)\b", re.I | re.S),
}

def inspect(text: str) -> Decision:
    if RULES["graded_work"].search(text):
        return Decision(False, "Hermes never completes or submits graded schoolwork. It may tutor, explain, or review Royce's own attempt.", True)
    if RULES["money"].search(text):
        return Decision(False, "Hermes cannot spend money or credits.", True)
    if RULES["impersonation"].search(text):
        return Decision(False, "Hermes never impersonates Royce.", True)
    if RULES["account"].search(text):
        return Decision(False, "Hermes cannot change account settings or credentials.", True)
    return Decision(True, "allowed")
