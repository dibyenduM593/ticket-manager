"""Charter enforcement. The rules that no posture can tune."""

from __future__ import annotations

import pytest
from conftest import make_estimate

from triage import charter as charter_mod
from triage import scorer
from triage.config import load_posture
from triage.models import Urgency


def rank_with(estimates, posture_name="revenue_first", capacity=3):
    posture = load_posture(posture_name)
    return scorer.rank(scorer.score_tickets(estimates, posture), posture, capacity)


# ----------------------------------------------------------------- R1: exposure


def test_exposure_deferred_past_capacity_is_force_promoted(batch1_ctx):
    """The demo moment, asserted.

    TKT-4482 is free tier, $0 ARR, stated 'no rush'. Under revenue_first it scores
    5th of 6, which puts it outside a capacity of 3. The charter refuses.
    """
    from triage.pipeline import Context, run_deterministic

    working = Context(batch1_ctx.batch, batch1_ctx.company, batch1_ctx.sources, batch1_ctx.state.clone())
    ordering, _, _ = run_deterministic(working, load_posture("revenue_first"))

    override = next(o for o in ordering.overrides if o.ticket_id == "TKT-4482")
    assert override.rule_id == "R1"
    assert override.clause_id == "R1b"
    assert override.from_rank == 5
    assert override.to_rank == 3
    assert "security exposure" in override.detail
    assert ordering.ranked[2].ticket_id == "TKT-4482"
    assert ordering.ranked[2].served
    assert ordering.ranked[2].charter_promoted


def test_promotion_is_minimal_not_to_the_top():
    """The charter is a FLOOR, not a preference. Promoting to rank 1 would quietly
    turn it into one, and would hide how much the override actually cost."""
    high = make_estimate(ticket_id="TKT-HI", blast_radius=0.95)
    mid = make_estimate(ticket_id="TKT-MID", blast_radius=0.7)
    other = make_estimate(ticket_id="TKT-OTH", blast_radius=0.6)
    exposure = make_estimate(
        ticket_id="TKT-SEC", blast_radius=0.01, stated_urgency=Urgency.low,
        security_exposure=True, tier="free", arr=0.0,
    )

    ordering = rank_with([high, mid, other, exposure], capacity=3)
    assert ordering.rank_of("TKT-SEC") == 3          # the last served slot, not the first
    assert ordering.ranked[0].ticket_id == "TKT-HI"  # the genuine top ticket is untouched


def test_exposure_never_ranks_below_a_cosmetic_ticket():
    """R1a, tested where R1b cannot reach it: capacity large enough that nothing is
    deferred, so only the relative-rank clause can fire."""
    cosmetic = make_estimate(
        ticket_id="TKT-COSM", category="ui_cosmetic", stated_urgency=Urgency.critical,
        credibility=1.0, tier="enterprise", arr=480000.0,
    )
    exposure = make_estimate(
        ticket_id="TKT-SEC", stated_urgency=Urgency.low, blast_radius=0.0,
        security_exposure=True, tier="free", arr=0.0,
    )
    ordering = rank_with([cosmetic, exposure], posture_name="revenue_first", capacity=10)
    assert ordering.rank_of("TKT-SEC") < ordering.rank_of("TKT-COSM")


def test_data_loss_triggers_the_same_protection_as_security_exposure():
    loss = make_estimate(ticket_id="TKT-LOSS", data_loss=True, stated_urgency=Urgency.low,
                         tier="free", arr=0.0, blast_radius=0.0)
    filler = [make_estimate(ticket_id=f"TKT-F{i}", blast_radius=0.9) for i in range(4)]
    ordering = rank_with([loss, *filler], capacity=2)
    assert ordering.rank_of("TKT-LOSS") <= 2
    assert any(o.ticket_id == "TKT-LOSS" for o in ordering.overrides)


# ------------------------------------------------------------- R2: the ceiling


def test_waiting_ceiling_forces_service_regardless_of_tier():
    """No ticket waits beyond 5 days. This is the only rule that reads the ledger."""
    stale = make_estimate(
        ticket_id="TKT-STALE", waiting_hours=121.0, times_skipped=7,
        blast_radius=0.05, tier="free", arr=0.0,
    )
    filler = [make_estimate(ticket_id=f"TKT-F{i}", blast_radius=0.9, arr=480000.0,
                            tier="enterprise") for i in range(4)]
    ordering = rank_with([stale, *filler], posture_name="revenue_first", capacity=2)

    override = next(o for o in ordering.overrides if o.ticket_id == "TKT-STALE")
    assert override.rule_id == "R2"
    assert "121h" in override.detail
    assert ordering.rank_of("TKT-STALE") == 2


def test_one_hour_under_the_ceiling_does_not_fire():
    """The boundary is a boundary, not a suggestion. TKT-4471 sits at 112h in batch 42
    precisely so the rule is seen NOT firing before it fires."""
    nearly = make_estimate(ticket_id="TKT-NEAR", waiting_hours=119.0, blast_radius=0.05)
    filler = [make_estimate(ticket_id=f"TKT-F{i}", blast_radius=0.9) for i in range(4)]
    ordering = rank_with([nearly, *filler], capacity=2)
    assert not any(o.ticket_id == "TKT-NEAR" for o in ordering.overrides)


def test_batch_1_verdant_is_inside_the_ceiling_and_batch_2_is_not(batch1_ctx, batch2_ctx):
    """The narrative arc, asserted: debt climbing in batch 42, breached in batch 43."""
    e1 = {e.ticket_id: e for e in batch1_ctx.estimates()}["TKT-4471"]
    e2 = {e.ticket_id: e for e in batch2_ctx.estimates()}["TKT-4471"]
    assert 100 <= e1.waiting_hours < 120
    assert e2.waiting_hours >= 120


# --------------------------------------------------------- R3: legal deadlines


def test_compliance_deadline_is_hard():
    """A GDPR erasure clock does not negotiate with a revenue posture."""
    gdpr = make_estimate(
        ticket_id="TKT-GDPR", category="compliance", compliance_deadline_hours=18.0,
        blast_radius=0.0, tier="growth", arr=36000.0, stated_urgency=Urgency.high,
    )
    whales = [make_estimate(ticket_id=f"TKT-W{i}", blast_radius=0.95,
                            tier="enterprise", arr=480000.0) for i in range(4)]
    ordering = rank_with([gdpr, *whales], posture_name="revenue_first", capacity=2)
    assert ordering.rank_of("TKT-GDPR") <= 2
    assert any(o.rule_id == "R3" for o in ordering.overrides)


def test_compliance_deadline_also_sets_a_severity_floor():
    gdpr = make_estimate(compliance_deadline_hours=18.0, blast_radius=0.0,
                         stated_urgency=Urgency.low, credibility=0.5)
    bd = scorer.severity(gdpr)
    assert bd.charter_floor == pytest.approx(0.95)
    assert bd.severity == pytest.approx(0.95)


def test_a_distant_compliance_deadline_does_not_fire():
    gdpr = make_estimate(compliance_deadline_hours=200.0, blast_radius=0.0)
    assert scorer.severity(gdpr).charter_floor == 0.0


# ------------------------------------------------------------------ R4: clamp


def test_credibility_clamp_bounds_come_from_the_charter_not_from_code():
    lo, hi = charter_mod.credibility_clamp()
    assert (lo, hi) == (0.3, 1.0)


# ---------------------------------------------------------- interaction cases


def test_multiple_rules_firing_in_one_batch_reach_a_fixed_point():
    """Promoting one ticket can push another across the capacity line and trip a
    second rule. Enforcement iterates until nothing is violated."""
    exposure = make_estimate(ticket_id="TKT-SEC", security_exposure=True,
                             blast_radius=0.0, stated_urgency=Urgency.low)
    stale = make_estimate(ticket_id="TKT-STALE", waiting_hours=130.0, blast_radius=0.05)
    gdpr = make_estimate(ticket_id="TKT-GDPR", compliance_deadline_hours=10.0, blast_radius=0.0)
    whales = [make_estimate(ticket_id=f"TKT-W{i}", blast_radius=0.95,
                            tier="enterprise", arr=480000.0) for i in range(3)]

    ordering = rank_with([exposure, stale, gdpr, *whales], capacity=3)
    served = {r.ticket_id for r in ordering.ranked if r.served}
    assert {"TKT-SEC", "TKT-STALE", "TKT-GDPR"} == served
    assert len(ordering.overrides) >= 3


def test_charter_cannot_be_weakened_by_any_posture(batch1_ctx):
    """Six postures, six orderings, one invariant: the exposure is always served."""
    from triage.pipeline import Context, run_deterministic
    from triage.config import load_postures

    for name, posture in load_postures().items():
        working = Context(batch1_ctx.batch, batch1_ctx.company, batch1_ctx.sources,
                          batch1_ctx.state.clone())
        ordering, _, _ = run_deterministic(working, posture)
        served = {r.ticket_id for r in ordering.ranked if r.served}
        assert "TKT-4482" in served, f"{name} deferred a confirmed exposure"


def test_zero_capacity_disables_must_be_served_without_crashing():
    """Degenerate input: no agents. The rank-above-category clause still applies;
    'must be served' is vacuous when nothing can be."""
    exposure = make_estimate(ticket_id="TKT-SEC", security_exposure=True, blast_radius=0.0)
    filler = [make_estimate(ticket_id=f"TKT-F{i}", blast_radius=0.9) for i in range(3)]
    ordering = rank_with([exposure, *filler], capacity=0)
    assert len(ordering.ranked) == 4
    assert not any(r.served for r in ordering.ranked)
