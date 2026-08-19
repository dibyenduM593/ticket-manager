"""The pipeline. Stages 1-11 from the design.

     1  ingest, sanitise, normalise            deterministic
     2  context gathering (4 sources)          deterministic + 1 LLM extraction
     3  correlation - cluster common causes    deterministic + 1 LLM
     4  conflict detection                     1 LLM
     5  posture advice from company state      1 LLM  (+ human confirmation)
     6  scoring, capacity aware                deterministic
     7  charter enforcement                    deterministic
     8  adversarial critique                   4 LLM
     9  adjudication and revision              1 LLM
    10  report render                          template + 1 LLM narrative
    11  state update                           deterministic

On API error after retries the pipeline completes using the deterministic core alone
and the report states plainly which stages did not run. Stages whose output is a
computation over the input -- extraction, correlation, conflict detection -- keep
producing it. Stages whose output is judgement -- advice, critique, adjudication,
narrative -- produce NOTHING rather than a stand-in, and a run with no posture named
and no advice available stops instead of choosing the company's values for it.

The deterministic core is not just the foundation -- it is the failure mode.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import charter as charter_mod
from . import correlate, estimation, ingest, paths, scorer
from .config import load_charter, load_company_state, load_posture, load_postures
from .models import (
    Batch,
    BatchReport,
    Cluster,
    CompanyState,
    Conflict,
    Escalation,
    Ordering,
    PendingTicket,
    Posture,
    PostureAdvice,
    RankedTicket,
    RegretRow,
    Revision,
    Ticket,
    TicketEstimate,
)
from .sources import SourceBundle
from .state import State


class NoPostureChosen(RuntimeError):
    """No posture was named and no advisor ran to suggest one.

    Raised rather than defaulting. A posture is the one input this system will not
    supply on the user's behalf, because the weights ARE the ethics.
    """


def company_state_for(batch_id: int) -> Path:
    """Per-batch company state, falling back to the default.

    The situation is an INPUT, and it moves: by batch 44 the incident is closed, load
    is back to normal, and NorthPeak's renewal is a week nearer. If company state were
    frozen the posture advisor would be answering a question about Wednesday lunchtime
    on Thursday morning, which is the sort of quiet staleness that makes a demo lie.
    """
    specific = paths.config_dir() / f"company_state_batch_{batch_id}.yaml"
    return specific if specific.exists() else paths.config_dir() / "company_state.yaml"


class RunOptions:
    """Everything that changes how a run behaves, in one object."""

    def __init__(
        self,
        posture: str | None = None,
        use_llm: bool = True,
        assume_yes: bool = False,
        persist_state: bool = True,
        confirm: Callable[[PostureAdvice, dict[str, Posture]], str] | None = None,
    ) -> None:
        self.posture = posture
        self.use_llm = use_llm
        self.assume_yes = assume_yes
        self.persist_state = persist_state
        self.confirm = confirm


class Context:
    """Loaded inputs for one run. Assembling this is stages 1 and 2."""

    def __init__(
        self,
        batch: Batch,
        company: CompanyState,
        sources: SourceBundle,
        state: State,
    ) -> None:
        self.batch = batch
        self.company = company
        self.sources = sources
        self.state = state
        # The batch declares its own moment when it has one; company_state supplies
        # the default. Batch 43 happens four hours after batch 42, and the fairness
        # ledger has to notice.
        self.as_of: datetime = batch.as_of or company.as_of
        self.capacity: int = company.operations.agents_available
        self.charter: dict[str, Any] = load_charter()
        #: Ticket ids dragged in from the waiting room rather than declared by the
        #: batch. Reported so an old ticket never appears in a ranking unexplained.
        self.carried: list[str] = []

    @classmethod
    def build(
        cls,
        batch: Batch,
        company_path: Path | str | None = None,
        state_root: Path | str | None = None,
        sources: SourceBundle | None = None,
        carry_backlog: bool = False,
    ) -> "Context":
        """Assemble a Context from a batch that is already in memory.

        THE single place a run is set up, whether the tickets came off disk or out of
        the web form. Sanitisation, state loading and carry-forward all happen here, so
        a second entry point cannot quietly skip one of them -- which is exactly what
        had happened: the demo server hand-built its Context and thereby skipped the
        injection filter, the per-batch company state, and the backlog entirely.

        `carry_backlog` defaults to FALSE, and that is not timidity. `triage eval`
        measures conflict recall against conflicts planted in one file, and `triage
        stability` exists to confirm the harness measures the pipeline and NOT the
        ledger -- ten runs that each dragged in a growing backlog would drift by
        construction. Both must see the batch exactly as authored. `triage run` and the
        web form, which are making real decisions in a sequence, pass True.
        """
        batch = ingest.sanitise_batch(batch)
        state = State.load(state_root)
        base = sources or SourceBundle.load()
        carried: list[str] = []

        if carry_backlog:
            waiting = state.carry_forward([t.id for t in batch.tickets])
            if waiting:
                carried = [e.ticket.id for e in waiting]
                batch = batch.model_copy(
                    update={"tickets": [e.ticket for e in waiting] + list(batch.tickets)}
                )
                # Live rows win; snapshots only fill the gap a typed-in ticket leaves,
                # since it never had instruments or a billing record of its own.
                base = SourceBundle.merged(
                    SourceBundle(
                        telemetry={
                            e.ticket.id: e.telemetry_snapshot
                            for e in waiting if e.telemetry_snapshot is not None
                        },
                        crm={
                            e.ticket.customer_id: e.crm_snapshot
                            for e in waiting if e.crm_snapshot is not None
                        },
                    ),
                    telemetry=base.telemetry,
                    crm=base.crm,
                )

        ctx = cls(
            batch=batch,
            company=load_company_state(company_path or company_state_for(batch.batch_id)),
            sources=base,
            state=state,
        )
        ctx.carried = carried
        return ctx

    @classmethod
    def load(
        cls,
        batch_path: Path | str,
        company_path: Path | str | None = None,
        state_root: Path | str | None = None,
        carry_backlog: bool = False,
    ) -> "Context":
        """Assemble a Context from a batch file. `parse_batch`, not `load_batch`:
        `build` sanitises, and doing it twice would rewrite already-redacted text."""
        return cls.build(
            ingest.parse_batch(batch_path),
            company_path=company_path,
            state_root=state_root,
            carry_backlog=carry_backlog,
        )

    def estimates(self, intensities: dict[str, float] | None = None) -> list[TicketEstimate]:
        """Stage 2. Every ticket, fully described in natural units."""
        self.state.see_tickets(
            [t.id for t in self.batch.tickets],
            self.batch.batch_id,
            {t.id: t.submitted_at for t in self.batch.tickets},
        )
        return [
            estimation.estimate_ticket(
                ticket=t,
                telemetry=self.sources.telemetry_for(t.id),
                crm=self.sources.crm_for(t.customer_id),
                state=self.state,
                as_of=self.as_of,
                urgency_intensity=(intensities or {}).get(t.id),
            )
            for t in self.batch.tickets
        ]

    def ticket(self, ticket_id: str) -> Ticket:
        for t in self.batch.tickets:
            if t.id == ticket_id:
                return t
        raise KeyError(ticket_id)

    def summary_line(self) -> str:
        o, b, c = self.company.operations, self.company.business, self.company.commercial
        parts = [
            f"system load {o.system_load:.0%}",
            f"{o.agents_available} agents against a backlog of {o.backlog_depth}",
            f"strategic priority {b.strategic_priority}",
            f"churn pressure {b.churn_pressure}",
            f"{b.runway_months} months runway",
        ]
        if b.funding_event:
            parts.append(f"{b.funding_event.type} in {b.funding_event.weeks_away} weeks")
        if o.active_incident:
            parts.append(f"active incident: {o.incident_summary}")
        if c.accounts_at_renewal:
            parts.append(f"at renewal: {', '.join(c.accounts_at_renewal)}")
        if c.sla_breaches_this_month:
            parts.append(f"{c.sla_breaches_this_month} SLA breaches this month")
        return "; ".join(parts)


# ------------------------------------------------------------- deterministic run


def run_deterministic(
    ctx: Context,
    posture: Posture,
    clusters: list[Cluster] | None = None,
    intensities: dict[str, float] | None = None,
) -> tuple[Ordering, list[Cluster], list[TicketEstimate]]:
    """Stages 2, 3 (fallback), 6 and 7 with no LLM anywhere in the call graph.

    This is the whole system's floor. It produces a defensible ordering, a charter
    override where one is owed, and a one-line justification per ticket.
    """
    estimates = ctx.estimates(intensities)
    if clusters is None:
        clusters = correlate.cluster_by_signature(
            [t.id for t in ctx.batch.tickets], ctx.sources.telemetry
        )
    scored = scorer.score_tickets(estimates, posture, clusters, ctx.charter)
    ordering = scorer.rank(scored, posture, ctx.capacity, ctx.charter)
    return ordering, clusters, estimates


def order_under_all_postures(
    ctx: Context,
    clusters: list[Cluster] | None = None,
    intensities: dict[str, float] | None = None,
) -> dict[str, Ordering]:
    """Score under every posture. Free -- the counterfactual regret table is just a
    diff over these, and `compare` renders them side by side."""
    out: dict[str, Ordering] = {}
    for name, posture in sorted(load_postures().items()):
        working = _fresh_context(ctx)
        ordering, _, _ = run_deterministic(working, posture, clusters, intensities)
        out[name] = ordering
    return out


def _fresh_context(ctx: Context) -> Context:
    """A context whose state is a clone, so scoring under six postures cannot leave
    six batches' worth of ledger mutations behind."""
    return Context(ctx.batch, ctx.company, ctx.sources, ctx.state.clone())


# --------------------------------------------------------- deterministic stages


def detect_conflicts_deterministic(estimates: list[TicketEstimate]) -> list[Conflict]:
    """Fallback conflict detection: rule-based, and deliberately narrow.

    It finds the contradictions that are arithmetic (a stated urgency far above the
    measured one, a declared blocking flag telemetry does not support, a credibility
    gap). It will not find the interesting ones -- that is the LLM stage's job, and
    the gap between the two is reported honestly in the eval.
    """
    out: list[Conflict] = []
    for e in estimates:
        if e.claimed_urgency_value - e.blast_radius >= 0.45:
            out.append(
                Conflict(
                    ticket_id=e.ticket_id,
                    source_a="ticket",
                    claim_a=f"merchant states urgency '{e.stated_urgency.value}'",
                    source_b="telemetry",
                    claim_b=f"{e.users_affected} users affected, {e.error_rate:.0%} error rate",
                    trusted="telemetry",
                    reasoning=(
                        "Stated urgency is unsupported by any independent measurement. "
                        f"Account credibility is {e.credibility:.2f} ({e.credibility_evidence})."
                    ),
                    severity_of_conflict="medium",
                )
            )
        if e.blast_radius - e.claimed_urgency_value >= 0.35:
            out.append(
                Conflict(
                    ticket_id=e.ticket_id,
                    source_a="ticket",
                    claim_a=f"merchant states urgency '{e.stated_urgency.value}'",
                    source_b="telemetry",
                    claim_b=f"blast radius {e.blast_radius:.2f} across {e.users_affected} users",
                    trusted="telemetry",
                    reasoning=(
                        "The merchant is understating this. Evidence outranks the claim in "
                        "both directions -- a low stated urgency is discounted no differently "
                        "from a high one."
                    ),
                    severity_of_conflict="high",
                )
            )
        if e.blocks_paying_workflow_claimed and not e.blocks_paying_workflow_observed:
            out.append(
                Conflict(
                    ticket_id=e.ticket_id,
                    source_a="ticket",
                    claim_a="blocks_paying_workflow declared true by the merchant",
                    source_b="telemetry",
                    claim_b=f"${e.gmv_at_risk_per_hour:,.0f}/h at risk, no checkout degradation observed",
                    trusted="neither, fully",
                    reasoning=(
                        "The declared flag is unsupported but not disproven; instruments "
                        "cannot distinguish a blocked buyer from one who changed their mind. "
                        "Scored on measured impact, flagged for a human."
                    ),
                    severity_of_conflict="medium",
                )
            )
        if e.security_exposure or e.data_loss:
            out.append(
                Conflict(
                    ticket_id=e.ticket_id,
                    source_a="crm",
                    claim_a=f"{e.tier.value} tier, ${e.arr:,.0f} ARR",
                    source_b="telemetry",
                    claim_b="confirmed security exposure or data loss",
                    trusted="telemetry",
                    reasoning="Account value has no bearing on a charter-protected condition.",
                    severity_of_conflict="high",
                )
            )
    return out


# ------------------------------------------------------------------- regret table


def regret_table(
    chosen: str,
    orderings: dict[str, Ordering],
    limit_per_posture: int = 2,
) -> list[RegretRow]:
    """Counterfactual regret. Free, because the scorer already ran under every posture.

    Makes "maximising one metric harms another" numeric instead of rhetorical.
    """
    base = orderings[chosen]
    rows: list[RegretRow] = []

    for name, other in sorted(orderings.items()):
        if name == chosen:
            continue
        diffs = []
        for r in other.ranked:
            try:
                here, there = r.rank, base.rank_of(r.ticket_id)
            except KeyError:
                continue
            if here != there:
                diffs.append((abs(here - there), r.ticket_id, here, there, r))
        diffs.sort(key=lambda d: (-d[0], d[1]))
        for _, tid, here, there, r in diffs[:limit_per_posture]:
            rows.append(
                RegretRow(
                    posture=name,
                    ticket_id=tid,
                    rank_here=here,
                    rank_under_chosen=there,
                    cost_sentence=_regret_sentence(r, here, there, base.capacity),
                )
            )
    return rows


def _regret_sentence(r, here: int, there: int, capacity: int) -> str:
    est = r.scored.estimate
    served_here, served_there = here <= capacity, there <= capacity
    direction = "earlier" if here < there else "later"
    delta = abs(here - there)

    if served_here and not served_there:
        return (
            f"{est.customer_id} is served this batch instead of waiting; "
            f"costs a served slot to whoever is displaced"
        )
    if served_there and not served_here:
        detail = []
        if est.gmv_at_risk_per_hour:
            detail.append(f"${est.gmv_at_risk_per_hour:,.0f}/h keeps bleeding")
        if est.security_exposure or est.data_loss:
            detail.append("a confirmed exposure stays open")
        if est.times_skipped:
            detail.append(f"a {est.times_skipped + 1}th consecutive skip")
        return (
            f"{est.customer_id} is deferred instead of served"
            + (f" - {', '.join(detail)}" if detail else "")
        )
    return f"{est.customer_id} moves {delta} position{'s' if delta > 1 else ''} {direction}; both sides of the capacity line unchanged"


# ------------------------------------------------------------------- escalations


def escalations(estimates: list[TicketEstimate]) -> list[Escalation]:
    """Tickets the system declines to decide on.

    Refusing to rank something is a legitimate output. Every entry here names what
    evidence would resolve it, so it is an escalation rather than a shrug.
    """
    out: list[Escalation] = []
    for e in estimates:
        if e.blocks_paying_workflow_claimed and not e.blocks_paying_workflow_observed:
            out.append(
                Escalation(
                    ticket_id=e.ticket_id,
                    why=(
                        "Merchant declares a paying workflow is blocked; instruments show "
                        f"${e.gmv_at_risk_per_hour:,.0f}/h at risk and no degradation. The "
                        "system cannot distinguish an unreproducible bug from a mistaken report."
                    ),
                    what_would_resolve_it="A session replay or one reproduction from the merchant's side.",
                )
            )
        if e.category_n == 0:
            out.append(
                Escalation(
                    ticket_id=e.ticket_id,
                    why=f"No resolution history for category '{e.category}' - the severe-rate "
                        "and expected-duration terms are priors, not measurements.",
                    what_would_resolve_it="Any resolved ticket in this category.",
                )
            )
        if e.security_exposure or e.data_loss:
            out.append(
                Escalation(
                    ticket_id=e.ticket_id,
                    why="Confirmed exposure. Ranking is a triage decision; disclosure "
                        "obligations are not, and this system has no view on those.",
                    what_would_resolve_it="Security and legal review, in parallel with the fix.",
                )
            )
    return out


# ---------------------------------------------------------------- adjudication


def apply_revisions(
    ordering: Ordering,
    revisions: list[Revision],
    capacity: int,
    charter: dict[str, Any] | None = None,
) -> Ordering:
    """Apply the adjudicator's mind-change, then re-enforce the charter.

    The re-enforcement is not a formality. An adjudicator persuaded by the CFO could
    move a revenue ticket up and push a confirmed exposure past the capacity line; the
    charter catches that and the report shows both moves. Values may be argued with.
    Floors may not.
    """
    if not revisions:
        return ordering

    items: list[RankedTicket] = [r.model_copy(deep=True) for r in ordering.ranked]
    for rev in revisions:
        current = next((i for i, it in enumerate(items) if it.ticket_id == rev.ticket_id), None)
        if current is None:
            continue
        item = items.pop(current)
        target = max(0, min(len(items), rev.to_rank - 1))
        items.insert(target, item)

    for i, item in enumerate(items):
        item.rank = i + 1
        item.served = i < capacity

    result = charter_mod.enforce_full(items, capacity, charter or load_charter())
    return Ordering(
        posture=ordering.posture,
        ranked=result.ranked,
        # Overrides from the original pass are kept: they are part of the history of
        # this ordering even when a later move made them moot.
        overrides=ordering.overrides + result.overrides,
        capacity=capacity,
        oversubscribed=result.oversubscribed or ordering.oversubscribed,
    )


# ------------------------------------------------------------------- full run


def run_batch(ctx: Context, options: RunOptions) -> BatchReport:
    """The report-only view of `run_batch_full`. What every CLI command calls."""
    report, _ = run_batch_full(ctx, options)
    return report


def run_batch_full(ctx: Context, options: RunOptions) -> tuple[BatchReport, dict[str, Ordering]]:
    """Stages 1-11, plus the per-posture counterfactual orderings.

    The orderings are computed here anyway -- the regret table is a diff over them --
    and were being discarded. The demo front end needs all six to show postures side by
    side, and rescoring afterwards would score against post-persist state and quietly
    print different numbers than the ones the decision was made on.

    Ordering of operations matters and is deliberate:
      extraction -> correlation -> deterministic pass -> conflicts -> advice ->
      human confirmation -> real scoring -> charter -> critique -> adjudication ->
      charter again -> report -> state.

    Conflicts and advice are computed against a PROVISIONAL ordering under `balanced`,
    because the advisor needs to see what the batch looks like before a posture has
    been chosen -- otherwise the recommendation is arguing for a ranking it already saw.
    """
    from .llm.client import LLMClient
    from .llm import stages as llm_stages

    client = LLMClient()
    if not options.use_llm:
        client.api_key = None

    degraded: list[str] = []

    def note(result) -> Any:
        if result.degraded and result.reason:
            degraded.append(result.reason)
        return result.value

    # -- stage 2: extraction ---------------------------------------------------
    extraction = note(llm_stages.extract_urgency(client, ctx.batch.tickets, ctx.charter))
    intensities = extraction["intensities"]
    estimates = ctx.estimates(intensities)

    # -- stage 3: correlation --------------------------------------------------
    clusters = note(
        llm_stages.correlate_tickets(
            client, ctx.batch.tickets, estimates, ctx.charter, ctx.sources.telemetry
        )
    )

    # -- stage 4: conflicts ----------------------------------------------------
    conflicts = note(
        llm_stages.detect_conflicts(
            client, ctx.batch.tickets, estimates, ctx.charter,
            detect_conflicts_deterministic(estimates),
        )
    )

    # -- stage 5: posture advice, then a human ---------------------------------
    advice = note(
        llm_stages.advise_posture(
            client, ctx.summary_line(), load_postures(), ctx.charter,
            ctx.batch.tickets, estimates,
        )
    )

    postures = load_postures()
    if options.posture:
        chosen, chosen_by = options.posture, "human (--posture)"
    elif advice is None:
        # There is no advice and no posture was named. Defaulting to `balanced` here
        # would be this file choosing the company's ethics and labelling it neutral --
        # equal weights are a value judgement too. Stop instead.
        raise NoPostureChosen(
            "no posture was given and the advisor did not run, so there is nothing to "
            "confirm. Pass --posture NAME (one of: " + ", ".join(sorted(postures)) + ")."
        )
    elif options.assume_yes or options.confirm is None:
        chosen, chosen_by = advice.recommended, f"agent recommendation, auto-confirmed ({advice.source})"
    else:
        chosen = options.confirm(advice, postures)
        chosen_by = "human (confirmed at the prompt)"
    posture = load_posture(chosen)

    # -- stages 6 and 7: score, then the charter -------------------------------
    scored = scorer.score_tickets(estimates, posture, clusters, ctx.charter)
    ordering = scorer.rank(scored, posture, ctx.capacity, ctx.charter)
    before_adjudication = list(ordering.ticket_order)

    # -- stage 8: four critics -------------------------------------------------
    critiques = note(
        llm_stages.critique(
            client, ordering, clusters, conflicts, ctx.summary_line(), ctx.charter
        )
    )

    # -- stage 9: revise or defend, then the charter again ---------------------
    adjudication = note(llm_stages.adjudicate(client, ordering, critiques, ctx.charter))
    if adjudication and adjudication.revisions:
        ordering = apply_revisions(ordering, adjudication.revisions, ctx.capacity, ctx.charter)

    # -- counterfactuals -------------------------------------------------------
    all_orderings = order_under_all_postures(ctx, clusters, intensities)
    all_orderings[posture.name] = ordering
    regret = regret_table(posture.name, all_orderings)

    # -- stage 10: narrative ---------------------------------------------------
    narrative = note(
        llm_stages.narrate(client, ordering, ctx.summary_line(), conflicts, adjudication, ctx.charter)
    )

    report = BatchReport(
        batch_id=ctx.batch.batch_id,
        label=ctx.batch.label,
        as_of=ctx.as_of,
        posture=posture.name,
        posture_chosen_by=chosen_by,
        company_state_summary=ctx.summary_line(),
        advice=advice,
        clusters=[c for c in clusters if len(c.ticket_ids) > 1],
        conflicts=conflicts,
        ordering=ordering,
        ordering_before_adjudication=before_adjudication,
        critiques=critiques,
        adjudication=adjudication,
        regret=regret,
        escalations=escalations(estimates) + _oversubscription_escalations(ordering),
        sanitisation_events=ingest.sanitisation_events(ctx.batch),
        degraded_stages=degraded,
        narrative=narrative,
    )

    # -- stage 11: state -------------------------------------------------------
    if options.persist_state:
        apply_batch_outcome(ctx, ordering, estimates)
        ctx.state.save()
        append_decision_log(report)

    return report, all_orderings


def _oversubscription_escalations(ordering: Ordering) -> list[Escalation]:
    """The charter requiring more service than there is capacity for is not a bug to
    be resolved by picking a favourite. It is a staffing fact, and a human owns it."""
    if not ordering.oversubscribed:
        return []
    return [
        Escalation(
            ticket_id=tid,
            why=(
                f"{len(ordering.oversubscribed)} tickets carry charter-protected "
                f"conditions but only {ordering.capacity} agents are available. The "
                "charter cannot invent capacity."
            ),
            what_would_resolve_it="An additional agent this batch, or an explicit human decision about which protection yields.",
        )
        for tid in ordering.oversubscribed
    ]


# ------------------------------------------------------------------ state update


def next_ad_hoc_batch(state: State, step_hours: float = 8.0) -> tuple[int, datetime]:
    """The id and moment for a batch that did not come from a file.

    Reads how far the sequence has got and returns the next slot. Without this an
    ad-hoc batch has to invent both -- and a hardcoded id collides with the previous
    submission's ledger rows, while a frozen clock means waiting debt can never grow,
    so the 120h ceiling could never be crossed no matter how many times you submitted.
    """
    fallback = load_company_state(paths.config_dir() / "company_state.yaml").as_of
    base = state.meta.as_of or fallback
    return state.meta.last_batch_id + 1, base + timedelta(hours=step_hours)


def apply_batch_outcome(
    ctx: Context,
    ordering: Ordering,
    estimates: list[TicketEstimate],
) -> tuple[list[str], list[str]]:
    """Stage 11. Deterministic, and the only path by which state/ ever changes.

    Fairness debt accrues only for tickets that were actually skipped -- ranks past
    the capacity line. Without that, `times_skipped` would increment off a number
    grounded in nothing.
    """
    served, deferred = scorer.capacity_split(ordering)

    by_id = {e.ticket_id: e for e in estimates}
    for tid in served:
        e = by_id[tid]
        ticket = ctx.ticket(tid)
        if e.claimed_urgency_value >= 0.75:
            ctx.state.record_observation(
                customer_id=e.customer_id,
                ticket_id=tid,
                claimed=ticket.stated_urgency,
                verdict=_verdict_from_evidence(e),
                batch_id=ctx.batch.batch_id,
            )

    ctx.state.accrue_skips(deferred, ctx.batch.batch_id)
    # Keep the deferred tickets whole, so the next batch can carry them rather than
    # relying on someone re-declaring them in a fixture by hand. Snapshots are taken
    # only as a fallback for tickets with no live row -- see PendingTicket.
    ctx.state.defer_tickets([
        PendingTicket(
            ticket=ctx.ticket(tid),
            deferred_in_batch=ctx.batch.batch_id,
            telemetry_snapshot=ctx.sources.telemetry.get(tid),
            crm_snapshot=ctx.sources.crm.get(ctx.ticket(tid).customer_id),
        )
        for tid in deferred
    ])
    ctx.state.retire_served(served)
    ctx.state.advance(ctx.batch.batch_id, ctx.as_of)
    return served, deferred


def _verdict_from_evidence(e: TicketEstimate) -> bool:
    """Was this urgency claim genuine?

    Mirrors estimation.was_actually_severe, using the live evidence available at
    triage time in place of the post-hoc resolution facts: more than ten users hit,
    or a charter-protected condition. Deliberately NOT the model's opinion -- an LLM
    may propose an observation as a constrained enum, but only code writes to state/.
    """
    return e.users_affected > 10 or e.security_exposure or e.data_loss


# ---------------------------------------------------------------- decision log


def append_decision_log(report: BatchReport, path: Path | None = None) -> None:
    """Append-only record of every decision with its inputs and counterfactuals.

    Powers `triage why`, and is the substrate for the contextual-bandit future work
    described in the README.
    """
    p = path or paths.decision_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for r in report.ordering.ranked:
            fh.write(
                json.dumps(
                    {
                        "batch_id": report.batch_id,
                        "as_of": report.as_of.isoformat(),
                        "posture": report.posture,
                        "posture_chosen_by": report.posture_chosen_by,
                        "ticket_id": r.ticket_id,
                        "customer_id": r.customer_id,
                        "rank": r.rank,
                        "served": r.served,
                        "score": round(r.score, 6),
                        "components": r.scored.components.as_dict(),
                        "severity": r.scored.severity.model_dump(mode="json"),
                        "estimate": r.scored.estimate.model_dump(mode="json"),
                        "charter_promoted": r.charter_promoted,
                        "cluster_id": r.scored.cluster_id,
                        "justification": r.justification,
                        "counterfactuals": [
                            row.model_dump(mode="json")
                            for row in report.regret
                            if row.ticket_id == r.ticket_id
                        ],
                    },
                    sort_keys=True,
                )
                + "\n"
            )
