"""Report rendering: markdown for humans, JSON for tests.

Both are written every run. The JSON exists so `tests/test_contract.py` asserts on
structure rather than prose -- a test that greps a narrative paragraph is a test that
fails whenever the model changes its mind about a comma.

Section order follows the spec:
  1 situation  2 contradictions  3 ranking  4 charter overrides  5 strategy ranking
  6 counterfactual regret  7 what the critics said  8 confidence and escalations
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths
from .models import BatchReport, Ordering

#: Two tickets whose scores differ by less than this are not meaningfully ordered.
#: Reporting them as 3rd and 4th with a straight face would be false precision.
NEAR_TIE = 0.02


def render_markdown(report: BatchReport) -> str:
    out: list[str] = []
    w = out.append

    w(f"# Batch {report.batch_id} — {report.label}")
    w("")
    w(f"*Triaged as of {report.as_of.isoformat()}*")
    w("")

    if report.degraded_stages:
        w("> ### ⚠ Degraded run")
        w("> ")
        w("> Some stages did not run. This ordering is the deterministic core's output "
          "and has not been through the full pipeline:")
        for reason in report.degraded_stages:
            w(f"> - {reason}")
        w("")

    # ---- 1. situation --------------------------------------------------------
    w("## 1. Situation")
    w("")
    w(f"**Company state:** {report.company_state_summary}")
    w("")
    w(f"**Posture in force:** `{report.posture}` — chosen by {report.posture_chosen_by}")
    w("")
    if report.narrative:
        w(report.narrative)
        w("")

    # ---- 2. contradictions ---------------------------------------------------
    w("## 2. Contradictions found")
    w("")
    if not report.conflicts:
        w("_No contradictions detected. All four sources agree on every ticket in this batch._")
    else:
        w(f"{len(report.conflicts)} contradictions across {len({c.ticket_id for c in report.conflicts})} tickets.")
        w("")
        w("| Ticket | Source A says | Source B says | Trusted | Why |")
        w("|---|---|---|---|---|")
        for c in report.conflicts:
            w(
                f"| `{c.ticket_id}` | **{c.source_a}**: {_cell(c.claim_a)} "
                f"| **{c.source_b}**: {_cell(c.claim_b)} | {_cell(c.trusted)} | {_cell(c.reasoning)} |"
            )
    w("")

    if report.sanitisation_events:
        w("### Instruction-shaped content stripped at ingest")
        w("")
        w("A merchant attempting to instruct the triage system is exhibiting the behaviour "
          "the credibility model already exists to price in. It is discounted and reported, "
          "not obeyed and not silently dropped.")
        w("")
        for ev in report.sanitisation_events:
            w(f"- `{ev['ticket_id']}` ({ev['customer_id']}): {', '.join(ev['stripped'])}")
        w("")

    if report.clusters:
        w("### Correlated clusters")
        w("")
        w("Scored as one event rather than as N independent complaints; severity is the "
          "noisy-OR aggregate of the members, not their maximum.")
        w("")
        for cl in report.clusters:
            w(f"- **{', '.join(cl.ticket_ids)}** — {cl.root_cause}")
            for ev in cl.shared_evidence:
                w(f"  - {ev}")
        w("")

    # ---- 3. ranking ----------------------------------------------------------
    w("## 3. Ranking")
    w("")
    w(f"Capacity is **{report.ordering.capacity} agents**, so ranks 1–{report.ordering.capacity} "
      f"are served this batch and the rest accrue fairness debt.")
    w("")
    w("| # | Ticket | Merchant | Score | Outcome | Why |")
    w("|---|---|---|---|---|---|")
    for r in report.ordering.ranked:
        outcome = "**served**" if r.served else "deferred"
        if r.charter_promoted:
            outcome += " ⚖"
        w(f"| {r.rank} | `{r.ticket_id}` | {r.customer_id} | {r.score:.3f} | {outcome} | {_cell(r.justification)} |")
    w("")
    w("⚖ = position set by the charter, not by the score.")
    w("")

    ties = near_ties(report.ordering)
    if ties:
        w("**Near-ties.** These pairs differ by less than "
          f"{NEAR_TIE:.2f} and are not meaningfully ordered by this system:")
        for a, b, delta in ties:
            w(f"- `{a}` and `{b}` (Δ {delta:.4f})")
        w("")

    # ---- 4. charter overrides ------------------------------------------------
    w("## 4. Charter overrides")
    w("")
    if not report.ordering.overrides:
        w("_No charter rule was engaged. The posture's ranking stands unmodified._")
    else:
        for o in report.ordering.overrides:
            w(f"### `{o.ticket_id}` — rank {o.from_rank} → {o.to_rank}  ({o.rule_id}/{o.clause_id}: {o.rule_name})")
            w("")
            w(f"> {o.statement}")
            w("")
            w(f"{o.detail}.")
            w("")
        w("Promotion is minimal by design: a protected ticket moves to the lowest rank "
          "that satisfies the rule, never to the top. The charter is a floor, not a preference.")
    w("")

    if report.ordering.oversubscribed:
        w("> ### ⚠ Charter oversubscribed")
        w("> ")
        w(f"> {len(report.ordering.oversubscribed)} tickets carry charter-protected conditions "
          f"but only {report.ordering.capacity} agents are available: "
          f"{', '.join('`' + t + '`' for t in report.ordering.oversubscribed)}. "
          "The charter cannot invent capacity; a human owns this.")
        w("")

    # ---- 5. strategy ranking -------------------------------------------------
    w("## 5. Strategy ranking")
    w("")
    if report.advice is None:
        w("_No advice stage ran._")
    else:
        a = report.advice
        w(f"**Recommended: `{a.recommended}`** ({'model' if a.source == 'llm' else 'deterministic fallback'})")
        w("")
        w(a.reasoning)
        w("")
        w(f"**What it costs:** {a.what_it_costs}")
        w("")
        if a.charter_collision_warning:
            w(f"> ⚠ **Flagged in advance:** {a.charter_collision_warning}")
            w("")
        if a.ranked_alternatives:
            w("| Rank | Posture | Reasoning | The trade it makes |")
            w("|---|---|---|---|")
            for alt in sorted(a.ranked_alternatives, key=lambda x: x.rank):
                w(f"| {alt.rank} | `{alt.posture}` | {_cell(alt.reasoning)} | {_cell(alt.trade_off)} |")
    w("")

    # ---- 6. regret -----------------------------------------------------------
    w("## 6. Counterfactual regret")
    w("")
    if not report.regret:
        w("_Every posture produced the same ordering for this batch. That is worth noticing: "
          "the evidence is strong enough that the value weights did not matter here._")
    else:
        w("What the rejected postures would have done differently, and what that costs.")
        w("")
        w("| Posture | Ticket | Rank there | Rank here | Cost |")
        w("|---|---|---|---|---|")
        for row in report.regret:
            w(f"| `{row.posture}` | `{row.ticket_id}` | {row.rank_here} | {row.rank_under_chosen} "
              f"| {_cell(row.cost_sentence)} |")
    w("")

    # ---- 7. critics ----------------------------------------------------------
    w("## 7. What the critics said")
    w("")
    if not report.critiques:
        w("_No critique stage ran. This ranking is unreviewed._")
    else:
        for c in report.critiques:
            w(f"### {c.critic.replace('_', ' ').title()} — arguing for `{c.posture_voice}`")
            w("")
            w(c.objection)
            w("")
            if c.specific_tickets:
                w(f"*Turns on:* {', '.join('`' + t + '`' for t in c.specific_tickets)}")
                w("")
            w(f"*Strongest single point:* {c.strongest_point}")
            w("")
            w(f"*What it would cost to agree:* {c.what_it_would_cost_to_agree}")
            w("")

        w("### Adjudication")
        w("")
        adj = report.adjudication
        if adj is None:
            w("_No adjudication stage ran._")
        elif adj.changed_mind:
            w("**The ranking changed in response to critique.**")
            w("")
            w(f"Before: {' → '.join('`' + t + '`' for t in report.ordering_before_adjudication)}")
            w("")
            w(f"After:  {' → '.join('`' + t + '`' for t in report.ordering.ticket_order)}")
            w("")
            for rev in adj.revisions:
                w(f"- `{rev.ticket_id}` rank {rev.from_rank} → {rev.to_rank}, "
                  f"conceding to **{rev.because_of_critic}**: {rev.reasoning}")
            w("")
        else:
            w("**The ranking stands.**")
            w("")
        if adj and adj.defence:
            w(adj.defence)
            w("")
        if adj and adj.conceded_but_not_acted:
            w("*Conceded but not acted on:*")
            for point in adj.conceded_but_not_acted:
                w(f"- {point}")
            w("")

    # ---- 8. confidence and escalations ---------------------------------------
    w("## 8. Confidence and escalations")
    w("")
    if not report.escalations:
        w("_Nothing escalated. Every ticket in this batch was decidable from the evidence available._")
    else:
        w("Tickets this system declines to decide on. Each names what evidence would resolve it, "
          "so it is an escalation rather than a shrug.")
        w("")
        for e in report.escalations:
            w(f"- **`{e.ticket_id}`** — {e.why}")
            w(f"  - *Would resolve it:* {e.what_would_resolve_it}")
    w("")

    return "\n".join(out) + "\n"


def near_ties(ordering: Ordering, threshold: float = NEAR_TIE) -> list[tuple[str, str, float]]:
    """Adjacent pairs too close to call.

    Reporting rank 3 and rank 4 with a straight face when they differ by 0.001 is
    false precision, and it is exactly where a stability measurement will show churn.
    Charter-promoted positions are skipped: those are not score comparisons at all.
    """
    out: list[tuple[str, str, float]] = []
    for a, b in zip(ordering.ranked, ordering.ranked[1:]):
        if a.charter_promoted or b.charter_promoted:
            continue
        delta = abs(a.score - b.score)
        if delta < threshold:
            out.append((a.ticket_id, b.ticket_id, delta))
    return out


def write(report: BatchReport, directory: Path | None = None) -> tuple[Path, Path]:
    d = directory or paths.reports_dir()
    d.mkdir(parents=True, exist_ok=True)
    md_path = d / f"batch_{report.batch_id}.md"
    json_path = d / f"batch_{report.batch_id}.json"

    md_path.write_text(render_markdown(report), encoding="utf-8")
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(report.model_dump(mode="json"), fh, indent=2, sort_keys=True)
        fh.write("\n")
    return md_path, json_path


def _cell(text: str) -> str:
    """Markdown table cells cannot contain raw pipes or newlines."""
    return " ".join(str(text).replace("|", "\\|").split())
