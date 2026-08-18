"""Ingest and sanitise.

Every input to this system is written by someone with a direct incentive to be ranked
first. Ticket text reaching an LLM prompt is therefore an attack surface, not a
theoretical one.

Four defences, of which this file is the second:

1. Ticket content never enters the system prompt. It arrives only inside delimited
   untrusted blocks -- see llm/prompts.py.
2. Sanitisation at ingest strips role markers and instruction-shaped preambles,
   LOGGING what it stripped rather than silently dropping it.  <-- this file
3. Structured output: the model returns a ranking object, not free text, so there is
   no channel through which injected prose becomes a control decision.
4. The scorer is deterministic and never sees raw text, so the final ordering cannot
   be moved by language alone.

The framing worth keeping: the credibility system already assumes customers
exaggerate. Prompt injection is exaggeration with a different syntax, and it gets the
same treatment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Batch, Ticket

#: Instruction-shaped patterns. Each is (regex, human label).
#: We rewrite rather than delete so the report can show what was in the message.
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*(system|assistant|human|user)\s*:", re.IGNORECASE | re.MULTILINE),
     "role marker at line start"),
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?", re.IGNORECASE),
     "instruction-override preamble"),
    (re.compile(r"disregard\s+(all\s+)?(previous|prior|above|the)\s+\w+", re.IGNORECASE),
     "instruction-override preamble"),
    (re.compile(r"</?\s*(system|instructions?|prompt|ticket)\s*>", re.IGNORECASE),
     "pseudo-XML control tag"),
    (re.compile(r"\[/?\s*(INST|SYS|SYSTEM)\s*\]", re.IGNORECASE),
     "chat-template control token"),
    (re.compile(r"you\s+(are|must|should)\s+(now\s+)?(a|an|the)?\s*\w*\s*(assistant|agent|model|ai)\b", re.IGNORECASE),
     "role reassignment"),
    (re.compile(r"\bmust\s+be\s+ranked\s+(first|#?1|top)\b", re.IGNORECASE),
     "direct ranking instruction"),
    (re.compile(r"\bset\s+(the\s+)?priority\s+to\b", re.IGNORECASE),
     "direct priority instruction"),
    (re.compile(r"\b(new|updated|override)\s+(system\s+)?(instructions?|directive|policy)\b", re.IGNORECASE),
     "claimed policy override"),
]

_REDACTION = "[redacted: {label}]"


def sanitise(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, notes). Notes name what was removed and are surfaced in
    the report -- an injection attempt is evidence about the claimant, so throwing it
    away silently would discard a signal."""
    notes: list[str] = []
    clean = text
    for pattern, label in _INJECTION_PATTERNS:
        clean, n = pattern.subn(_REDACTION.format(label=label), clean)
        if n:
            notes.append(f"{n}x {label}")
    # Collapse absurd shouting runs; they still register in urgency_intensity, but a
    # 400-char scream should not dominate the token budget.
    clean = re.sub(r"(!{4,})", "!!!", clean)
    clean = re.sub(r"\n{4,}", "\n\n", clean)
    return clean, notes


def load_batch(path: Path | str) -> Batch:
    """Load a batch and sanitise every body on the way in.

    Sanitisation happens exactly once, here, at the boundary. Nothing downstream ever
    sees the raw text -- including the report renderer, which shows the sanitised
    body and the notes side by side.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    tickets: list[Ticket] = []
    for raw in payload["tickets"]:
        clean_body, body_notes = sanitise(raw["body"])
        clean_subject, subject_notes = sanitise(raw["subject"])
        notes = [f"body: {n}" for n in body_notes] + [f"subject: {n}" for n in subject_notes]
        tickets.append(
            Ticket.model_validate(
                {**raw, "body": clean_body, "subject": clean_subject, "sanitisation_notes": notes}
            )
        )

    return Batch(
        batch_id=payload["batch_id"],
        label=payload["label"],
        tickets=tickets,
        planted_conflicts=payload.get("planted_conflicts", []),
        as_of=payload.get("as_of"),
    )


def sanitisation_events(batch: Batch) -> list[dict[str, object]]:
    return [
        {"ticket_id": t.id, "customer_id": t.customer_id, "stripped": t.sanitisation_notes}
        for t in batch.tickets
        if t.sanitisation_notes
    ]
