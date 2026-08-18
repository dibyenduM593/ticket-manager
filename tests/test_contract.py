"""Layer B -- the LLM contract.

What must hold no matter what the model returns:

  * the output validates against the schema (forced tool use, so this is structural)
  * every ticket appears exactly once in the final ordering
  * every cited ticket ID exists            <- the hallucinated-entity test
  * every cited source name is a real source
  * a malformed or hostile response degrades to the deterministic core, loudly

These run against a programmable double, and against cassettes when they exist.
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

from triage import paths
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


def test_advice_naming_an_unknown_posture_falls_back(ctx, estimates):
    client = FakeLLMClient({"advisor": advice_for("maximise_shareholder_value", ["balanced"])})
    fallback = _stub_advice()
    result = llm_stages.advise_posture(
        client, "situation", load_postures(), ctx.charter, ctx.batch.tickets, estimates, fallback
    )
    assert result.degraded
    assert "unknown posture" in result.reason
    assert result.value.recommended == fallback.recommended


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


def test_client_reports_unavailable_without_key_or_cassettes(tmp_path):
    client = LLMClient(api_key="", cassette_mode="replay", cassette_dir=tmp_path / "missing")
    assert not client.available


def test_replay_mode_never_touches_the_network(tmp_path):
    client = LLMClient(api_key="", cassette_mode="replay", cassette_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(LLMUnavailable):
        client.structured(
            stage="conflicts", system="s", user="u", schema=ConflictResult,
            tool_name="t", tool_description="d",
        )


# ------------------------------------------------------------------ cassettes


def _cassette_stages() -> list[str]:
    d = paths.cassette_dir()
    if not d.exists():
        return []
    return [p.name for p in d.iterdir() if p.is_dir() and any(p.glob("*.json"))]


@pytest.mark.skipif(not _cassette_stages(), reason="no cassettes recorded yet")
def test_recorded_cassettes_still_satisfy_their_schemas():
    """Guards against a schema change silently invalidating the replay path -- which
    is the reviewer's zero-setup quickstart, so it breaking would be expensive."""
    import json

    schema_for = {
        "conflicts": ConflictResult,
        "correlate": CorrelationResult,
        "advisor": AdviceResult,
    }
    for stage in _cassette_stages():
        schema = schema_for.get(stage)
        if schema is None:
            continue
        for path in (paths.cassette_dir() / stage).glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))["response"]
            schema.model_validate(payload)


# --------------------------------------------------------------------- stubs


def _stub_critique():
    from triage.models import Critique

    return Critique(
        critic="fairness_campaigner", posture_voice="fairness_first",
        objection="o", specific_tickets=[], strongest_point="s",
        what_it_would_cost_to_agree="c",
    )


def _stub_advice():
    from triage.models import PostureAdvice

    return PostureAdvice(
        recommended="balanced", reasoning="r", what_it_costs="c",
        ranked_alternatives=[], source="fallback",
    )
