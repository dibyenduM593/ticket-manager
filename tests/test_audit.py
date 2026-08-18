"""The audit is the system's report on itself, so it is the last place to be generous.

Noticing a single unfair ranking is decent. Noticing a policy drifting toward
unfairness and warning before it breaches is the point. Reporting a breach that has
already happened as a drift would be worse than not warning at all.
"""

from __future__ import annotations

from triage import audit


def decision(tier: str, waiting_hours: float, served: bool, batch_id: int = 42,
             posture: str = "revenue_first") -> dict:
    return {
        "batch_id": batch_id,
        "posture": posture,
        "served": served,
        "estimate": {"tier": tier, "waiting_hours": waiting_hours},
    }


def warnings_for(decisions: list[dict]) -> str:
    return " ".join(audit.audit(decisions)["warnings"])


# ------------------------------------------------------------------- ceiling


def test_wait_inside_the_ceiling_raises_nothing() -> None:
    text = warnings_for([decision("enterprise", 40.0, True), decision("growth", 50.0, True)])
    assert "BREACH" not in text and "drifting" not in text


def test_approaching_the_ceiling_warns_before_it_breaks() -> None:
    """120h ceiling; 100h is inside it but not safely."""
    text = warnings_for([decision("growth", 100.0, False), decision("enterprise", 10.0, True)])
    assert "drifting toward a breach" in text
    assert "BREACHED" not in text


def test_a_wait_past_the_ceiling_is_reported_as_a_breach_not_a_drift() -> None:
    """The bug this test exists for: TKT-4471 waited 128h against a 120h ceiling and
    the audit called it 'drifting toward a breach'."""
    text = warnings_for([decision("growth", 128.0, True), decision("enterprise", 10.0, True)])
    assert "BREACHED" in text
    assert "drifting toward" not in text


def test_the_breach_warning_names_why_the_rule_did_not_prevent_it() -> None:
    """Promotion happens at batch boundaries, so the charter catches a crossing after
    the fact. Saying so is the difference between a warning and an excuse."""
    text = warnings_for([decision("growth", 128.0, True)])
    assert "batch boundaries" in text
    assert "stream" in text


# ---------------------------------------------------------------------- tiers


def test_a_starved_tier_is_named() -> None:
    text = warnings_for([decision("free", 5.0, False) for _ in range(3)]
                        + [decision("enterprise", 5.0, True)])
    assert "free tier has not been served once" in text


def test_a_wide_tier_spread_is_stated_as_a_decision_not_an_accident() -> None:
    text = warnings_for(
        [decision("enterprise", 2.0, True), decision("free", 60.0, False)]
    )
    assert "longer than" in text
    assert "decision rather than a side effect" in text


def test_no_decisions_is_not_a_crash() -> None:
    result = audit.audit([])
    assert result["batches"] == 0
    assert "Run `triage run`" in audit.render(result)
