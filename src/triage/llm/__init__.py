"""LLM stages.

Every module in this package obeys three rules:

1. It returns a pydantic-validated object, never free text. There is no channel
   through which model prose becomes a control decision.
2. It never writes to `state/`. It may PROPOSE an observation as a constrained enum;
   deterministic code validates it and does the arithmetic.
   (Enforced by tests/test_separation.py::test_the_llm_package_cannot_write_to_state)
3. It has a deterministic fallback, and when it falls back the report says so.
"""

from .client import CassetteMiss, LLMClient, LLMUnavailable, StageResult

__all__ = ["LLMClient", "StageResult", "CassetteMiss", "LLMUnavailable"]
