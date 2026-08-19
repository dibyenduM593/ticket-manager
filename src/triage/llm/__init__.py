"""LLM stages.

Every module in this package obeys three rules:

1. It returns a pydantic-validated object, never free text. There is no channel
   through which model prose becomes a control decision.
2. It never writes to `state/`. It may PROPOSE an observation as a constrained enum;
   deterministic code validates it and does the arithmetic.
3. When it cannot reach the model it either returns a value COMPUTED FROM THE INPUT
   (the urgency heuristic, signature clustering, the rule-based conflict detector) or
   it returns nothing. It never returns prose written here and presented as the
   system's own reasoning, and the report always says which stages did not run.
"""

from .client import LLMClient, LLMUnavailable, StageResult

__all__ = ["LLMClient", "StageResult", "LLMUnavailable"]
