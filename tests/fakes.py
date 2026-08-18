"""A programmable stand-in for the LLM client.

Cassettes prove the pipeline works against responses the model really produced. This
double proves it survives responses the model MIGHT produce -- hallucinated ticket
IDs, duplicated entries, out-of-range ranks, unknown posture names, schema-shaped
nonsense. Those are the cases worth testing, and you cannot record a cassette of a
failure mode you have not yet seen.

Both matter, and they test different things.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from triage.llm.client import LLMUnavailable


class FakeLLMClient:
    """Dispatches on `stage`. Any stage without a handler raises LLMUnavailable,
    which is how a test asserts that the deterministic fallback took over."""

    def __init__(self, handlers: dict[str, Any] | None = None, available: bool = True) -> None:
        self.handlers = handlers or {}
        self._available = available
        self.calls: list[dict[str, Any]] = []
        self.seen: list[tuple[str, str, str]] = []  # (stage, system, user)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def replaying(self) -> bool:
        return False

    def structured(
        self, *, stage: str, system: str, user: str, schema: type[BaseModel],
        tool_name: str, tool_description: str, max_tokens: int = 4096,
    ):
        self.seen.append((stage, system, user))
        if stage not in self.handlers:
            raise LLMUnavailable(f"fake client has no handler for stage {stage!r}")
        handler = self.handlers[stage]
        payload = handler(user) if isinstance(handler, Callable) else handler
        if isinstance(payload, BaseModel):
            return payload
        return schema.model_validate(payload)


def extraction_for(ticket_ids: list[str], intensity: float = 0.5, injection: bool = False) -> dict:
    return {
        "extractions": [
            {
                "ticket_id": t,
                "urgency_intensity": intensity,
                "injection_attempt": injection,
                "understating": False,
            }
            for t in ticket_ids
        ]
    }


def no_clusters() -> dict:
    return {"clusters": [], "reasoning": "no shared signatures"}


def conflicts_for(pairs: list[tuple[str, str, str]]) -> dict:
    """pairs of (ticket_id, source_a, source_b)."""
    return {
        "conflicts": [
            {
                "ticket_id": t,
                "source_a": a,
                "claim_a": "a says x",
                "source_b": b,
                "claim_b": "b says y",
                "trusted": b,
                "reasoning": "because",
                "severity_of_conflict": "medium",
            }
            for t, a, b in pairs
        ],
        "tickets_with_no_conflict": [],
    }


def advice_for(recommended: str, others: list[str]) -> dict:
    return {
        "recommended": recommended,
        "reasoning": "because the situation says so",
        "what_it_costs": "the free tier waits",
        "ranked_alternatives": [
            {"posture": p, "rank": i + 2, "reasoning": "r", "trade_off": "t"}
            for i, p in enumerate(others)
        ],
        "charter_collision_warning": None,
    }


def critique_for(tickets: list[str]) -> dict:
    return {
        "objection": "this ranking is indefensible",
        "specific_tickets": tickets,
        "strongest_point": "the long waiter",
        "what_it_would_cost_to_agree": "an outage stays open",
    }


def adjudication_for(revisions: list[tuple[str, int]], changed: bool = True) -> dict:
    return {
        "changed_mind": changed,
        "revisions": [
            {"ticket_id": t, "to_rank": r, "because_of_critic": "fairness_campaigner",
             "reasoning": "conceded"}
            for t, r in revisions
        ],
        "defence": "the rest stands",
        "conceded_but_not_acted": ["the CFO is right about renewal exposure"],
    }


def narrative() -> dict:
    return {
        "situation": "An incident is open.",
        "headline_decision": "A free-tier exposure was promoted over an enterprise outage.",
        "what_would_change_my_mind": "Telemetry retracting the exposure.",
    }
