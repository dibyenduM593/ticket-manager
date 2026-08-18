"""The eval has to be harder to fool than the detector it scores.

A recall number is a claim about the system, and a matcher that is generous by
accident turns that claim into marketing. These tests pin the ways it could flatter:
one detection satisfying several planted conflicts, a wrong source pair counting as
an exact hit, a vague detection on the right ticket scoring the same as a precise one.
"""

from __future__ import annotations

import pytest

from triage import evaluate
from triage.models import Conflict, PlantedConflict


def planted(pid: str, ticket: str, sources: list[str]) -> PlantedConflict:
    return PlantedConflict(id=pid, ticket_id=ticket, sources=sources, description=pid)


def detected(ticket: str, a: str, b: str) -> Conflict:
    return Conflict(
        ticket_id=ticket, source_a=a, claim_a="x", source_b=b, claim_b="y",
        trusted=b, reasoning="because", severity_of_conflict="medium",
    )


# ----------------------------------------------------------------- normalising


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("telemetry", "telemetry"),
        ("Telemetry", "telemetry"),
        ("monitoring metrics", "telemetry"),
        ("CRM", "crm"),
        ("crm/billing record", "crm"),
        ("resolution history", "history"),
        ("the fairness ledger", "ledger"),
        ("ticket message", "ticket"),
    ],
)
def test_source_aliases_normalise(raw: str, expected: str) -> None:
    assert evaluate.normalise_source(raw) == expected


def test_unknown_source_survives_rather_than_vanishing() -> None:
    """An unmatched detection citing a source that does not exist should be visible
    as exactly that, not silently mapped onto a real one."""
    assert evaluate.normalise_source("Astrology") == "astrology"


# -------------------------------------------------------------------- matching


def test_exact_source_pair_is_an_exact_hit() -> None:
    result = evaluate.match_batch(
        planted=[planted("P1", "TKT-1", ["crm", "telemetry"])],
        detected=[detected("TKT-1", "CRM", "telemetry")],
        batch_id=1, label="",
    )
    assert result.strict_hits == 1
    assert result.missed == []
    assert result.unmatched == []


def test_right_ticket_wrong_source_pair_is_not_an_exact_hit() -> None:
    """Finding *a* contradiction on TKT-1 is not finding the one we planted. It
    counts toward recall and explicitly not toward exact recall."""
    result = evaluate.match_batch(
        planted=[planted("P1", "TKT-1", ["crm", "telemetry"])],
        detected=[detected("TKT-1", "ticket", "telemetry")],
        batch_id=1, label="",
    )
    assert len(result.matches) == 1
    assert result.strict_hits == 0
    assert result.matches[0].kind == "same ticket, different source pair"


def test_wrong_ticket_never_matches() -> None:
    result = evaluate.match_batch(
        planted=[planted("P1", "TKT-1", ["crm", "telemetry"])],
        detected=[detected("TKT-2", "crm", "telemetry")],
        batch_id=1, label="",
    )
    assert result.matches == []
    assert [p.id for p in result.missed] == ["P1"]
    assert len(result.unmatched) == 1


def test_one_detection_cannot_satisfy_two_planted_conflicts() -> None:
    """The failure this test exists for: a detector emitting one vague conflict per
    ticket and scoring full recall on a batch with three conflicts on that ticket."""
    result = evaluate.match_batch(
        planted=[
            planted("P1", "TKT-1", ["crm", "telemetry"]),
            planted("P2", "TKT-1", ["ticket", "telemetry"]),
            planted("P3", "TKT-1", ["history", "telemetry"]),
        ],
        detected=[detected("TKT-1", "telemetry", "crm")],
        batch_id=1, label="",
    )
    assert len(result.matches) == 1
    assert len(result.missed) == 2


def test_exact_matches_are_assigned_before_loose_ones() -> None:
    """Greedy in two passes. If the loose pass ran first it could consume the
    detection that the exact pass needed, and under-report exact recall."""
    result = evaluate.match_batch(
        planted=[
            planted("P1", "TKT-1", ["ticket", "telemetry"]),
            planted("P2", "TKT-1", ["crm", "telemetry"]),
        ],
        detected=[
            detected("TKT-1", "telemetry", "history"),   # loose fit for either
            detected("TKT-1", "crm", "telemetry"),       # exact fit for P2 only
        ],
        batch_id=1, label="",
    )
    by_id = {m.planted_id: m for m in result.matches}
    assert by_id["P2"].strict is True
    assert by_id["P1"].strict is False


# ----------------------------------------------------------------- arithmetic


def test_recall_and_precision_floor_arithmetic() -> None:
    batch = evaluate.match_batch(
        planted=[
            planted("P1", "TKT-1", ["crm", "telemetry"]),
            planted("P2", "TKT-2", ["ticket", "history"]),
        ],
        detected=[
            detected("TKT-1", "crm", "telemetry"),
            detected("TKT-3", "ticket", "telemetry"),
        ],
        batch_id=1, label="",
    )
    result = evaluate.EvalResult(detector="test", batches=[batch])

    assert result.planted == 2
    assert result.matched == 1
    assert result.recall == pytest.approx(0.5)
    assert result.recall_exact == pytest.approx(0.5)
    assert result.unmatched == 1
    assert result.precision_floor == pytest.approx(0.5)


def test_empty_detection_is_zero_recall_not_a_crash() -> None:
    result = evaluate.EvalResult(
        detector="test",
        batches=[evaluate.match_batch([planted("P1", "TKT-1", ["crm"])], [], 1, "")],
    )
    assert result.recall == 0.0
    assert result.precision_floor == 0.0
    assert "MISS P1" in evaluate.render(result)


# ------------------------------------------------------- against the real data


def test_labels_are_never_shown_to_the_model(repo_root) -> None:
    """`planted_conflicts` is ground truth. If it reached a prompt the recall number
    would be measuring the model's reading comprehension, not its detection."""
    llm_dir = repo_root / "src" / "triage" / "llm"
    for path in llm_dir.glob("*.py"):
        assert "planted" not in path.read_text(encoding="utf-8"), path


def test_deterministic_detector_scores_against_the_real_batches() -> None:
    """The rule-based fallback finds the arithmetic conflicts and misses the
    interesting ones. It should score well under 100% -- an eval this detector aced
    would be an eval that is not measuring anything."""
    result = evaluate.evaluate(use_llm=False)

    assert result.planted == 21
    assert result.degraded is True
    assert 0.0 < result.recall < 0.8
    assert result.matched_exact <= result.matched
