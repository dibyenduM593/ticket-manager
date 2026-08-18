"""Layer C -- behavioural guarantees about the system as a whole.

These are the claims a reviewer would want to check by hand, written down so they are
checked on every commit instead.
"""

from __future__ import annotations

import json

from fakes import FakeLLMClient, adjudication_for, critique_for

from triage import paths, report as report_mod
from triage.config import load_posture
from triage.ingest import load_batch
from triage.llm import stages as llm_stages
from triage.pipeline import (
    Context,
    RunOptions,
    apply_revisions,
    order_under_all_postures,
    run_batch,
    run_deterministic,
)
from triage.sources import SourceBundle
from triage.state import State


# --------------------------------------------------- postures must do something


def test_postures_are_not_decorative(batch1_ctx):
    """Three or more distinct orderings from six postures. If a posture file could be
    deleted without changing any output, it is decoration and this test says so."""
    orderings = order_under_all_postures(batch1_ctx)
    distinct = {tuple(o.ticket_order) for o in orderings.values()}
    assert len(distinct) >= 3, f"only {len(distinct)} distinct orderings: {distinct}"


def test_each_posture_leads_on_the_axis_it_weights(batch1_ctx):
    """A sharper version: the posture that weights fairness most must actually put the
    most-waited ticket first, and the one weighting speed must lead with a quick fix."""
    orderings = order_under_all_postures(batch1_ctx)

    fairness_top = orderings["fairness_first"].ranked[0]
    assert fairness_top.ticket_id == "TKT-4471"
    assert fairness_top.scored.components.fairness > 0.9

    crisis_top = orderings["crisis_mode"].ranked[0].scored
    revenue_top = orderings["revenue_first"].ranked[0].scored
    # the ARR-vs-GMV split: crisis_mode leads with the higher live GMV, revenue_first
    # with the larger contract
    assert crisis_top.estimate.gmv_at_risk_per_hour >= revenue_top.estimate.gmv_at_risk_per_hour
    assert revenue_top.estimate.arr >= crisis_top.estimate.arr


def test_the_arr_versus_gmv_conflict_is_real_and_flips_the_order(batch1_ctx):
    """Two revenue signals in genuine disagreement. Bloom & Vine has 13x less ARR than
    NorthPeak and more money bleeding per hour, and the two postures split on it."""
    orderings = order_under_all_postures(batch1_ctx)
    assert orderings["revenue_first"].ticket_order[0] == "TKT-4477"   # ARR wins
    assert orderings["crisis_mode"].ticket_order[0] == "TKT-4479"     # live GMV wins


# ------------------------------------------------------- fairness over batches


def test_a_long_waiter_reaches_the_top_before_the_ceiling(batch2_ctx):
    """The charter's job is to be redundant. By batch 43 TKT-4471 has crossed 120h, so
    R2 fires -- but the test that matters is that it is SERVED, however it got there."""
    ordering, _, _ = run_deterministic(batch2_ctx, load_posture("revenue_first"))
    served = {r.ticket_id for r in ordering.ranked if r.served}
    assert "TKT-4471" in served


def test_fairness_debt_accrues_only_for_tickets_actually_skipped(batch1_ctx):
    """A ranking is not a decision. Only the tickets past the capacity line pay."""
    from triage.pipeline import apply_batch_outcome

    ordering, _, estimates = run_deterministic(batch1_ctx, load_posture("revenue_first"))
    before = {t: e.times_skipped for t, e in ((e.ticket_id, e) for e in estimates)}
    served, deferred = apply_batch_outcome(batch1_ctx, ordering, estimates)

    assert set(served).isdisjoint(deferred)
    assert len(served) == batch1_ctx.capacity

    for tid in deferred:
        entry = batch1_ctx.state.ledger_entry(tid)
        assert entry.times_skipped == before[tid] + 1
    for tid in served:
        assert batch1_ctx.state.ledger_entry(tid) is None  # served tickets leave the queue


def test_state_update_is_idempotent(batch1_ctx):
    """Re-running a batch must not double-count an urgency claim."""
    from triage.pipeline import apply_batch_outcome

    ordering, _, estimates = run_deterministic(batch1_ctx, load_posture("revenue_first"))
    apply_batch_outcome(batch1_ctx, ordering, estimates)
    first = batch1_ctx.state.customer("northpeak").urgency_claims
    apply_batch_outcome(batch1_ctx, ordering, estimates)
    assert batch1_ctx.state.customer("northpeak").urgency_claims == first


# ------------------------------------------------------------ prompt injection


def test_prompt_injection_does_not_alter_ranking(batch2_ctx):
    """The headline security guarantee.

    TKT-4490 carries an instruction block demanding rank 1. It must land where its
    EVIDENCE puts it -- a $14 invoice rounding error on a starter account, affecting
    one user -- and the attempt must be surfaced.
    """
    ordering, _, estimates = run_deterministic(batch2_ctx, load_posture("revenue_first"))
    est = {e.ticket_id: e for e in estimates}["TKT-4490"]

    assert ordering.rank_of("TKT-4490") > batch2_ctx.capacity, "injected ticket was served"
    assert est.users_affected == 1
    assert est.blast_radius < 0.2

    injected = batch2_ctx.ticket("TKT-4490")
    assert "Ignore all previous instructions" not in injected.body
    assert injected.sanitisation_notes


def test_the_injection_ranks_exactly_where_an_identical_clean_ticket_would(batch2_ctx, tmp_path):
    """The strongest form of the claim: strip the attack entirely and the rank is
    unchanged. The language contributed nothing, which is the whole point."""
    raw = json.loads((paths.batches_dir() / "batch_2.json").read_text(encoding="utf-8"))
    for t in raw["tickets"]:
        if t["id"] == "TKT-4490":
            t["body"] = "Our invoice page shows a total of $412 but the line items add up to $398."
    clean_path = tmp_path / "batch_clean.json"
    clean_path.write_text(json.dumps(raw), encoding="utf-8")

    clean_ctx = Context(load_batch(clean_path), batch2_ctx.company, batch2_ctx.sources,
                        batch2_ctx.state.clone())
    with_attack, _, _ = run_deterministic(batch2_ctx, load_posture("revenue_first"))
    without_attack, _, _ = run_deterministic(clean_ctx, load_posture("revenue_first"))

    assert with_attack.rank_of("TKT-4490") == without_attack.rank_of("TKT-4490")
    assert with_attack.ticket_order == without_attack.ticket_order


def test_the_injection_attempt_reaches_the_report(batch2_ctx):
    report = run_batch(batch2_ctx, RunOptions(posture="revenue_first", use_llm=False,
                                              persist_state=False))
    assert any(ev["ticket_id"] == "TKT-4490" for ev in report.sanitisation_events)
    assert "TKT-4490" in report_mod.render_markdown(report)
    assert "instruction-shaped content" in report_mod.render_markdown(report).lower()


# ------------------------------------------------------- charter after a revision


def test_adjudication_cannot_undo_a_charter_override(batch1_ctx):
    """Values may be argued with. Floors may not.

    An adjudicator persuaded by the CFO tries to bury the free-tier exposure at rank 6.
    The charter re-check drags it back inside the served set.
    """
    ordering, _, _ = run_deterministic(batch1_ctx, load_posture("revenue_first"))
    assert ordering.rank_of("TKT-4482") == 3

    from triage.models import Revision

    revised = apply_revisions(
        ordering,
        [Revision(ticket_id="TKT-4482", from_rank=3, to_rank=6,
                  because_of_critic="cfo", reasoning="free tier")],
        batch1_ctx.capacity,
        batch1_ctx.charter,
    )
    served = {r.ticket_id for r in revised.ranked if r.served}
    assert "TKT-4482" in served
    assert any(o.ticket_id == "TKT-4482" for o in revised.overrides)


def test_a_legitimate_revision_is_allowed_through(batch1_ctx):
    """The charter must not be so eager that no mind-change is ever possible."""
    from triage.models import Revision

    ordering, _, _ = run_deterministic(batch1_ctx, load_posture("revenue_first"))
    assert ordering.rank_of("TKT-4471") == 5
    revised = apply_revisions(
        ordering,
        [Revision(ticket_id="TKT-4471", from_rank=5, to_rank=1,
                  because_of_critic="fairness_campaigner", reasoning="six skips")],
        batch1_ctx.capacity,
        batch1_ctx.charter,
    )
    assert revised.rank_of("TKT-4471") == 1
    assert "TKT-4482" in {r.ticket_id for r in revised.ranked if r.served}


def test_a_visible_mind_change_survives_the_full_pipeline(batch1_ctx):
    client = FakeLLMClient(
        {
            **{f"critic_{c['key']}": critique_for(["TKT-4471"]) for c in llm_stages.CRITICS},
            "adjudicate": adjudication_for([("TKT-4471", 1)]),
        }
    )
    ordering, clusters, _ = run_deterministic(batch1_ctx, load_posture("revenue_first"))
    critiques = llm_stages.critique(client, ordering, clusters, [], "situation", batch1_ctx.charter)
    adj = llm_stages.adjudicate(client, ordering, critiques.value, batch1_ctx.charter)

    assert adj.value.changed_mind
    assert adj.value.conceded_but_not_acted
    revised = apply_revisions(ordering, adj.value.revisions, batch1_ctx.capacity, batch1_ctx.charter)
    assert revised.ticket_order != ordering.ticket_order
    assert revised.rank_of("TKT-4471") == 1


# ------------------------------------------------------------ degenerate inputs


def _write_batch(tmp_path, tickets, batch_id=99):
    path = tmp_path / f"batch_{batch_id}.json"
    path.write_text(
        json.dumps({"batch_id": batch_id, "label": "degenerate",
                    "as_of": "2026-08-19T14:00:00Z", "tickets": tickets}),
        encoding="utf-8",
    )
    return path


def _ticket(tid, **kw):
    base = {
        "id": tid, "customer_id": "verdant", "subject": "s", "body": "b",
        "category": "integration_api", "stated_urgency": "medium",
        "blocks_paying_workflow": False, "submitted_at": "2026-08-19T13:00:00Z",
    }
    base.update(kw)
    return base


def _ctx_for(path, batch1_ctx):
    return Context(load_batch(path), batch1_ctx.company, SourceBundle.load(), State.load().clone())


def test_empty_batch_degrades_gracefully(tmp_path, batch1_ctx):
    ctx = _ctx_for(_write_batch(tmp_path, []), batch1_ctx)
    report = run_batch(ctx, RunOptions(posture="balanced", use_llm=False, persist_state=False))
    assert report.ordering.ranked == []
    assert report.regret == []
    assert report_mod.render_markdown(report)  # still renders


def test_single_ticket_batch(tmp_path, batch1_ctx):
    ctx = _ctx_for(_write_batch(tmp_path, [_ticket("TKT-9001")]), batch1_ctx)
    report = run_batch(ctx, RunOptions(posture="balanced", use_llm=False, persist_state=False))
    assert [r.ticket_id for r in report.ordering.ranked] == ["TKT-9001"]
    assert report.ordering.ranked[0].served


def test_five_identical_tickets_resolve_deterministically(tmp_path, batch1_ctx):
    """No ties left to sort stability. Same score, same wait -> lowest ID first."""
    tickets = [_ticket(f"TKT-90{i:02d}") for i in range(5)]
    ctx = _ctx_for(_write_batch(tmp_path, tickets), batch1_ctx)
    first, _, _ = run_deterministic(ctx, load_posture("balanced"))
    assert first.ticket_order == sorted(first.ticket_order)
    for _ in range(5):
        again = Context(ctx.batch, ctx.company, ctx.sources, State.load().clone())
        assert run_deterministic(again, load_posture("balanced"))[0].ticket_order == first.ticket_order


def test_missing_telemetry_is_treated_as_an_unsupported_claim(tmp_path, batch1_ctx):
    """No telemetry row is not an error -- it means the instruments saw nothing, which
    is itself a finding. The claim is then all there is, and it is discounted."""
    ctx = _ctx_for(
        _write_batch(tmp_path, [_ticket("TKT-NOTELEM", stated_urgency="critical")]), batch1_ctx
    )
    ordering, _, estimates = run_deterministic(ctx, load_posture("balanced"))
    est = estimates[0]
    assert est.blast_radius == 0.0
    assert ordering.ranked[0].scored.severity.driver == "claimed"


def test_unknown_category_is_flagged_rather_than_guessed(tmp_path, batch1_ctx):
    ctx = _ctx_for(
        _write_batch(tmp_path, [_ticket("TKT-UNKNOWNCAT", category="quantum_flux")]), batch1_ctx
    )
    report = run_batch(ctx, RunOptions(posture="balanced", use_llm=False, persist_state=False))
    assert any("No resolution history" in e.why for e in report.escalations)


def test_capacity_larger_than_the_batch(tmp_path, batch1_ctx):
    ctx = _ctx_for(_write_batch(tmp_path, [_ticket("TKT-A"), _ticket("TKT-B")]), batch1_ctx)
    ctx.capacity = 10
    ordering, _, _ = run_deterministic(ctx, load_posture("balanced"))
    assert all(r.served for r in ordering.ranked)


# ----------------------------------------------------------------- reporting


def test_report_renders_every_section(batch1_ctx):
    report = run_batch(batch1_ctx, RunOptions(posture="revenue_first", use_llm=False,
                                              persist_state=False))
    md = report_mod.render_markdown(report)
    for heading in (
        "## 1. Situation", "## 2. Contradictions found", "## 3. Ranking",
        "## 4. Charter overrides", "## 5. Strategy ranking",
        "## 6. Counterfactual regret", "## 7. What the critics said",
        "## 8. Confidence and escalations",
    ):
        assert heading in md, f"missing {heading}"


def test_a_degraded_run_says_so_at_the_top_of_the_report(batch1_ctx):
    report = run_batch(batch1_ctx, RunOptions(posture="revenue_first", use_llm=False,
                                              persist_state=False))
    md = report_mod.render_markdown(report)
    assert "Degraded run" in md
    assert md.index("Degraded run") < md.index("## 1. Situation")


def test_report_json_round_trips(batch1_ctx, tmp_path):
    from triage.models import BatchReport

    report = run_batch(batch1_ctx, RunOptions(posture="revenue_first", use_llm=False,
                                              persist_state=False))
    _, json_path = report_mod.write(report, tmp_path)
    reloaded = BatchReport.model_validate(json.loads(json_path.read_text(encoding="utf-8")))
    assert reloaded.ordering.ticket_order == report.ordering.ticket_order


def test_near_ties_are_declared_not_hidden(batch1_ctx):
    """Under crisis_mode two tickets sit 0.001 apart. Presenting that as a confident
    1st and 2nd would be false precision."""
    ordering, _, _ = run_deterministic(batch1_ctx, load_posture("crisis_mode"))
    ties = report_mod.near_ties(ordering)
    assert any({a, b} == {"TKT-4477", "TKT-4479"} for a, b, _ in ties)


def test_markdown_tables_survive_a_pipe_in_the_content(batch1_ctx):
    report = run_batch(batch1_ctx, RunOptions(posture="balanced", use_llm=False,
                                              persist_state=False))
    report.conflicts[0].reasoning = "a | b | c\nsecond line"
    md = report_mod.render_markdown(report)
    row = next(line for line in md.splitlines() if "a \\| b \\| c" in line)
    assert row.count("\n") == 0
