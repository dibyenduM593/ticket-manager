"""Stability measurement: run the same batch N times and report what moved.

Temperature 0 is not determinism. It narrows the distribution enough that the residual
variation is *measurable*, which is a different and more useful property. This module
measures it.

What is reported, and why each line is there:

* **Ordering agreement.** How many of N runs produced the identical ranking, and what
  the modal ranking was. The headline number.
* **Per-rank divergence.** Which positions moved. "9/10 identical" hides whether the
  divergence was ranks 4 and 5 or ranks 1 and 2; the first is a footnote and the
  second would invalidate the demo.
* **Upstream variation.** Posture recommended, conflicts found, whether adjudication
  changed its mind. The ordering can be stable while the reasoning that produced it
  is not, and reporting only the ordering would hide that.

Runs never persist state. Ten runs that each accrued fairness debt would be measuring
the ledger's drift, not the pipeline's stability.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class RunSample:
    """One run's observable outputs."""

    ordering: tuple[str, ...]
    served: tuple[str, ...]
    posture: str
    conflicts: int
    clusters: int
    changed_mind: bool
    overrides: tuple[str, ...]
    degraded: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordering": list(self.ordering),
            "served": list(self.served),
            "posture": self.posture,
            "conflicts": self.conflicts,
            "clusters": self.clusters,
            "changed_mind": self.changed_mind,
            "overrides": list(self.overrides),
            "degraded": list(self.degraded),
        }


@dataclass
class StabilityResult:
    batch_id: int
    path: str
    runs: int
    mode: str
    samples: list[RunSample] = field(default_factory=list)

    # ------------------------------------------------------------- ordering

    @property
    def orderings(self) -> Counter:
        return Counter(s.ordering for s in self.samples)

    @property
    def modal_ordering(self) -> tuple[str, ...]:
        return self.orderings.most_common(1)[0][0] if self.samples else ()

    @property
    def modal_count(self) -> int:
        return self.orderings.most_common(1)[0][1] if self.samples else 0

    @property
    def distinct(self) -> int:
        return len(self.orderings)

    @property
    def unstable_ranks(self) -> list[int]:
        """1-indexed positions that were not the same ticket in every run."""
        if not self.samples:
            return []
        depth = min(len(s.ordering) for s in self.samples)
        return [
            i + 1
            for i in range(depth)
            if len({s.ordering[i] for s in self.samples}) > 1
        ]

    @property
    def stable_prefix(self) -> int:
        """How many leading ranks were identical in every run."""
        unstable = self.unstable_ranks
        return (unstable[0] - 1) if unstable else len(self.modal_ordering)

    @property
    def served_stable(self) -> bool:
        """The set that was actually worked on. A swap inside the served set is a
        cosmetic difference; a swap across the capacity line is a real one."""
        return len({frozenset(s.served) for s in self.samples}) == 1

    # -------------------------------------------------------------- upstream

    def _spread(self, key) -> Counter:
        return Counter(key(s) for s in self.samples)

    @property
    def degraded(self) -> bool:
        return any(s.degraded for s in self.samples)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch": self.path,
            "runs": self.runs,
            "mode": self.mode,
            "degraded": self.degraded,
            "identical_runs": self.modal_count,
            "distinct_orderings": self.distinct,
            "modal_ordering": list(self.modal_ordering),
            "unstable_ranks": self.unstable_ranks,
            "stable_prefix": self.stable_prefix,
            "served_set_stable": self.served_stable,
            "posture_spread": dict(self._spread(lambda s: s.posture)),
            "conflict_count_spread": {str(k): v for k, v in self._spread(lambda s: s.conflicts).items()},
            "changed_mind_spread": {str(k): v for k, v in self._spread(lambda s: s.changed_mind).items()},
            "samples": [s.as_dict() for s in self.samples],
        }


# ------------------------------------------------------------------ the run


def measure(
    batch_path: Path | str | None = None,
    runs: int = 10,
    posture: str | None = None,
    use_llm: bool = True,
) -> StabilityResult:
    """Run one batch `runs` times and collect what varied.

    `persist_state=False` on every run: this measures the pipeline, not the ledger.
    """
    from .pipeline import Context, RunOptions, run_batch

    path = Path(batch_path) if batch_path else paths.eval_dir() / "batch_1.json"
    mode = "live" if use_llm else "deterministic core only"

    samples: list[RunSample] = []
    batch_id = 0

    for _ in range(runs):
        # A fresh Context per run: state is re-read from disk, so run 7 does not
        # inherit run 6's in-memory ledger mutations.
        ctx = Context.load(path)
        batch_id = ctx.batch.batch_id
        report = run_batch(
            ctx,
            RunOptions(
                posture=posture,
                use_llm=use_llm,
                assume_yes=True,
                persist_state=False,
            ),
        )
        samples.append(
            RunSample(
                ordering=tuple(report.ordering.ticket_order),
                served=tuple(r.ticket_id for r in report.ordering.ranked if r.served),
                posture=report.posture,
                conflicts=len(report.conflicts),
                clusters=len(report.clusters),
                changed_mind=bool(report.adjudication and report.adjudication.changed_mind),
                overrides=tuple(sorted(o.ticket_id for o in report.ordering.overrides)),
                degraded=tuple(report.degraded_stages),
            )
        )

    return StabilityResult(
        batch_id=batch_id, path=str(path), runs=runs, mode=mode, samples=samples
    )


# ----------------------------------------------------------------- rendering


def render(result: StabilityResult) -> str:
    lines: list[str] = []
    w = lines.append

    w(f"Stability - batch {result.batch_id}, {result.runs} runs, mode: {result.mode}")
    if result.degraded:
        w("")
        w("  ! DEGRADED. At least one LLM stage fell back. With the deterministic core")
        w("    alone the ordering is stable by construction, so a 10/10 here says")
        w("    nothing about the full pipeline.")
    w("")
    w(f"  identical orderings   {result.modal_count}/{result.runs}"
      f"   ({result.distinct} distinct ordering{'s' if result.distinct != 1 else ''})")
    w(f"  stable prefix         ranks 1-{result.stable_prefix} identical in every run"
      if result.stable_prefix else "  stable prefix         none - rank 1 moved")
    w(f"  served set            {'identical' if result.served_stable else 'VARIED across runs'}")

    if result.unstable_ranks:
        w(f"  ranks that moved      {', '.join(str(r) for r in result.unstable_ranks)}")
        for rank in result.unstable_ranks:
            seen = Counter(s.ordering[rank - 1] for s in result.samples)
            detail = ", ".join(f"{tid} x{n}" for tid, n in seen.most_common())
            w(f"      rank {rank}: {detail}")
    else:
        w("  ranks that moved      none")

    w("")
    w("  upstream of the ordering:")
    for label, spread in (
        ("posture recommended", result._spread(lambda s: s.posture)),
        ("conflicts found", result._spread(lambda s: s.conflicts)),
        ("adjudicator changed its mind", result._spread(lambda s: s.changed_mind)),
    ):
        values = ", ".join(f"{k} x{n}" for k, n in spread.most_common())
        flag = "" if len(spread) == 1 else "   <- varied"
        w(f"      {label:<30} {values}{flag}")

    w("")
    w("  modal ordering: " + " > ".join(result.modal_ordering))
    return "\n".join(lines)


def write(result: StabilityResult, path: Path | None = None) -> Path:
    p = path or paths.reports_dir() / f"stability_batch_{result.batch_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(result.as_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p
