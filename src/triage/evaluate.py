"""Planted-conflict eval: the one number in this project that is honestly earned.

We cannot have ground truth for "the right ranking". The brief's premise is that no
such thing exists, and a system that claimed otherwise would be answering an easier
question than the one it was given.

We *can* have ground truth for "was there a contradiction here", because we authored
the contradiction. Twenty-one of them, across three batches, declared in
`planted_conflicts` in each batch file and never shown to the model -- the ingest
layer loads them onto `Batch`, and nothing in `llm/` ever reads that field.

Two things this eval is careful about:

* **Recall is reported at two strictnesses.** A detector that says "TKT-4482: the
  ticket contradicts telemetry" when the planted conflict was "CRM contradicts
  telemetry" has found *a* contradiction on the right ticket but not the one we
  planted. Collapsing those two cases into one percentage flatters the detector.

* **Unmatched detections are not called false positives.** The label set is a lower
  bound: we know 21 contradictions are present, not that only 21 are. A detection we
  did not plant may be a real contradiction we did not think to write down. They are
  reported as `unmatched`, listed in full so a human can classify them, and the
  precision figure is explicitly labelled a floor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .models import Conflict, PlantedConflict

#: What the four sources are called, and everything a detector might call them.
#: The LLM stage is free-text on this field, so it says "billing" or "resolution
#: history" as often as it says "crm". Normalising here rather than constraining the
#: schema keeps the detector's own vocabulary visible in the report.
_SOURCE_ALIASES: dict[str, str] = {
    "ticket": "ticket",
    "message": "ticket",
    "merchant": "ticket",
    "customer": "ticket",
    "ticket message": "ticket",
    "merchant claim": "ticket",
    "stated urgency": "ticket",
    "telemetry": "telemetry",
    "metrics": "telemetry",
    "monitoring": "telemetry",
    "instruments": "telemetry",
    "observability": "telemetry",
    "crm": "crm",
    "billing": "crm",
    "account": "crm",
    "crm/billing": "crm",
    "commercial": "crm",
    "history": "history",
    "resolution history": "history",
    "track record": "history",
    "credibility": "history",
    "customer history": "history",
    "category history": "history",
    "ledger": "ledger",
    "fairness ledger": "ledger",
    "wait time": "ledger",
    "waiting": "ledger",
}


def normalise_source(raw: str) -> str:
    """Map a detector's word for a source onto one of the four (plus the ledger).

    Unrecognised names pass through lowercased rather than being dropped: an
    unmatched detection citing 'astrology' should be visible as exactly that.
    """
    key = raw.strip().lower()
    if key in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[key]
    for alias, canonical in _SOURCE_ALIASES.items():
        if alias in key:
            return canonical
    return key


def _source_set(values: Iterable[str]) -> frozenset[str]:
    return frozenset(normalise_source(v) for v in values)


# --------------------------------------------------------------------- results


@dataclass
class Match:
    """One planted conflict, paired with the detection that found it."""

    planted_id: str
    ticket_id: str
    detection_index: int
    strict: bool
    planted_sources: tuple[str, ...]
    detected_sources: tuple[str, ...]

    @property
    def kind(self) -> str:
        return "exact" if self.strict else "same ticket, different source pair"


@dataclass
class BatchEval:
    batch_id: int
    label: str
    planted: list[PlantedConflict]
    detected: list[Conflict]
    matches: list[Match] = field(default_factory=list)
    missed: list[PlantedConflict] = field(default_factory=list)
    unmatched: list[Conflict] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    @property
    def strict_hits(self) -> int:
        return sum(1 for m in self.matches if m.strict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "label": self.label,
            "planted": len(self.planted),
            "detected": len(self.detected),
            "matched": len(self.matches),
            "matched_exact": self.strict_hits,
            "missed": [{"id": p.id, "ticket_id": p.ticket_id,
                        "sources": p.sources, "description": p.description}
                       for p in self.missed],
            "matches": [{"planted_id": m.planted_id, "ticket_id": m.ticket_id,
                         "kind": m.kind,
                         "planted_sources": list(m.planted_sources),
                         "detected_sources": list(m.detected_sources)}
                        for m in self.matches],
            "unmatched": [{"ticket_id": c.ticket_id,
                           "sources": [c.source_a, c.source_b],
                           "reasoning": c.reasoning} for c in self.unmatched],
            "degraded": self.degraded,
        }


@dataclass
class EvalResult:
    detector: str
    batches: list[BatchEval]

    @property
    def planted(self) -> int:
        return sum(len(b.planted) for b in self.batches)

    @property
    def detected(self) -> int:
        return sum(len(b.detected) for b in self.batches)

    @property
    def matched(self) -> int:
        return sum(len(b.matches) for b in self.batches)

    @property
    def matched_exact(self) -> int:
        return sum(b.strict_hits for b in self.batches)

    @property
    def unmatched(self) -> int:
        return sum(len(b.unmatched) for b in self.batches)

    @property
    def tickets(self) -> int:
        return sum(len({p.ticket_id for p in b.planted} | {c.ticket_id for c in b.detected})
                   for b in self.batches)

    @property
    def recall(self) -> float:
        return self.matched / self.planted if self.planted else 0.0

    @property
    def recall_exact(self) -> float:
        return self.matched_exact / self.planted if self.planted else 0.0

    @property
    def precision_floor(self) -> float:
        """A floor, not a measurement. See the module docstring."""
        return self.matched / self.detected if self.detected else 0.0

    @property
    def degraded(self) -> bool:
        return any(b.degraded for b in self.batches)

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "degraded": self.degraded,
            "planted": self.planted,
            "detected": self.detected,
            "matched": self.matched,
            "matched_exact": self.matched_exact,
            "unmatched": self.unmatched,
            "recall": round(self.recall, 4),
            "recall_exact": round(self.recall_exact, 4),
            "precision_floor": round(self.precision_floor, 4),
            "batches": [b.as_dict() for b in self.batches],
        }


# --------------------------------------------------------------------- matching


def match_batch(
    planted: list[PlantedConflict],
    detected: list[Conflict],
    batch_id: int,
    label: str,
    degraded: list[str] | None = None,
) -> BatchEval:
    """One-to-one assignment, exact source pairs first.

    Greedy in two passes rather than optimal: with at most a dozen conflicts per
    batch the optimal assignment and the greedy one are the same, and the greedy one
    is legible enough to argue with. One detection can satisfy at most one planted
    conflict, so a detector cannot score 21/21 by emitting one vague conflict per
    ticket.
    """
    used: set[int] = set()
    matches: list[Match] = []
    remaining: list[PlantedConflict] = []

    for strict in (True, False):
        pool = planted if strict else remaining
        still_missing: list[PlantedConflict] = []

        for p in pool:
            p_sources = _source_set(p.sources)
            hit = None
            for i, c in enumerate(detected):
                if i in used or c.ticket_id != p.ticket_id:
                    continue
                d_sources = _source_set([c.source_a, c.source_b])
                if (d_sources == p_sources) if strict else bool(d_sources & p_sources):
                    hit = (i, d_sources)
                    break
            if hit is None:
                still_missing.append(p)
                continue
            i, d_sources = hit
            used.add(i)
            matches.append(
                Match(
                    planted_id=p.id,
                    ticket_id=p.ticket_id,
                    detection_index=i,
                    strict=strict,
                    planted_sources=tuple(sorted(p_sources)),
                    detected_sources=tuple(sorted(d_sources)),
                )
            )
        remaining = still_missing

    return BatchEval(
        batch_id=batch_id,
        label=label,
        planted=planted,
        detected=detected,
        matches=sorted(matches, key=lambda m: m.planted_id),
        missed=remaining,
        unmatched=[c for i, c in enumerate(detected) if i not in used],
        degraded=degraded or [],
    )


# ------------------------------------------------------------------- the run


def evaluate(
    batch_paths: list[Path] | None = None,
    use_llm: bool = False,
) -> EvalResult:
    """Run conflict detection over every batch and score it against the labels.

    Each batch is evaluated against **pristine seeded state**, not against the state
    left behind by the previous batch. Detection quality is the thing being measured;
    threading run-order effects through it would mean a regression in batch 1 could
    only be diagnosed by reading batch 3.
    """
    from .llm.client import LLMClient
    from .llm import stages as llm_stages
    from .pipeline import Context, detect_conflicts_deterministic

    batch_paths = batch_paths or sorted(paths.eval_dir().glob("batch_*.json"))

    client = LLMClient()
    if not use_llm:
        client.api_key = None

    detector = "llm (live)" if use_llm else "deterministic"
    out: list[BatchEval] = []

    for path in batch_paths:
        ctx = Context.load(path)
        degraded: list[str] = []

        extraction = llm_stages.extract_urgency(client, ctx.batch.tickets, ctx.charter)
        if extraction.degraded and extraction.reason:
            degraded.append(extraction.reason)
        estimates = ctx.estimates(extraction.value["intensities"])

        result = llm_stages.detect_conflicts(
            client, ctx.batch.tickets, estimates, ctx.charter,
            detect_conflicts_deterministic(estimates),
        )
        if result.degraded and result.reason:
            degraded.append(result.reason)

        out.append(
            match_batch(
                planted=list(ctx.batch.planted_conflicts),
                detected=list(result.value),
                batch_id=ctx.batch.batch_id,
                label=ctx.batch.label,
                degraded=degraded,
            )
        )

    return EvalResult(detector=detector, batches=out)


# ------------------------------------------------------------------ rendering


def render(result: EvalResult) -> str:
    lines: list[str] = []
    w = lines.append

    w(f"Planted-conflict eval - detector: {result.detector}")
    if result.degraded:
        w("")
        w("  ! DEGRADED. At least one stage fell back to the deterministic path.")
        w("    These numbers describe the fallback, not the full pipeline.")
    w("")
    w(f"  recall            {result.matched}/{result.planted}"
      f"  ({result.recall:.0%})   planted conflicts found")
    w(f"    of which exact  {result.matched_exact}/{result.planted}"
      f"  ({result.recall_exact:.0%})   same ticket AND same source pair")
    w(f"  unmatched         {result.unmatched} detections across {result.detected} total")
    w(f"  precision floor   {result.precision_floor:.0%}"
      "   (a floor: the label set is a lower bound, see evaluate.py)")
    w("")

    for b in result.batches:
        w(f"  batch {b.batch_id}: {len(b.matches)}/{len(b.planted)} found, "
          f"{len(b.unmatched)} unmatched")
        for m in b.matches:
            if not m.strict:
                w(f"      ~ {m.planted_id} {m.ticket_id}: planted "
                  f"{'+'.join(m.planted_sources)}, detected {'+'.join(m.detected_sources)}")
        for p in b.missed:
            w(f"      MISS {p.id} {p.ticket_id} ({'+'.join(p.sources)}): {p.description}")
        for c in b.unmatched:
            w(f"      EXTRA {c.ticket_id} ({c.source_a} vs {c.source_b}): {c.reasoning[:90]}")
    w("")
    w("  Unmatched detections are candidate false positives, not confirmed ones: we")
    w("  know 21 contradictions are present, not that only 21 are.")
    return "\n".join(lines)


def write(result: EvalResult, path: Path | None = None) -> Path:
    p = path or paths.reports_dir() / "eval_conflicts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(result.as_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p
