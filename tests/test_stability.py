"""The stability harness reports a number the README quotes. It has to be right about
what moved, not just about how often something did.

"9/10 identical" is a footnote if the divergence was ranks 4 and 5, and a retraction
if it was ranks 1 and 2. These tests pin that distinction.
"""

from __future__ import annotations

from triage import paths, stability


def sample(order: tuple[str, ...], served: int = 3, **kw) -> stability.RunSample:
    defaults = dict(
        posture="crisis_mode", conflicts=3, clusters=1,
        changed_mind=False, overrides=(), degraded=(),
    )
    defaults.update(kw)
    return stability.RunSample(ordering=order, served=order[:served], **defaults)


def result_of(*orders: tuple[str, ...], **kw) -> stability.StabilityResult:
    return stability.StabilityResult(
        batch_id=42, path="x", runs=len(orders), mode="test",
        samples=[sample(o, **kw) for o in orders],
    )


A = ("T1", "T2", "T3", "T4", "T5")
SWAP_LOW = ("T1", "T2", "T3", "T5", "T4")
SWAP_TOP = ("T2", "T1", "T3", "T4", "T5")


# ------------------------------------------------------------------ agreement


def test_identical_runs_report_one_ordering_and_no_movement() -> None:
    r = result_of(A, A, A)
    assert r.distinct == 1
    assert r.modal_count == 3
    assert r.unstable_ranks == []
    assert r.stable_prefix == 5
    assert r.served_stable is True


def test_low_stakes_swap_is_localised_to_the_ranks_that_moved() -> None:
    r = result_of(A, A, A, A, A, A, A, A, A, SWAP_LOW)
    assert r.modal_count == 9
    assert r.distinct == 2
    assert r.unstable_ranks == [4, 5]
    assert r.stable_prefix == 3


def test_a_swap_at_the_top_is_not_hidden_by_a_high_agreement_number() -> None:
    """The case that would invalidate a demo: 9/10 identical, and the one divergence
    is rank 1. `stable_prefix` has to go to zero and say so."""
    r = result_of(A, A, A, A, A, A, A, A, A, SWAP_TOP)
    assert r.modal_count == 9
    assert r.unstable_ranks == [1, 2]
    assert r.stable_prefix == 0
    assert "rank 1 moved" in stability.render(r)


def test_served_set_stability_is_separate_from_ordering_stability() -> None:
    """A swap inside the served set is cosmetic. A swap across the capacity line
    changes who gets worked on, and is not."""
    inside = result_of(A, SWAP_TOP, served=3)
    across = result_of(A, SWAP_LOW, served=3)

    assert inside.served_stable is True     # T1/T2 both served either way
    assert across.served_stable is True     # T4/T5 both deferred either way

    crossing = result_of(A, ("T1", "T2", "T4", "T3", "T5"), served=3)
    assert crossing.served_stable is False


# -------------------------------------------------------------- upstream drift


def test_upstream_variation_is_reported_even_when_the_ordering_is_stable() -> None:
    """The ordering can be stable while the reasoning that produced it is not.
    Reporting only the ordering would hide that."""
    r = stability.StabilityResult(
        batch_id=42, path="x", runs=2, mode="test",
        samples=[sample(A, conflicts=5), sample(A, conflicts=9)],
    )
    assert r.unstable_ranks == []
    rendered = stability.render(r)
    assert "varied" in rendered
    assert "5 x1" in rendered and "9 x1" in rendered


def test_degraded_runs_are_flagged_so_a_perfect_score_is_not_misread() -> None:
    r = result_of(A, A, A, degraded=("no API key",))
    assert r.degraded is True
    assert "DEGRADED" in stability.render(r)


# ----------------------------------------------------------- against real data


def test_deterministic_core_is_stable_and_persists_nothing() -> None:
    """Three real runs of batch 42. The core is stable by construction -- the point
    of asserting it is that the harness measures the pipeline and not the ledger:
    if runs persisted state, fairness debt would move the ordering between them."""
    before = (paths.state_dir() / "ledger.json").read_text(encoding="utf-8")

    r = stability.measure(runs=3, use_llm=False, posture="revenue_first")

    assert r.runs == 3
    assert r.distinct == 1
    assert r.unstable_ranks == []
    assert r.degraded is True
    assert (paths.state_dir() / "ledger.json").read_text(encoding="utf-8") == before
