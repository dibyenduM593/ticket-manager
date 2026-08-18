"""Credibility maths. Pure functions, no API, sub-millisecond."""

from __future__ import annotations

import pytest

from triage.estimation import credibility, credibility_evidence, was_actually_severe
from triage.models import CustomerHistory, ResolvedTicket, Urgency


def hist(claims: int, confirmed: int) -> CustomerHistory:
    return CustomerHistory(urgency_claims=claims, confirmed_severe=confirmed)


def test_unseen_account_sits_exactly_at_neutral():
    """The question 'what does your system do with an account it has never seen?'
    has a real answer: 0.5, meaning no opinion."""
    assert credibility(CustomerHistory()) == 0.5


def test_single_data_point_does_not_brand_an_account():
    """One unconfirmed claim moves an account to 0.33, not to 0. The Beta(1,1) prior
    is four characters of code and this is what it buys."""
    assert credibility(hist(claims=1, confirmed=0)) == pytest.approx(1 / 3)
    assert credibility(hist(claims=1, confirmed=1)) == pytest.approx(2 / 3)


def test_northpeak_profile_matches_the_designed_track_record():
    """2 of 9 confirmed -> 0.27. This is the number the whole demo turns on."""
    assert credibility(hist(claims=9, confirmed=2)) == pytest.approx(3 / 11, abs=1e-9)
    assert round(credibility(hist(claims=9, confirmed=2)), 2) == 0.27


def test_credibility_is_monotone_in_confirmations():
    scores = [credibility(hist(claims=10, confirmed=c)) for c in range(11)]
    assert scores == sorted(scores)


def test_credibility_never_reaches_the_endpoints():
    """A perfect record is not certainty and a terrible one is not proof of lying."""
    assert 0.0 < credibility(hist(claims=50, confirmed=0)) < 0.1
    assert 0.9 < credibility(hist(claims=50, confirmed=50)) < 1.0


def test_evidence_string_is_readable_and_honest_about_emptiness():
    assert "no prior urgency claims" in credibility_evidence(CustomerHistory())
    assert credibility_evidence(hist(9, 2)) == "2 of 9 urgency claims confirmed severe"


# --------------------------------------------------- the operational definition


def resolved(**kw) -> ResolvedTicket:
    base = dict(
        id="TKT-1",
        customer_id="acme",
        category="integration_api",
        stated_urgency=Urgency.high,
        claimed_urgent=True,
        actual_users_affected=0,
        code_changed=False,
        hours_to_resolve=1.0,
        batch=1,
    )
    base.update(kw)
    return ResolvedTicket(**base)


def test_severity_verdict_is_derived_not_stored():
    """We store operational facts and derive the verdict, so the definition of
    'genuine concern' stays visible and arguable rather than asserted."""
    assert was_actually_severe(resolved(actual_users_affected=11))
    assert not was_actually_severe(resolved(actual_users_affected=10))
    assert was_actually_severe(resolved(code_changed=True, hours_to_resolve=8.5))
    assert not was_actually_severe(resolved(code_changed=True, hours_to_resolve=8.0))
    assert not was_actually_severe(resolved(code_changed=False, hours_to_resolve=200.0))


# ------------------------------------------------------------- blast radius


def test_blast_radius_measures_reach_and_only_reach():
    """A failure rate is not a measure of how far something reached.

    A GDPR endpoint returning 500 to its one user is totally broken and barely
    anything's problem. Folding the rate into reach made it score identically to an
    outage across twelve hundred buyers.
    """
    from triage.estimation import blast_radius, failure_intensity
    from triage.models import Telemetry

    total_failure_one_user = Telemetry(ticket_id="t", users_affected=1, error_rate=1.0)
    partial_failure_many = Telemetry(ticket_id="t", users_affected=1240, error_rate=0.68)

    assert blast_radius(total_failure_one_user) < 0.1
    assert blast_radius(partial_failure_many) > 0.9
    assert blast_radius(total_failure_one_user) < blast_radius(partial_failure_many)

    # the rate signal is kept, just not confused with reach
    assert failure_intensity(total_failure_one_user) == 1.0
    assert failure_intensity(partial_failure_many) == pytest.approx(0.68)


def test_no_instrumented_reach_falls_back_to_category_history():
    """Zero users affected gives zero blast radius, and the ticket is then judged on
    what this kind of problem usually does. That is honest rather than pessimistic."""
    from conftest import make_estimate

    from triage import scorer

    uninstrumented = make_estimate(
        blast_radius=0.0, category_severe_rate=0.82, stated_urgency=Urgency.low, credibility=0.5
    )
    bd = scorer.severity(uninstrumented)
    assert bd.observed == pytest.approx(0.82)
    assert bd.driver == "observed"


def test_seeded_state_reproduces_the_designed_profiles():
    """state/customers.json is derived by counting history, not written by hand.
    If the seeder or the severity definition drifts, this fails."""
    from triage.state import State

    state = State.load()
    expected = {
        "northpeak": 0.27,
        "kitecopper": 0.22,
        "bloomvine": 0.57,
        "tidewater": 0.67,
        "verdant": 0.67,
        "soloartisan": 0.75,
    }
    for cust, want in expected.items():
        assert round(credibility(state.customer(cust)), 2) == want, cust
