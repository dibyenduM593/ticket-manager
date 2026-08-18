"""The scorer. The `max` vs weighted-average argument is the point of this file."""

from __future__ import annotations

import pytest
from conftest import make_estimate

from triage import scorer
from triage.config import load_posture
from triage.estimation import aggregate_cluster_severity
from triage.models import Cluster, Urgency


# ------------------------------------------------- the boy-who-cried-wolf test


def test_chronic_inflator_real_outage_still_ranks_first():
    """The single most important behavioural guarantee in the system.

    NorthPeak: credibility 0.27, claims critical, and this time telemetry agrees.
    A trustworthy merchant reports a minor annoyance with full credibility.

    Under `max`, the outage wins, because telemetry does not care who filed it.
    """
    inflator = make_estimate(
        ticket_id="TKT-OUTAGE",
        stated_urgency=Urgency.critical,
        credibility=0.27,
        blast_radius=0.94,
        category_severe_rate=0.82,
    )
    trusted_minor = make_estimate(
        ticket_id="TKT-MINOR",
        stated_urgency=Urgency.high,
        credibility=0.95,
        blast_radius=0.05,
        category_severe_rate=0.04,
    )

    posture = load_posture("balanced")
    scored = scorer.score_tickets([inflator, trusted_minor], posture)
    ordering = scorer.rank(scored, posture, capacity=1)

    assert ordering.ticket_order[0] == "TKT-OUTAGE"
    assert ordering.ranked[0].scored.severity.severity == pytest.approx(0.94)
    assert ordering.ranked[0].scored.severity.driver == "observed"


def averaged(e) -> float:
    """The rejected alternative, implemented so the argument against it is checkable."""
    claimed = e.claimed_urgency_value * max(0.3, min(1.0, e.credibility))
    observed = max(e.blast_radius, e.category_severe_rate)
    return 0.5 * claimed + 0.5 * observed


def test_averaging_dilutes_a_measurement_by_the_reputation_of_the_reporter():
    """The core property, and the cleanest statement of why max is right.

    `severity >= observed` must hold always. Averaging breaks it: a blast radius
    measured at 0.94 is scored 0.62 because of who filed the ticket. Instruments do
    not know the reporter and their readings should not be discounted as if they did.
    """
    outage = make_estimate(stated_urgency=Urgency.critical, credibility=0.27, blast_radius=0.94)

    assert averaged(outage) == pytest.approx(0.62)
    assert averaged(outage) < outage.blast_radius          # the measurement, diluted
    assert scorer.severity(outage).severity == pytest.approx(0.94)
    assert scorer.severity(outage).severity >= outage.blast_radius


def test_averaging_produces_an_actual_rank_inversion():
    """Not just a wrong level -- a wrong order.

    The inflator's real outage (claim and evidence far apart) against a trusted
    merchant's moderate, well-corroborated issue (claim and evidence agreeing).
    Averaging punishes exactly the tickets where the two disagree most, which is the
    whole population the credibility system exists to handle.
    """
    outage = make_estimate(
        ticket_id="TKT-OUTAGE", stated_urgency=Urgency.critical,
        credibility=0.27, blast_radius=0.94,
    )
    corroborated_moderate = make_estimate(
        ticket_id="TKT-MODERATE", stated_urgency=Urgency.high,
        credibility=0.90, blast_radius=0.65, category_severe_rate=0.65,
    )

    assert averaged(outage) < averaged(corroborated_moderate)                    # the bug
    assert (
        scorer.severity(outage).severity
        > scorer.severity(corroborated_moderate).severity
    )                                                                            # the fix


def test_max_makes_no_claim_about_loud_trusted_merchants():
    """Honesty about the limit of the fix: max does NOT rescue an evidenced ticket
    from a credible account that shouts. A trusted merchant stating critical carries
    a high claimed term and should -- that is what credibility is for. Pinned so the
    property is not overclaimed in the README."""
    quiet_real = make_estimate(stated_urgency=Urgency.low, credibility=0.3, blast_radius=0.60)
    loud_trusted = make_estimate(stated_urgency=Urgency.critical, credibility=0.95, blast_radius=0.05)
    assert scorer.severity(loud_trusted).severity > scorer.severity(quiet_real).severity


def test_credibility_is_a_tiebreaker_for_unsupported_claims_only():
    """Same claim, same account, different evidence. The discount bites in exactly
    one of these two cases -- which is what makes it a discount rather than a penalty."""
    unsupported = make_estimate(stated_urgency=Urgency.critical, credibility=0.27, blast_radius=0.0)
    supported = make_estimate(stated_urgency=Urgency.critical, credibility=0.27, blast_radius=0.9)

    assert scorer.severity(unsupported).severity == pytest.approx(0.3)
    assert scorer.severity(unsupported).driver == "claimed"
    assert scorer.severity(supported).severity == pytest.approx(0.9)
    assert scorer.severity(supported).driver == "observed"


def test_discount_dampens_but_never_erases():
    """Charter R4: floor of 0.3 on the multiplier. A merchant with the worst possible
    track record still gets 30% of their stated urgency."""
    worst = make_estimate(stated_urgency=Urgency.critical, credibility=0.01, blast_radius=0.0)
    assert scorer.severity(worst).severity == pytest.approx(0.3)
    assert scorer.severity(worst).severity > 0.0


def test_understated_severity_is_promoted_by_evidence_too():
    """The discount runs in both directions: a merchant who says 'no rush' about a
    real problem is corrected by evidence exactly as a shouter is."""
    understated = make_estimate(
        stated_urgency=Urgency.low, credibility=0.75, blast_radius=0.77
    )
    assert scorer.severity(understated).severity == pytest.approx(0.77)
    assert scorer.severity(understated).driver == "observed"


# ------------------------------------------------------------- charter floors


def test_charter_floor_lifts_a_tiny_blast_radius_to_maximum_severity():
    """One confirmed session used post-revocation is a blast radius of one and a
    severity of 1.0. Severity is not a function of blast radius here."""
    exposure = make_estimate(
        stated_urgency=Urgency.low, credibility=0.5, blast_radius=0.02, security_exposure=True
    )
    bd = scorer.severity(exposure)
    assert bd.severity == pytest.approx(1.0)
    assert bd.charter_floor == pytest.approx(1.0)
    assert bd.driver == "observed"


# --------------------------------------------------------------- aggregation


def test_cluster_severity_is_aggregate_not_max():
    """Three 0.5s are not a 0.5 problem. Noisy-OR: 1 - 0.5^3."""
    assert aggregate_cluster_severity([0.5, 0.5, 0.5]) == pytest.approx(0.875)
    assert aggregate_cluster_severity([0.5]) == pytest.approx(0.5)
    assert aggregate_cluster_severity([]) == 0.0
    assert aggregate_cluster_severity([0.9, 0.9]) > max(0.9, 0.9)


def test_clustered_tickets_are_promoted_together():
    """Individually mid-severity, jointly a platform event -- and the whole cluster
    moves, not just its loudest member."""
    a = make_estimate(ticket_id="TKT-A", blast_radius=0.5, category_severe_rate=0.0)
    b = make_estimate(ticket_id="TKT-B", blast_radius=0.5, category_severe_rate=0.0)
    posture = load_posture("platform_health")

    solo = scorer.score_tickets([a, b], posture, clusters=[])
    assert all(s.cluster_severity is None for s in solo)

    clustered = scorer.score_tickets(
        [a, b], posture,
        clusters=[Cluster(cluster_id="c1", ticket_ids=["TKT-A", "TKT-B"], root_cause="shared")],
    )
    assert all(s.cluster_severity == pytest.approx(0.75) for s in clustered)
    assert all(s.components.criticality == pytest.approx(0.75) for s in clustered)
    assert clustered[0].score > solo[0].score
    assert clustered[1].score > solo[1].score


def test_unclustered_tickets_become_singletons():
    """Fallback: if correlation fails or returns nothing, every ticket is a cluster
    of one and the pipeline proceeds unchanged."""
    a = make_estimate(ticket_id="TKT-A")
    scored = scorer.score_tickets([a], load_posture("balanced"), clusters=None)
    assert scored[0].cluster_size == 1
    assert scored[0].cluster_severity is None


def test_llm_cluster_naming_a_nonexistent_ticket_cannot_break_scoring():
    a = make_estimate(ticket_id="TKT-A")
    scored = scorer.score_tickets(
        [a], load_posture("balanced"),
        clusters=[Cluster(cluster_id="c1", ticket_ids=["TKT-A", "TKT-GHOST"], root_cause="x")],
    )
    assert len(scored) == 1
    assert scored[0].cluster_size == 1


# ------------------------------------------------------------------ ordering


def test_tiebreak_is_explicit_and_not_sort_stability():
    """Identical scores resolve to longest-waiting-first, then lowest ticket ID.
    Never rely on sort stability over float scores -- it makes orderings drift
    between the rehearsal and the recording."""
    posture = load_posture("balanced")
    a = make_estimate(ticket_id="TKT-B", waiting_hours=5.0)
    b = make_estimate(ticket_id="TKT-A", waiting_hours=5.0)
    c = make_estimate(ticket_id="TKT-C", waiting_hours=90.0)

    scored = scorer.score_tickets([a, b, c], posture)
    for s in scored:  # force an exact tie
        s.score = 0.5
    ordering = scorer.rank(scored, posture, capacity=2)
    assert ordering.ticket_order == ["TKT-C", "TKT-A", "TKT-B"]


def test_ordering_is_deterministic_across_repeated_runs():
    posture = load_posture("revenue_first")
    ests = [make_estimate(ticket_id=f"TKT-{i}", blast_radius=i / 10) for i in range(6)]
    first = scorer.rank(scorer.score_tickets(ests, posture), posture, 3).ticket_order
    for _ in range(20):
        assert scorer.rank(scorer.score_tickets(ests, posture), posture, 3).ticket_order == first


def test_capacity_split_is_what_makes_skipped_a_true_statement():
    posture = load_posture("balanced")
    ests = [make_estimate(ticket_id=f"TKT-{i}", blast_radius=1 - i / 10) for i in range(6)]
    ordering = scorer.rank(scorer.score_tickets(ests, posture), posture, capacity=3)
    served, deferred = scorer.capacity_split(ordering)
    assert len(served) == 3 and len(deferred) == 3
    assert all(r.served for r in ordering.ranked[:3])
    assert not any(r.served for r in ordering.ranked[3:])


# ----------------------------------------------------------------- golden test


GOLDEN_BATCH_1 = {
    "revenue_first":   ["TKT-4477", "TKT-4479", "TKT-4482", "TKT-4478", "TKT-4471", "TKT-4480"],
    "crisis_mode":     ["TKT-4479", "TKT-4477", "TKT-4482", "TKT-4471", "TKT-4478", "TKT-4480"],
    "fairness_first":  ["TKT-4471", "TKT-4477", "TKT-4482", "TKT-4479", "TKT-4478", "TKT-4480"],
    "speed_optimised": ["TKT-4477", "TKT-4479", "TKT-4482", "TKT-4471", "TKT-4478", "TKT-4480"],
    "balanced":        ["TKT-4477", "TKT-4479", "TKT-4482", "TKT-4471", "TKT-4478", "TKT-4480"],
    "platform_health": ["TKT-4477", "TKT-4479", "TKT-4482", "TKT-4471", "TKT-4478", "TKT-4480"],
}


@pytest.mark.parametrize("posture_name,expected", sorted(GOLDEN_BATCH_1.items()))
def test_golden_ordering_batch_1(batch1_ctx, posture_name, expected):
    """Fixture + posture -> exact ordering. No API, no clock, no randomness.

    If any of the scoring maths, the config weights, the seeded history or the
    charter changes, this is the test that says so.
    """
    from triage.pipeline import Context, run_deterministic

    working = Context(batch1_ctx.batch, batch1_ctx.company, batch1_ctx.sources, batch1_ctx.state.clone())
    ordering, _, _ = run_deterministic(working, load_posture(posture_name))
    assert ordering.ticket_order == expected
