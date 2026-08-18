"""The LLM client: forced-schema structured output, temperature 0, cassettes.

Design choices worth defending:

* **Forced tool use, not JSON-in-prose.** `tool_choice` pins the model to a single
  tool whose input schema is generated from a pydantic model. The model cannot reply
  with anything but a conforming object, so parsing is not a failure mode and injected
  prose has no channel to become a decision.

* **Temperature 0.** Not because it makes the model deterministic -- it does not,
  quite -- but because the residual variation is then small enough to measure, which
  is what the stability numbers in the README report.

* **Prompt caching on the system block.** The system prompt carries the charter, the
  posture rationales and the output contract, and it is identical across the four
  critic calls in a batch. Caching it is most of the cost of a run.

* **Cassettes.** Recorded responses give stable CI, a free test suite, and the
  reviewer's zero-setup path. One artefact, three jobs.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .. import paths

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-sonnet-5"
MAX_ATTEMPTS = 3


class LLMUnavailable(RuntimeError):
    """Raised after retries, or when no key is configured. Callers degrade, not crash."""


class CassetteMiss(LLMUnavailable):
    """Replay mode was asked for a call that was never recorded."""


@dataclass
class StageResult:
    """What a stage produced, and whether it was the real thing.

    `degraded` is carried all the way to the report. A run that silently used the
    fallback and looked identical to a full run would be the single most dishonest
    thing this system could do.
    """

    value: Any
    degraded: bool = False
    reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def ok(cls, value: Any, usage: dict[str, int] | None = None) -> "StageResult":
        return cls(value=value, degraded=False, usage=usage or {})

    @classmethod
    def fallback(cls, value: Any, reason: str) -> "StageResult":
        return cls(value=value, degraded=True, reason=reason)


class LLMClient:
    def __init__(
        self,
        model: str | None = None,
        cassette_mode: str | None = None,
        cassette_dir: Path | None = None,
        api_key: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("TRIAGE_MODEL", DEFAULT_MODEL)
        self.provider = provider or os.environ.get("TRIAGE_PROVIDER", "anthropic")
        self.cassette_mode = cassette_mode or os.environ.get("TRIAGE_CASSETTE_MODE", "off")
        self.cassette_dir = cassette_dir or paths.cassette_dir()
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.calls: list[dict[str, Any]] = []
        self._client = None

    # ------------------------------------------------------------------ properties

    @property
    def replaying(self) -> bool:
        return self.cassette_mode == "replay"

    @property
    def available(self) -> bool:
        """Can this client serve a call at all?"""
        if self.replaying:
            return self.cassette_dir.exists()
        return bool(self.api_key)

    # ------------------------------------------------------------------- main call

    def structured(
        self,
        *,
        stage: str,
        system: str,
        user: str,
        schema: type[T],
        tool_name: str,
        tool_description: str,
        max_tokens: int = 4096,
    ) -> T:
        """One call, one validated object. Raises LLMUnavailable; never returns junk."""
        key = self._cassette_key(stage, system, user, tool_name)

        if self.replaying:
            payload = self._read_cassette(stage, key)
            if payload is None:
                raise CassetteMiss(f"no cassette for stage {stage!r} (key {key[:12]})")
            return self._validate(schema, payload, stage)

        if not self.api_key:
            raise LLMUnavailable(
                "ANTHROPIC_API_KEY is not set. Use --replay for the no-key path."
            )

        payload, usage = self._call_api(
            system=system, user=user, schema=schema,
            tool_name=tool_name, tool_description=tool_description,
            max_tokens=max_tokens,
        )
        self.calls.append({"stage": stage, "usage": usage})

        if self.cassette_mode == "record":
            self._write_cassette(stage, key, payload)

        return self._validate(schema, payload, stage)

    # ------------------------------------------------------------------- internals

    def _anthropic(self):
        if self._client is None:
            if self.provider != "anthropic":
                raise LLMUnavailable(
                    f"provider {self.provider!r} is not implemented; the seam exists, "
                    "the second provider does not"
                )
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _call_api(
        self, *, system: str, user: str, schema: type[BaseModel],
        tool_name: str, tool_description: str, max_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        import anthropic

        client = self._anthropic()
        last: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=0,
                    system=[
                        {
                            "type": "text",
                            "text": system,
                            # The system block is identical across the four critic
                            # calls in a batch; caching it is most of the cost.
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
                    tools=[
                        {
                            "name": tool_name,
                            "description": tool_description,
                            "input_schema": _json_schema(schema),
                        }
                    ],
                    # Forced: the model's only legal move is to emit a conforming object.
                    tool_choice={"type": "tool", "name": tool_name},
                )
            except (anthropic.APIStatusError, anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                last = exc
                if attempt == MAX_ATTEMPTS - 1:
                    break
                time.sleep((2 ** attempt) + random.random())
                continue

            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    usage = {
                        "input_tokens": getattr(resp.usage, "input_tokens", 0),
                        "output_tokens": getattr(resp.usage, "output_tokens", 0),
                        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                    }
                    return dict(block.input), usage

            last = LLMUnavailable("model returned no tool_use block despite forced tool choice")
            break

        raise LLMUnavailable(f"{type(last).__name__ if last else 'unknown'}: {last}")

    def _validate(self, schema: type[T], payload: dict[str, Any], stage: str) -> T:
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            # A schema violation from a forced tool call is rare and worth surfacing
            # loudly rather than papering over -- the caller degrades to the
            # deterministic fallback and the report says which stage failed.
            raise LLMUnavailable(f"stage {stage!r} returned a non-conforming object: {exc}") from exc

    # -------------------------------------------------------------------- cassettes

    def _cassette_key(self, stage: str, system: str, user: str, tool_name: str) -> str:
        h = hashlib.sha256()
        for part in (self.model, stage, tool_name, system, user):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def _cassette_path(self, stage: str, key: str) -> Path:
        return self.cassette_dir / stage / f"{key[:32]}.json"

    def _read_cassette(self, stage: str, key: str) -> dict[str, Any] | None:
        path = self._cassette_path(stage, key)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)["response"]

    def _write_cassette(self, stage: str, key: str, payload: dict[str, Any]) -> None:
        path = self._cassette_path(stage, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump({"stage": stage, "model": self.model, "response": payload},
                      fh, indent=2, sort_keys=True)
            fh.write("\n")


def _json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic -> JSON Schema, with the noise the API does not need removed."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema
