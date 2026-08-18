"""Posture audit: what did the chosen posture actually DO, across batches?

Noticing a single unfair ranking is decent. Noticing a POLICY DRIFTING toward
unfairness, and warning before it breaches, is the thing worth building.

Reads `reports/decision_log.jsonl`, which is append-only and written by the pipeline.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import paths
from .config import load_charter


class AuditRow:
    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.waits: list[float] = []
        self.served = 0
        self.deferred = 0

    @property
    def median_wait(self) -> float:
        return statistics.median(self.waits) if self.waits else 0.0

    @property
    def worst_wait(self) -> float:
        return max(self.waits) if self.waits else 0.0

    @property
    def service_rate(self) -> float:
        total = self.served + self.deferred
        return self.served / total if total else 0.0


def load_decisions(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or paths.decision_log_path()
    if not p.exists():
        return []
    rows = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def audit(decisions: list[dict[str, Any]], posture: str | None = None) -> dict[str, Any]:
    """Aggregate by tier under one posture (or all of them)."""
    rows = [d for d in decisions if posture is None or d["posture"] == posture]
    if not rows:
        return {"posture": posture, "batches": 0, "tiers": {}, "warnings": []}

    by_tier: dict[str, AuditRow] = defaultdict(lambda: AuditRow(""))
    for d in rows:
        tier = d["estimate"]["tier"]
        row = by_tier.setdefault(tier, AuditRow(tier))
        row.tier = tier
        row.waits.append(float(d["estimate"]["waiting_hours"]))
        if d["served"]:
            row.served += 1
        else:
            row.deferred += 1

    ceiling = _waiting_ceiling()
    worst = max((r.worst_wait for r in by_tier.values()), default=0.0)

    warnings: list[str] = []
    if ceiling and worst >= ceiling:
        # Already over. Saying "drifting toward a breach" here would be softer than
        # the facts, and an audit that rounds its worst finding downward is worse
        # than no audit.
        warnings.append(
            f"BREACHED. Worst observed wait is {worst / 24:.1f} days against a charter "
            f"ceiling of {ceiling / 24:.0f} days. The charter promotes on the batch AFTER "
            "a ticket crosses the line, because promotion happens at batch boundaries and "
            "the ticket crossed between them. The rule caught it; it did not prevent it. "
            "Preventing it means promoting on projected wait at the NEXT boundary, or "
            "processing arrivals as a stream."
        )
    elif ceiling and worst >= ceiling * 0.8:
        warnings.append(
            f"Worst observed wait is {worst / 24:.1f} days against a charter ceiling of "
            f"{ceiling / 24:.0f} days. This policy is drifting toward a breach, not sitting "
            "safely inside the rule."
        )

    tiers = sorted(by_tier.values(), key=lambda r: -r.median_wait)
    if len(tiers) >= 2 and tiers[-1].median_wait > 0:
        spread = tiers[0].median_wait / max(tiers[-1].median_wait, 1e-9)
        if spread >= 5:
            warnings.append(
                f"{tiers[0].tier} tier waits {spread:.0f}x longer than {tiers[-1].tier} tier "
                "at the median. That may be exactly what the posture intends -- it is stated "
                "here so it is a decision rather than a side effect."
            )

    starved = [r.tier for r in by_tier.values() if r.service_rate == 0.0 and (r.served + r.deferred) >= 3]
    for tier in starved:
        warnings.append(f"{tier} tier has not been served once in this window.")

    return {
        "posture": posture,
        "batches": len({d["batch_id"] for d in rows}),
        "decisions": len(rows),
        "tiers": {
            r.tier: {
                "median_wait_hours": round(r.median_wait, 1),
                "worst_wait_hours": round(r.worst_wait, 1),
                "served": r.served,
                "deferred": r.deferred,
                "service_rate": round(r.service_rate, 2),
            }
            for r in sorted(by_tier.values(), key=lambda x: x.tier)
        },
        "warnings": warnings,
        "charter_ceiling_hours": ceiling,
    }


def render(result: dict[str, Any]) -> str:
    if not result["batches"]:
        return "No decisions recorded yet. Run `triage run` on a batch first."

    lines = [
        f"{result['posture'] or 'all postures'}, {result['batches']} batches, "
        f"{result['decisions']} decisions",
        "",
        f"  {'tier':<12}{'median wait':>13}{'worst':>11}{'served':>9}{'deferred':>10}",
    ]
    for tier, stats in result["tiers"].items():
        lines.append(
            f"  {tier:<12}{stats['median_wait_hours']:>11.1f}h"
            f"{stats['worst_wait_hours']:>10.1f}h"
            f"{stats['served']:>9}{stats['deferred']:>10}"
        )
    ceiling = result.get("charter_ceiling_hours")
    if ceiling:
        lines.append("")
        lines.append(f"  charter ceiling: {ceiling / 24:.0f} days ({ceiling:.0f}h)")
    if result["warnings"]:
        lines.append("")
        for warn in result["warnings"]:
            lines.append(f"  WARNING  {warn}")
    return "\n".join(lines)


def _waiting_ceiling() -> float:
    for rule in load_charter().get("rules", []):
        for clause in rule.get("clauses", []):
            if "trigger_waiting_hours_gte" in clause:
                return float(clause["trigger_waiting_hours_gte"])
    return 0.0
