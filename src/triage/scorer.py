"""The crossing point.

Estimation and valuation meet here and nowhere else. Everything above this line in
the call graph is either purely factual or purely declared; this file is the only
place both are in scope at once, which is why it is deliberately short.
"""

from __future__ import annotations

from typing import Any

from . import charter as charter_mod
from . import estimation, valuation
from .config import load_charter, load_valuation_config
from .models import (
    Cluster,
    Ordering,
    Posture,
    RankedTicket,
    ScoredTicket,
    SeverityBreakdown,
    TicketEstimate,
)


def severity(
    est: TicketEstimate, charter: dict[str, Any] | None = None
) -> SeverityBreakdown:
    """The one line that matters.

        claimed  = stated_urgency * clamp(credibility, 0.3, 1.0)
        observed = max(blast_radius, category_severe_rate, charter_floor)
        severity = max(claimed, observed)          <- max, NOT average

    The guarantee `max` buys, stated exactly:

        severity >= observed, always.

    A measurement is never diluted by the reputation of whoever reported it. Under
    `0.5*claimed + 0.5*observed`, a chronic exaggerator's genuine outage measured at
    0.94 is scored 0.62 -- a 34% haircut applied to an instrument reading because of
    who filed the ticket. That is the boy-who-cried-wolf false negative, and it is an
    averaging bug, not a probability problem.

    It produces real inversions, not just wrong levels. Take the inflator's outage
    (claimed 0.30, observed 0.94) against a trusted merchant's moderate, well
    corroborated issue where claim and evidence agree (claimed 0.675, observed 0.65):

        averaged:  0.620  <  0.663    the outage loses
        max:       0.940  >  0.675    the outage wins

    Averaging punishes precisely the tickets where claim and evidence disagree most,
    which is exactly the population the credibility system exists to handle.

    What max does NOT claim: it is not a general fix for loud trusted merchants. A
    credible account that states 'critical' still carries a high `claimed` term, and
    should -- that is what credibility is for. Credibility remains a tiebreaker for
    UNSUPPORTED claims; the moment any independent source corroborates, the discount
    stops mattering.

    Guarded by tests/test_scorer.py::test_chronic_inflator_real_outage_still_ranks_first
    and ::test_averaging_would_have_produced_the_false_negative
    """
    c = charter or load_charter()
    lo, hi = charter_mod.credibility_clamp(c)
    discount = max(lo, min(hi, est.credibility))

    claimed = est.claimed_urgency_value * discount

    floor, floor_reason = charter_mod.severity_floor(est, c)
    observed = max(est.blast_radius, est.category_severe_rate, floor)

    sev = max(claimed, observed)
    floor_is_binding = floor > 0 and floor >= max(est.blast_radius, est.category_severe_rate)
    return SeverityBreakdown(
        claimed=claimed,
        observed=observed,
        charter_floor=floor,
        charter_floor_reason=floor_reason if floor_is_binding else None,
        severity=sev,
        driver="claimed" if claimed > observed else "observed",
    )


def score_tickets(
    estimates: list[TicketEstimate],
    posture: Posture,
    clusters: list[Cluster] | None = None,
    charter: dict[str, Any] | None = None,
) -> list[ScoredTicket]:
    """Score every ticket, promoting clustered severity to the whole cluster.

    Correlated tickets are one event. `severity(cluster) = aggregate(members)`, not
    `max(members)` -- see estimation.aggregate_cluster_severity for why.
    """
    c = charter or load_charter()
    vcfg = load_valuation_config()

    by_id = {e.ticket_id: e for e in estimates}
    membership = _membership(list(by_id), clusters)

    raw_severity = {e.ticket_id: severity(e, c) for e in estimates}

    cluster_sev: dict[str, float] = {}
    for cid, members in membership["clusters"].items():
        if len(members) > 1:
            cluster_sev[cid] = estimation.aggregate_cluster_severity(
                [raw_severity[m].severity for m in members]
            )

    out: list[ScoredTicket] = []
    for est in estimates:
        cid = membership["of_ticket"][est.ticket_id]
        members = membership["clusters"][cid]
        sev_bd = raw_severity[est.ticket_id]
        agg = cluster_sev.get(cid)
        effective = max(sev_bd.severity, agg) if agg is not None else sev_bd.severity

        comp = valuation.components(est, effective, posture, vcfg)
        out.append(
            ScoredTicket(
                ticket_id=est.ticket_id,
                customer_id=est.customer_id,
                estimate=est,
                severity=sev_bd,
                components=comp,
                score=valuation.weighted_score(comp, posture),
                cluster_id=cid,
                cluster_size=len(members),
                cluster_severity=agg,
            )
        )
    return out


def rank(
    scored: list[ScoredTicket],
    posture: Posture,
    capacity: int,
    charter: dict[str, Any] | None = None,
) -> Ordering:
    """Sort, enforce the charter, then split at the capacity line.

    Tiebreak is explicit and defended: score desc, then longest waiting first, then
    lowest ticket ID. Never rely on sort stability over float scores -- it makes
    orderings drift between the rehearsal and the recording, and corrupts the
    stability metric reported in the README.
    """
    ordered = sorted(
        scored,
        key=lambda s: (-s.score, -s.estimate.waiting_hours, s.ticket_id),
    )
    ranked = [
        RankedTicket(
            rank=i + 1,
            ticket_id=s.ticket_id,
            customer_id=s.customer_id,
            score=s.score,
            served=i < capacity,
            scored=s,
            justification=justify(s, posture),
        )
        for i, s in enumerate(ordered)
    ]
    result = charter_mod.enforce_full(ranked, capacity, charter or load_charter())
    return Ordering(
        posture=posture.name,
        ranked=result.ranked,
        overrides=result.overrides,
        capacity=capacity,
        oversubscribed=result.oversubscribed,
    )


def capacity_split(ordering: Ordering) -> tuple[list[str], list[str]]:
    """A ranking is not a decision. This is the line that turns an ordering into an
    action, and it is what makes 'this merchant was skipped six times' a true
    statement rather than a figure of speech."""
    served = [r.ticket_id for r in ordering.ranked if r.served]
    deferred = [r.ticket_id for r in ordering.ranked if not r.served]
    return served, deferred


# ---------------------------------------------------------------- explanation


def justify(s: ScoredTicket, posture: Posture) -> str:
    """One deterministic line per ticket. Available even when every LLM stage is down."""
    est = s.estimate
    bits: list[str] = []

    if s.severity.driver == "observed":
        # Name which of the three terms actually set `observed`. Saying "from evidence
        # (3 users)" when the number really came from a category base rate is the kind
        # of plausible-sounding explanation that is worse than no explanation at all.
        bits.append(f"severity {s.severity.severity:.2f} from {_observed_driver(s)}")
    else:
        bits.append(
            f"severity {s.severity.severity:.2f} rests on the claim alone, "
            f"discounted to {est.credibility:.2f} credibility ({est.credibility_evidence})"
        )

    if s.cluster_size > 1:
        bits.append(f"scored as part of a {s.cluster_size}-ticket cluster")

    top_axis = max(s.components.as_dict().items(), key=lambda kv: posture.weight(kv[0]) * kv[1])
    bits.append(f"{top_axis[0]} dominates under {posture.name} ({top_axis[1]:.2f})")

    if est.times_skipped:
        bits.append(f"skipped {est.times_skipped}x, waiting {est.waiting_hours:.0f}h")

    if est.blocks_paying_workflow_claimed and not est.blocks_paying_workflow_observed:
        bits.append("claims to block a paying workflow; telemetry disagrees")

    return "; ".join(bits)


def _observed_driver(s: ScoredTicket) -> str:
    """Which term set `observed`, in words. Ties resolve to the strongest claim about
    the world: a charter floor is a declaration, measured reach beats a base rate."""
    est = s.estimate
    floor, blast, rate = s.severity.charter_floor, est.blast_radius, est.category_severe_rate
    top = max(floor, blast, rate)

    if floor == top and floor > 0:
        return f"a charter floor of {floor:.2f} ({s.severity.charter_floor_reason})"
    if blast == top:
        return (
            f"measured reach ({est.users_affected} users affected, "
            f"{est.error_rate:.0%} error rate)"
        )
    return (
        f"category history alone -- '{est.category}' issues are genuinely severe "
        f"{rate:.0%} of the time (n={est.category_n}) -- while this ticket's own "
        f"telemetry shows only {est.users_affected} users affected"
    )


# ----------------------------------------------------------------- clustering


def _membership(ticket_ids: list[str], clusters: list[Cluster] | None) -> dict[str, Any]:
    """Every ticket belongs to exactly one cluster. Unclustered tickets become
    singletons, which is also the fallback when the correlation stage fails."""
    of_ticket: dict[str, str] = {}
    groups: dict[str, list[str]] = {}

    for cl in clusters or []:
        members = [t for t in cl.ticket_ids if t in ticket_ids and t not in of_ticket]
        if not members:
            continue
        groups[cl.cluster_id] = members
        for t in members:
            of_ticket[t] = cl.cluster_id

    for t in ticket_ids:
        if t not in of_ticket:
            cid = f"solo::{t}"
            of_ticket[t] = cid
            groups[cid] = [t]

    return {"of_ticket": of_ticket, "clusters": groups}
