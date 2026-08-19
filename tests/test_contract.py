"""Layer B -- the LLM contract.

What must hold no matter what the model returns:

  * the output validates against the schema (forced tool use, so this is structural)
  * every ticket appears exactly once in the final ordering
  * every cited ticket ID exists            <- the hallucinated-entity test
  * every cited source name is a real source
  * a malformed or hostile response degrades to the deterministic core, loudly

These run against a programmable double. There is no recorded-response path:
nothing in this repo replays an answer the model gave on some earlier day.
"""

from __future__ import annotations

import pytest
from fakes import (
    FakeLLMClient,
    adjudication_for,
    advice_for,
    conflicts_for,
    critique_for,
)

from triage.config import load_posture, load_postures
from triage.llm import stages as llm_stages
from triage.llm.client import LLMClient, LLMUnavailable
from triage.llm.schemas import AdviceResult, ConflictResult, CorrelationResult
from triage.pipeline import detect_conflicts_deterministic, run_deterministic

VALID_SOURCES = {"ticket", "telemetry", "crm", "history", "ledger", "charter"}


@pytest.fixture
def ctx(batch1_ctx):
    return batch1_ctx


@pytest.fixture
def estimates(ctx):
    return ctx.estimates()


# ------------------------------------------------------- structural invariants


def test_every_ticket_appears_exactly_once_in_the_ordering(ctx):
    ordering, _, _ = run_deterministic(ctx, load_posture("balanced"))
    ids = ordering.ticket_order
    assert sorted(ids) == sorted(t.id for t in ctx.batch.tickets)
    assert len(ids) == len(set(ids))


def test_ranks_are_contiguous_and_start_at_one(ctx):
    ordering, _, _ = run_deterministic(ctx, load_posture("revenue_first"))
    assert [r.rank for r in ordering.ranked] == list(range(1, len(ordering.ranked) + 1))


def test_served_flags_agree_with_capacity(ctx):
    ordering, _, _ = run_deterministic(ctx, load_posture("revenue_first"))
    served = [r for r in ordering.ranked if r.served]
    assert len(served) == min(ctx.capacity, len(ordering.ranked))
    assert all(r.rank <= ctx.capacity for r in served)


# ------------------------------------------------------ hallucinated entities


def test_conflicts_naming_nonexistent_tickets_are_dropped(ctx, estimates):
    """The hallucinated-entity test. A model citing TKT-9999 must not put TKT-9999
    into the report as though it were a real finding."""
    client = FakeLLMClient(
        {
            "conflicts": conflicts_for(
                [("TKT-4477", "ticket", "telemetry"), ("TKT-9999", "ticket", "crm")]
            )
        }
    )
    result = llm_stages.detect_conflicts(
        client, ctx.batch.tickets, estimates, ctx.charter, []
    )
    ids = {c.ticket_id for c in result.value}
    assert ids == {"TKT-4477"}
    assert not result.degraded


def test_clusters_naming_nonexistent_tickets_are_dropped(ctx, estimates):
    client = FakeLLMClient(
        {
            "correlate": {
                "clusters": [
                    {
                        "ticket_ids": ["TKT-4478", "TKT-9999"],
                        "root_cause": "invented",
                        "shared_evidence": ["none"],
                        "confidence": 0.9,
                    }
                ],
                "reasoning": "r",
            }
        }
    )
    result = llm_stages.correlate_tickets(
        client, ctx.batch.tickets, estimates, ctx.charter, ctx.sources.telemetry
    )
    for cluster in result.value:
        for tid in cluster.ticket_ids:
            assert tid in {t.id for t in ctx.batch.tickets}
    # a cluster reduced below two real members is no cluster at all
    assert all(len(c.ticket_ids) >= 2 for c in result.value)


def test_critique_citing_a_nonexistent_ticket_is_filtered(ctx):
    ordering, clusters, _ = run_deterministic(ctx, load_posture("balanced"))
    client = FakeLLMClient(
        {f"critic_{c['key']}": critique_for(["TKT-4471", "TKT-9999"]) for c in llm_stages.CRITICS}
    )
    result = llm_stages.critique(client, ordering, clusters, [], "situation", ctx.charter)
    for c in result.value:
        assert "TKT-9999" not in c.specific_tickets
        assert "TKT-4471" in c.specific_tickets


def test_adjudication_revising_a_nonexistent_ticket_is_ignored(ctx):
    ordering, _, _ = run_deterministic(ctx, load_posture("balanced"))
    client = FakeLLMClient({"adjudicate": adjudication_for([("TKT-9999", 1)])})
    result = llm_stages.adjudicate(client, ordering, [_stub_critique()], ctx.charter)
    assert result.value.revisions == []
    assert result.value.changed_mind is False


def test_advice_naming_an_unknown_posture_yields_no_advice(ctx, estimates):
    """A posture the config does not declare produces NO advice, not a substitute one.

    Silently swapping in another recommendation would put a posture nobody chose in
    front of a human under the word "recommended".
    """
    client = FakeLLMClient({"advisor": advice_for("maximise_shareholder_value", ["balanced"])})
    result = llm_stages.advise_posture(
        client, "situation", load_postures(), ctx.charter, ctx.batch.tickets, estimates
    )
    assert result.degraded
    assert "unknown posture" in result.reason
    assert result.value is None


def test_no_key_means_no_advice_at_all(ctx, estimates):
    result = llm_stages.advise_posture(
        FakeLLMClient(available=False), "situation", load_postures(), ctx.charter,
        ctx.batch.tickets, estimates,
    )
    assert result.value is None
    assert result.degraded


def test_no_key_means_no_adjudication_object(ctx):
    """Absent, not a stand-in carrying `changed_mind: false` and a written defence."""
    ordering, _, _ = run_deterministic(ctx, load_posture("balanced"))
    result = llm_stages.adjudicate(FakeLLMClient(available=False), ordering, [], ctx.charter)
    assert result.value is None
    assert result.degraded


# ---------------------------------------------------------- source validity


def test_every_cited_source_is_a_real_source(ctx, estimates):
    """Deterministic conflicts must only cite sources that exist; the schema pins the
    LLM to the same enum, so this catches drift on our side of the contract."""
    for c in detect_conflicts_deterministic(estimates):
        assert c.source_a in VALID_SOURCES or c.source_a == "neither, fully"
        assert c.source_b in VALID_SOURCES


def test_the_conflict_schema_rejects_an_invented_source():
    with pytest.raises(Exception):
        ConflictResult.model_validate(
            {
                "conflicts": [
                    {
                        "ticket_id": "TKT-1",
                        "source_a": "astrology",
                        "claim_a": "x",
                        "source_b": "telemetry",
                        "claim_b": "y",
                        "trusted": "telemetry",
                        "reasoning": "r",
                        "severity_of_conflict": "low",
                    }
                ],
                "tickets_with_no_conflict": [],
            }
        )


def test_the_correlation_schema_rejects_a_one_ticket_cluster():
    """A 'cluster' of one is a category error, and the schema says so rather than
    leaving it to be silently filtered downstream."""
    with pytest.raises(Exception):
        CorrelationResult.model_validate(
            {"clusters": [{"ticket_ids": ["TKT-1"], "root_cause": "x",
                           "shared_evidence": [], "confidence": 1.0}], "reasoning": "r"}
        )


def test_the_advice_schema_rejects_an_out_of_range_rank():
    with pytest.raises(Exception):
        AdviceResult.model_validate(
            {
                "recommended": "balanced",
                "reasoning": "r",
                "what_it_costs": "c",
                "ranked_alternatives": [{"posture": "balanced", "rank": 99,
                                         "reasoning": "r", "trade_off": "t"}],
            }
        )


# --------------------------------------------------------------- degradation


def test_a_failing_stage_degrades_and_says_so(ctx, estimates):
    """No handler registered -> LLMUnavailable -> deterministic fallback, with the
    reason carried to the report. A silent fallback would be the worst outcome."""
    client = FakeLLMClient({})
    result = llm_stages.detect_conflicts(
        client, ctx.batch.tickets, estimates, ctx.charter,
        detect_conflicts_deterministic(estimates),
    )
    assert result.degraded
    assert "conflict detection failed" in result.reason
    assert result.value  # the deterministic detector still found things


def test_partial_critic_failure_is_reported_not_hidden(ctx):
    ordering, clusters, _ = run_deterministic(ctx, load_posture("balanced"))
    client = FakeLLMClient({"critic_cfo": critique_for(["TKT-4477"])})  # only one of four
    result = llm_stages.critique(client, ordering, clusters, [], "situation", ctx.charter)
    assert len(result.value) == 1
    assert result.degraded
    assert "3 of 4 critics failed" in result.reason


def test_all_stages_unavailable_still_produces_a_complete_report(ctx):
    """The deterministic core is not just the foundation, it is the failure mode."""
    from triage.pipeline import RunOptions, run_batch

    report = run_batch(ctx, RunOptions(posture="revenue_first", use_llm=False, persist_state=False))
    assert len(report.ordering.ranked) == len(ctx.batch.tickets)
    assert report.degraded_stages
    assert report.ordering.overrides           # the charter still fired
    assert report.regret                       # counterfactuals are deterministic
    assert report.critiques == []              # honestly empty rather than fabricated


def test_client_is_unavailable_without_a_key():
    assert not LLMClient(api_key="").available


def test_a_keyless_client_raises_rather_than_answering():
    """The only two outcomes are a real model response and an exception.

    There is no third path -- no cache, no recorded reply, no stand-in object -- so a
    caller cannot receive something that looks like an answer and is not one.
    """
    with pytest.raises(LLMUnavailable):
        LLMClient(api_key="").structured(
            stage="conflicts", system="s", user="u", schema=ConflictResult,
            tool_name="t", tool_description="d",
        )


# --------------------------------------------------------------------- stubs


def _stub_critique():
    from triage.models import Critique

    return Critique(
        critic="fairness_campaigner", posture_voice="fairness_first",
        objection="o", specific_tickets=[], strongest_point="s",
        what_it_would_cost_to_agree="c",
    )
