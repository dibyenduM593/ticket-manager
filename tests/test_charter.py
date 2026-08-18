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


# ------------------------------------------- minimality under multiple protections


def test_a_protected_ticket_already_at_rank_1_is_not_demoted_by_anothers_promotion():
    """Two protections at once: a security exposure that EARNED rank 1 by score, and
    a long-waiter outside capacity that R2a forces in.

    The bug this pins: pass 1 used to extract every protected ticket -- including
    ones already safely served -- and reinsert them as a block after the top
    unprotected tickets. The rank-1 exposure got demoted below a lower-scored
    ticket, and the override log claimed it had been 'deferred past the capacity
    line' while it sat at rank 1. Minimal intervention means the only ticket that
    moves is the one the rule had to move.
    """
    sec = make_estimate(
        ticket_id="TKT-SEC", blast_radius=0.9, security_exposure=True,
        users_affected=900, gmv_at_risk_per_hour=9000.0,
    )
    a = make_estimate(ticket_id="TKT-A", blast_radius=0.55, users_affected=300)
    b = make_estimate(ticket_id="TKT-B", blast_radius=0.45, users_affected=200)
    lw = make_estimate(
        ticket_id="TKT-LW", blast_radius=0.05, stated_urgency=Urgency.low,
        waiting_hours=130.0, times_skipped=6,
    )
    c = make_estimate(ticket_id="TKT-C", blast_radius=0.04, users_affected=1)

    ordering = rank_with([sec, a, b, lw, c], posture_name="crisis_mode", capacity=3)

    # the exposure earned rank 1 by score and must still be there
    assert ordering.ranked[0].ticket_id == "TKT-SEC"
    # the long-waiter is promoted to the LAST served slot, displacing exactly one ticket
    assert ordering.rank_of("TKT-LW") == 3
    assert ordering.rank_of("TKT-A") == 2
    # exactly one override: the ticket the rule had to move, and no other
    assert [o.ticket_id for o in ordering.overrides] == ["TKT-LW"]


def test_override_log_never_records_a_ticket_the_charter_did_not_need_to_move():
    """Every override row is a claim in the report. A row saying a served rank-1
    ticket was 'deferred past the capacity line' is a false statement, and one
    false row poisons trust in the true ones next to it."""
    sec = make_estimate(
        ticket_id="TKT-SEC", blast_radius=0.9, security_exposure=True,
        users_affected=900, gmv_at_risk_per_hour=9000.0,
    )
    a = make_estimate(ticket_id="TKT-A", blast_radius=0.55, users_affected=300)
    lw = make_estimate(
        ticket_id="TKT-LW", blast_radius=0.05, stated_urgency=Urgency.low,
        waiting_hours=130.0, times_skipped=6,
    )

    ordering = rank_with([sec, a, lw], posture_name="crisis_mode", capacity=2)

    for o in ordering.overrides:
        assert o.from_rank != o.to_rank
        # a "must be served" override may only describe a ticket that was actually
        # outside the served set before enforcement
        if o.clause_id in ("R1b", "R2a", "R3a"):
            assert o.from_rank > ordering.capacity, (
                f"{o.ticket_id} was already served at rank {o.from_rank}; "
                f"recording it as force-served is false"
            )


def test_two_protected_outside_capacity_promote_without_disturbing_the_top():
    """Two R2a long-waiters firing at once. (Long waits are the right fixture here:
    a <=48h compliance deadline also carries a 0.95 severity floor, so an R3a ticket
    scores its way to the top and needs no override at all.) The top-scored
    unprotected ticket keeps rank 1, and the protected pair keeps its own relative
    score order."""
    hi = make_estimate(ticket_id="TKT-HI", blast_radius=0.95, users_affected=800)
    mid = make_estimate(ticket_id="TKT-MID", blast_radius=0.70, users_affected=400)
    oth = make_estimate(ticket_id="TKT-OTH", blast_radius=0.60, users_affected=300)
    lw1 = make_estimate(
        ticket_id="TKT-LW1", blast_radius=0.08, stated_urgency=Urgency.low,
        waiting_hours=125.0, times_skipped=5,
    )
    lw2 = make_estimate(
        ticket_id="TKT-LW2", blast_radius=0.05, stated_urgency=Urgency.low,
        waiting_hours=130.0, times_skipped=6,
    )

    ordering = rank_with([hi, mid, oth, lw1, lw2], posture_name="crisis_mode", capacity=3)

    served = [r.ticket_id for r in ordering.ranked if r.served]
    assert ordering.ranked[0].ticket_id == "TKT-HI"
    assert set(served) == {"TKT-HI", "TKT-LW1", "TKT-LW2"}
    assert {o.ticket_id for o in ordering.overrides} == {"TKT-LW1", "TKT-LW2"}


# ------------------------------------------------------------------ properties


def test_charter_invariants_hold_on_randomised_orderings():
    """Seeded fuzz over 300 random orderings. Hand-picked cases prove the bugs I
    thought of; this asserts the invariants on inputs nobody thought of.

    Invariants, on every input:
      1. every must_be_served trigger is served, or the batch is reported
         oversubscribed and no unprotected ticket holds a served slot
      2. no rank_above_category violation survives enforcement
      3. minimality: tickets the charter did not move keep their relative order
      4. every override records a real move (from_rank != to_rank)
    """
    import random

    from triage.charter import enforce_full, _triggers
    from triage.config import load_charter

    rng = random.Random(20260819)
    charter = load_charter()
    clauses = [
        (rule, clause)
        for rule in charter["rules"]
        for clause in rule.get("clauses", [])
        if clause.get("kind") == "must_be_served"
    ]

    for trial in range(300):
        n = rng.randint(1, 8)
        capacity = rng.randint(0, n)
        estimates = []
        for i in range(n):
            estimates.append(
                make_estimate(
                    ticket_id=f"T{trial}-{i}",
                    blast_radius=rng.random(),
                    security_exposure=rng.random() < 0.15,
                    data_loss=rng.random() < 0.10,
                    waiting_hours=rng.choice([1.0, 50.0, 119.0, 121.0, 200.0]),
                    times_skipped=rng.randint(0, 8),
                    compliance_deadline_hours=rng.choice([None, 6.0, 24.0, 100.0]),
                    category=rng.choice(
                        ["checkout_failure", "ui_cosmetic", "integration_api", "security"]
                    ),
                )
            )
        posture = load_posture(rng.choice(["revenue_first", "crisis_mode", "fairness_first"]))
        scored = scorer.score_tickets(estimates, posture)
        ranked = [
            r.model_copy(deep=True)
            for r in scorer.rank(scored, posture, capacity).ranked
        ]
        # rank() already enforced once; fuzz the enforcer directly on a shuffle too
        rng.shuffle(ranked)
        for i, item in enumerate(ranked):
            item.rank, item.served = i + 1, i < capacity

        result = enforce_full(ranked, capacity, charter)
        ctx = f"trial {trial}, capacity {capacity}"

        # -- 1. protection, or an honest refusal
        protected = {
            item.ticket_id
            for item in result.ranked
            if any(_triggers(clause, item.scored) for _, clause in clauses)
        }
        served = [r.ticket_id for r in result.ranked if r.served]
        if capacity > 0 and protected:
            if result.oversubscribed:
                assert len(protected) > capacity, ctx
                assert set(served) <= protected, (
                    f"{ctx}: oversubscribed yet an unprotected ticket is served"
                )
            else:
                assert protected <= set(served), (
                    f"{ctx}: {protected - set(served)} protected but deferred"
                )

        # -- 2. no surviving rank_above violation. A ui_cosmetic ticket that itself
        # carries a protected flag is not a member of the outranked class -- it is
        # not a cosmetic ISSUE, whatever category it was filed under.
        order = result.ranked
        for i, item in enumerate(order):
            if item.scored.estimate.security_exposure or item.scored.estimate.data_loss:
                above = [
                    o for o in order[:i]
                    if o.scored.estimate.category == "ui_cosmetic"
                    and not (o.scored.estimate.security_exposure or o.scored.estimate.data_loss)
                ]
                assert not above, f"{ctx}: exposure below cosmetic {[o.ticket_id for o in above]}"

        # -- 3. minimality: untouched tickets keep relative order
        moved = {o.ticket_id for o in result.overrides}
        before = [r.ticket_id for r in ranked if r.ticket_id not in moved]
        after = [r.ticket_id for r in result.ranked if r.ticket_id not in moved]
        assert before == after, f"{ctx}: unmoved tickets reordered"

        # -- 4. every override is a real move
        for o in result.overrides:
            assert o.from_rank != o.to_rank, f"{ctx}: no-op override for {o.ticket_id}"
