"""ESTIMATION -- facts, in natural units. Learned from history. Empirically testable.

This module answers: how credible is this claim, how many users are affected, how
long will this really take, how long has this waited.

It does NOT answer: whether a $480k account matters more than 2,000 free users.

Hard rule, enforced by `tests/test_separation.py`: this module may not import
`valuation`, `config.load_postures`, or anything under `config/postures/`. A company
with the opposite ethics would compute identical numbers from this file.

The one deliberate exception in the whole system runs the other way -- the charter
sets a declared FLOOR on observed severity -- and it is applied in scorer.py, in the
open, not smuggled in here. See config/charter.yaml `severity_floors`.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .models import (
    URGENCY_SCALE,
    CrmRecord,
    CustomerHistory,
    ResolvedTicket,
    Telemetry,
    Ticket,
    TicketEstimate,
)
from .state import State

#: Users-affected that saturates the blast-radius term. This is a normalisation
#: constant for an ESTIMATE (how far did this reach), not a weight (how much do we
#: care). A platform with 20x the merchants would set it higher; a company with
#: different values would not touch it.
USERS_FULL_SCALE = 2000


# ------------------------------------------------------------------ credibility


def credibility(hist: CustomerHistory) -> float:
    """Beta(1,1) posterior mean. Shrinks toward 0.5 for low-evidence accounts.

    The +1/+2 is deliberate: a brand-new merchant with one claim sits near neutral
    instead of being branded on a single data point. An account we have never seen
    scores exactly 0.5 -- we have no opinion, and saying so is more honest than
    defaulting to trust or to suspicion.
    """
    return (hist.confirmed_severe + 1) / (hist.urgency_claims + 2)


def credibility_evidence(hist: CustomerHistory) -> str:
    if hist.urgency_claims == 0:
        return "no prior urgency claims on record (prior only)"
    return (
        f"{hist.confirmed_severe} of {hist.urgency_claims} urgency claims "
        f"confirmed severe"
    )


# --------------------------------------------------------------- severity truth


def was_actually_severe(r: ResolvedTicket) -> bool:
    """An OPERATIONAL definition of 'this was a genuine concern', not a truth claim.

    Stated as a line a reviewer can move: more than ten users hit, or it needed a
    code change and took a working day to fix. Everything else was, in retrospect,
    a support conversation rather than an incident.

    We derive this rather than storing `was_severe: true` precisely so the
    definition stays visible and arguable.
    """
    return r.actual_users_affected > 10 or (r.code_changed and r.hours_to_resolve > 8)


# --------------------------------------------------------------- blast radius


def blast_radius(t: Telemetry) -> float:
    """How far did this reach, per instruments that do not know who filed it.

    REACH ONLY, deliberately. An earlier version took `max` over users-affected and
    the failure rates, which meant a GDPR endpoint returning 500 to its one user
    scored a blast radius of 1.0 -- the same as an outage across twelve hundred
    buyers. The report then said "severity 1.00 from measured reach (1 user
    affected)", which is not a sentence anyone should have to defend.

    A failure RATE is not a measure of reach. It says how completely the affected
    users are broken, not how many there are, and `users_affected` already counts
    them. Where reach is instrumented, rate adds nothing; where it is not, rate
    cannot manufacture it.

    Zero users affected therefore gives zero blast radius, and the ticket falls back
    to its category's base rate in `observed`. That is the honest answer: we have no
    measurement of reach here, so we are using what this kind of problem usually does.

    `failure_intensity` below keeps the rate signal available as a reported fact.
    """
    return _log_norm(t.users_affected, USERS_FULL_SCALE)


def failure_intensity(t: Telemetry) -> float:
    """How completely broken the affected users are. Reported, not summed into reach."""
    signals = [_clamp01(t.error_rate)]
    if t.checkout_success_rate is not None:
        signals.append(_clamp01(1.0 - t.checkout_success_rate))
    return max(signals)


def observed_blocking(t: Telemetry) -> bool:
    """Does the platform agree that a paying workflow is blocked?

    This is the check that lets a declared `blocks_paying_workflow: true` be
    contradicted -- the merchant ticks the box, telemetry reports GMV at risk of
    zero because their checkout is completing fine.
    """
    if t.gmv_at_risk_per_hour > 0:
        return True
    if t.checkout_success_rate is not None and t.checkout_success_rate < 0.95:
        return True
    return False


# ------------------------------------------------------- urgency from the text

_SHOUT_PATTERNS = (
    r"\bASAP\b",
    r"\bURGENT\b",
    r"\bIMMEDIATELY\b",
    r"\bCRITICAL\b",
    r"\bEMERGENCY\b",
    r"\bP0\b",
    r"\bNOW\b",
    r"\bESCALATE\b",
    r"\bOUTAGE\b",
    r"\blosing (?:thousands|money|revenue|sales)\b",
    r"\bevery minute\b",
    r"\bCEO\b|\bCTO\b|\blegal\b|\blawyer\b",
    r"\bcancel(?:ling)? (?:our|my) (?:contract|account|plan)\b",
)


def urgency_intensity_heuristic(body: str) -> float:
    """Deterministic fallback for the LLM's `urgency_intensity` extraction.

    Counts escalation markers, shouting, and exclamation. Crude on purpose -- its job
    is to keep the pipeline running when the API is down, and to give the LLM's
    extraction something to be checked against, not to be clever.
    """
    if not body.strip():
        return 0.0
    score = 0.0
    for pat in _SHOUT_PATTERNS:
        if re.search(pat, body, flags=re.IGNORECASE):
            score += 0.12
    words = body.split()
    if words:
        shouted = sum(1 for w in words if len(w) > 3 and w.isupper())
        score += min(0.25, shouted / len(words) * 2.0)
    score += min(0.20, body.count("!") * 0.05)
    return _clamp01(score)


# ------------------------------------------------------------------------ time


def hours_between(later: datetime, earlier: datetime) -> float:
    return max(0.0, (_utc(later) - _utc(earlier)).total_seconds() / 3600.0)


def waiting_hours(ticket: Ticket, state: State, as_of: datetime) -> float:
    """Wall-clock wait, measured against `company_state.as_of` -- never datetime.now().

    An injectable clock is the difference between golden tests that hold and golden
    tests that rot overnight.
    """
    entry = state.ledger_entry(ticket.id)
    start = (entry.first_seen_at if entry and entry.first_seen_at else ticket.submitted_at)
    return hours_between(as_of, start)


# ------------------------------------------------------------- the whole picture


def estimate_ticket(
    ticket: Ticket,
    telemetry: Telemetry,
    crm: CrmRecord,
    state: State,
    as_of: datetime,
    urgency_intensity: float | None = None,
) -> TicketEstimate:
    """Assemble every fact about one ticket. No weights are applied here."""
    hist = state.customer(ticket.customer_id)
    cat = state.category(ticket.category)
    entry = state.ledger_entry(ticket.id)

    return TicketEstimate(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        category=ticket.category,
        stated_urgency=ticket.stated_urgency,
        claimed_urgency_value=URGENCY_SCALE[ticket.stated_urgency.value],
        urgency_intensity=(
            urgency_intensity
            if urgency_intensity is not None
            else urgency_intensity_heuristic(ticket.body)
        ),
        credibility=credibility(hist),
        credibility_evidence=credibility_evidence(hist),
        users_affected=telemetry.users_affected,
        error_rate=telemetry.error_rate,
        blast_radius=blast_radius(telemetry),
        category_severe_rate=cat.severe_rate,
        category_median_hours=cat.median_hours,
        category_n=cat.n,
        security_exposure=telemetry.security_exposure,
        data_loss=telemetry.data_loss,
        tier=crm.tier,
        arr=crm.arr,
        gmv_at_risk_per_hour=telemetry.gmv_at_risk_per_hour,
        waiting_hours=waiting_hours(ticket, state, as_of),
        times_skipped=entry.times_skipped if entry else 0,
        expected_resolution_hours=cat.median_hours,
        compliance_deadline_hours=(
            hours_between(ticket.compliance_deadline_at, as_of)
            if ticket.compliance_deadline_at
            else None
        ),
        sla_deadline_hours=(
            hours_between(ticket.sla_deadline_at, as_of) if ticket.sla_deadline_at else None
        ),
        blocks_paying_workflow_claimed=ticket.blocks_paying_workflow,
        blocks_paying_workflow_observed=observed_blocking(telemetry),
    )


# ------------------------------------------------------------------- aggregate


def aggregate_cluster_severity(severities: list[float]) -> float:
    """Noisy-OR. Three merchants at 0.5 each are not a 0.5 problem, they are a 0.875
    problem: the chance that *nothing* is systemically wrong is the product of the
    chances that each report individually means nothing.

    Explicitly NOT max(): max would say a cluster is exactly as bad as its loudest
    member, which is the modelling error that makes correlated tickets look like
    three separate mid-severity complaints.
    """
    if not severities:
        return 0.0
    survival = 1.0
    for s in severities:
        survival *= 1.0 - _clamp01(s)
    return _clamp01(1.0 - survival)


# ----------------------------------------------------------------------- utils


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _log_norm(value: float, full_scale: float) -> float:
    if value <= 0:
        return 0.0
    return _clamp01(math.log1p(value) / math.log1p(full_scale))


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
