"""The LLM stages. One function per stage, each with a deterministic fallback.

Every function here returns a StageResult so the caller can tell a real answer from a
fallback without inspecting the value. When a stage degrades, the reason travels to
the report -- a run that quietly used the fallback and looked identical to a full run
would be the most dishonest thing this system could do.
"""

from __future__ import annotations

from typing import Any

from .. import correlate as correlate_mod
from ..config import load_postures
from ..models import (
    Adjudication,
    Cluster,
    Conflict,
    Critique,
    Ordering,
    Posture,
    PostureAdvice,
    PostureRanking,
    Revision,
    Ticket,
    TicketEstimate,
)
from . import prompts
from .client import LLMClient, LLMUnavailable, StageResult
from .schemas import (
    AdjudicationResult,
    AdviceResult,
    ConflictResult,
    CorrelationResult,
    CritiqueResult,
    ExtractionResult,
    NarrativeResult,
)


# ------------------------------------------------------------- stage 2: extract


EXTRACT_ROLE = """\
You extract one number and two flags from support ticket text for a triage system.

You are measuring RHETORIC, not severity. Whether the problem is real is decided
elsewhere by telemetry, and your output is deliberately kept away from that decision.
A calm report of a catastrophe scores LOW. A furious report of a typo scores HIGH.
"""


def extract_urgency(
    client: LLMClient, tickets: list[Ticket], charter: dict[str, Any]
) -> StageResult:
    """Stage 2. Returns {ticket_id: intensity} plus injection/understatement flags."""
    from ..estimation import urgency_intensity_heuristic

    def fallback(reason: str) -> StageResult:
        return StageResult.fallback(
            {
                "intensities": {t.id: urgency_intensity_heuristic(t.body) for t in tickets},
                "injection_flags": {t.id: bool(t.sanitisation_notes) for t in tickets},
                "understating": {},
            },
            reason,
        )

    if not client.available:
        return fallback("no API key and no cassettes; used the keyword heuristic")

    try:
        result = client.structured(
            stage="extract",
            system=prompts.system_prompt(role=EXTRACT_ROLE, charter=charter),
            user=(
                "Score every ticket below. Return one entry per ticket, no more and no fewer.\n\n"
                + prompts.render_batch(tickets)
            ),
            schema=ExtractionResult,
            tool_name="record_urgency_extraction",
            tool_description="Record rhetorical urgency intensity and flags for each ticket.",
        )
    except LLMUnavailable as exc:
        return fallback(f"extraction call failed ({exc}); used the keyword heuristic")

    valid = {t.id for t in tickets}
    return StageResult.ok(
        {
            "intensities": {
                e.ticket_id: e.urgency_intensity for e in result.extractions if e.ticket_id in valid
            },
            "injection_flags": {
                e.ticket_id: e.injection_attempt for e in result.extractions if e.ticket_id in valid
            },
            "understating": {
                e.ticket_id: e.understating for e in result.extractions if e.ticket_id in valid
            },
        }
    )


# ----------------------------------------------------------- stage 3: correlate


CORRELATE_ROLE = """\
You group support tickets by suspected common root cause for a triage system.

Three merchants reporting checkout failures in one batch may be ONE platform incident
affecting three merchants, and the correct severity is then the aggregate rather than
three separate mid-severity readings.

You must cite the shared evidence -- identical error signature, same region, overlapping
time window, same subsystem. "They both mention payments" is not evidence. Returning an
empty list is a correct and common answer; a false cluster promotes unrelated tickets
together and is worse than no cluster at all.
"""


def correlate_tickets(
    client: LLMClient,
    tickets: list[Ticket],
    estimates: list[TicketEstimate],
    charter: dict[str, Any],
    telemetry: dict,
) -> StageResult:
    """Stage 3. Deterministic signature clustering, optionally extended by the model."""
    deterministic = correlate_mod.cluster_by_signature([t.id for t in tickets], telemetry)

    if not client.available:
        return StageResult.fallback(
            deterministic, "no API key and no cassettes; clustered on error signature only"
        )

    try:
        result = client.structured(
            stage="correlate",
            system=prompts.system_prompt(role=CORRELATE_ROLE, charter=charter),
            user=(
                "Group these tickets by suspected common root cause.\n\n"
                f"{prompts.render_batch(tickets, estimates)}\n\n"
                "Already grouped deterministically on identical error signature "
                f"(do not re-propose these): {[c.ticket_ids for c in deterministic] or 'none'}"
            ),
            schema=CorrelationResult,
            tool_name="record_clusters",
            tool_description="Record correlated ticket clusters and the evidence linking them.",
        )
    except LLMUnavailable as exc:
        return StageResult.fallback(
            deterministic, f"correlation call failed ({exc}); clustered on error signature only"
        )

    proposed = [
        Cluster(
            cluster_id=f"llm::{i}",
            ticket_ids=c.ticket_ids,
            root_cause=c.root_cause,
            shared_evidence=c.shared_evidence,
            confidence=c.confidence,
        )
        for i, c in enumerate(result.clusters)
    ]
    merged = correlate_mod.merge_llm_clusters(deterministic, proposed, {t.id for t in tickets})
    return StageResult.ok(merged)


# ------------------------------------------------------------ stage 4: conflicts


CONFLICTS_ROLE = """\
You find CONTRADICTIONS between independent sources of evidence about support tickets.

There are four sources, and they are independent in the sense that matters: no one can
be derived from another, and each is authored by a party with different incentives.

  ticket     what the merchant claims       subjective, inflatable
  telemetry  what the platform measured     indifferent to who filed it
  crm        what the account is worth      says nothing about severity
  history    what this account/category does over time

A conflict is two sources that cannot both be fully right about the same ticket. Your
job is to name them precisely and say which you trust and why.

Say "neither, fully" when that is the honest answer. A system that always picks a
winner is pretending to a certainty it does not have.
"""


def detect_conflicts(
    client: LLMClient,
    tickets: list[Ticket],
    estimates: list[TicketEstimate],
    charter: dict[str, Any],
    fallback_conflicts: list[Conflict],
) -> StageResult:
    """Stage 4. One call, all tickets, so cross-ticket contradictions are visible."""
    if not client.available:
        return StageResult.fallback(
            fallback_conflicts, "no API key and no cassettes; used rule-based conflict detection"
        )

    try:
        result = client.structured(
            stage="conflicts",
            system=prompts.system_prompt(role=CONFLICTS_ROLE, charter=charter),
            user=(
                "Find every contradiction between sources in this batch. Include "
                "contradictions BETWEEN tickets where two merchants on the same "
                "infrastructure describe incompatible situations.\n\n"
                + prompts.render_batch(tickets, estimates)
            ),
            schema=ConflictResult,
            tool_name="record_conflicts",
            tool_description="Record every detected contradiction between independent sources.",
            max_tokens=8192,
        )
    except LLMUnavailable as exc:
        return StageResult.fallback(
            fallback_conflicts, f"conflict detection failed ({exc}); used rule-based detection"
        )

    valid = {t.id for t in tickets}
    conflicts = [
        Conflict(
            ticket_id=c.ticket_id,
            source_a=c.source_a,
            claim_a=c.claim_a,
            source_b=c.source_b,
            claim_b=c.claim_b,
            trusted=c.trusted,
            reasoning=c.reasoning,
            severity_of_conflict=c.severity_of_conflict,
        )
        for c in result.conflicts
        if c.ticket_id in valid  # hallucinated IDs are dropped here and asserted in tests
    ]
    return StageResult.ok(conflicts)


# -------------------------------------------------------------- stage 5: advisor


ADVISOR_ROLE = """\
You advise on which VALUE POSTURE fits a company's declared situation.

You are not choosing the company's ethics. A human confirms or overrides your
recommendation before it is applied, and that is deliberate: letting a model
autonomously set a company's values is the thing not to build.

Rank all the postures for THIS situation, recommend one, and name what the
recommendation costs in this batch's own terms. An advisor who recommends without
naming the cost is not advising, they are flattering.
"""


def advise_posture(
    client: LLMClient,
    company_summary: str,
    postures: dict[str, Posture],
    charter: dict[str, Any],
    tickets: list[Ticket],
    estimates: list[TicketEstimate],
    fallback: PostureAdvice,
) -> StageResult:
    """Stage 5. Advice only -- confirmation happens in the CLI, not here."""
    if not client.available:
        return StageResult.fallback(
            fallback, "no API key and no cassettes; used the company-state decision tree"
        )

    try:
        result = client.structured(
            stage="advisor",
            system=prompts.system_prompt(role=ADVISOR_ROLE, charter=charter),
            user=(
                f"{prompts.render_company_state(company_summary)}\n\n"
                f"AVAILABLE POSTURES:\n{prompts.render_postures(postures)}\n\n"
                f"THE BATCH YOU WILL BE RANKING UNDER THIS POSTURE:\n"
                f"{prompts.render_batch(tickets, estimates)}\n\n"
                "Recommend one posture, rank the rest, and name the cost concretely: "
                "which specific tickets and merchants pay for your recommendation."
            ),
            schema=AdviceResult,
            tool_name="record_posture_advice",
            tool_description="Record a ranked recommendation over value postures for this situation.",
            max_tokens=6144,
        )
    except LLMUnavailable as exc:
        return StageResult.fallback(fallback, f"posture advice failed ({exc}); used the decision tree")

    if result.recommended not in postures:
        return StageResult.fallback(
            fallback, f"model recommended unknown posture {result.recommended!r}; used the decision tree"
        )

    return StageResult.ok(
        PostureAdvice(
            recommended=result.recommended,
            reasoning=result.reasoning,
            what_it_costs=result.what_it_costs,
            ranked_alternatives=[
                PostureRanking(posture=a.posture, rank=a.rank, reasoning=a.reasoning, trade_off=a.trade_off)
                for a in result.ranked_alternatives
                if a.posture in postures
            ],
            charter_collision_warning=result.charter_collision_warning,
            source="llm",
        )
    )


# -------------------------------------------------------------- stage 8: critics


CRITICS: list[dict[str, str]] = [
    {
        "key": "fairness_campaigner",
        "posture_voice": "fairness_first",
        "role": (
            "You are a fairness campaigner. Tear this ranking apart.\n\n"
            "You care about who is systematically at the back of the queue, about "
            "merchants who have been skipped repeatedly, and about whether 'we serve "
            "the most important customers' is a policy or just a description of who "
            "shouts loudest. Cite waiting times and skip counts."
        ),
    },
    {
        "key": "cfo",
        "posture_voice": "revenue_first",
        "role": (
            "You are the CFO. Tell me what this ranking costs.\n\n"
            "You care about ARR at risk, renewal exposure, GMV bleeding per hour, and "
            "the difference between money contracted last year and money leaving the "
            "building this afternoon. Put numbers on your objection."
        ),
    },
    {
        "key": "sre",
        "posture_voice": "platform_health",
        "role": (
            "You are an SRE. Tell me what is about to break.\n\n"
            "You care about blast radius, correlated failures, and problems whose "
            "current impact understates their trajectory. You are suspicious of any "
            "ranking that treats correlated tickets as independent complaints."
        ),
    },
    {
        "key": "support_lead",
        "posture_voice": "speed_optimised",
        "role": (
            "You are the support lead with the agents you actually have and a backlog "
            "you cannot clear.\n\nTell me why this ranking is unworkable in practice: "
            "what it does to throughput, what it does to the queue behind it, and which "
            "of these tickets will still be open next week."
        ),
    },
]


def critique(
    client: LLMClient,
    ordering: Ordering,
    clusters: list[Cluster],
    conflicts: list[Conflict],
    company_summary: str,
    charter: dict[str, Any],
    critics: list[dict[str, str]] | None = None,
) -> StageResult:
    """Stage 8. Four calls, one per rejected strategy.

    A visible mind-change is the most convincing artefact this system can produce, and
    it only happens if the critics are given a real chance to land a hit. Each is asked
    for its single strongest point, and each is asked to argue against itself.
    """
    critics = critics or CRITICS
    if not client.available:
        return StageResult.fallback([], "no API key and no cassettes; ranking is unreviewed")

    postures = load_postures()
    out: list[Critique] = []
    failures: list[str] = []

    shared = (
        f"{prompts.render_company_state(company_summary)}\n\n"
        f"{prompts.render_ordering(ordering)}\n\n"
        f"{prompts.render_clusters(clusters)}\n\n"
        "CONTRADICTIONS THE SYSTEM FOUND:\n"
        + ("\n".join(f"  {c.ticket_id}: {c.source_a} says {c.claim_a} / {c.source_b} says "
                     f"{c.claim_b} -> trusted {c.trusted}" for c in conflicts) or "  none")
    )

    for critic in critics:
        voice = postures.get(critic["posture_voice"])
        extra = (
            f"The posture you argue for is `{critic['posture_voice']}`, whose stated "
            f"rationale is: {' '.join(voice.rationale.split())}" if voice else ""
        )
        try:
            result = client.structured(
                stage=f"critic_{critic['key']}",
                system=prompts.system_prompt(role=critic["role"], charter=charter, extra=extra),
                user=shared + "\n\nMake your strongest case against this ranking.",
                schema=CritiqueResult,
                tool_name="record_critique",
                tool_description="Record an objection to the proposed ranking.",
            )
        except LLMUnavailable as exc:
            failures.append(f"{critic['key']} ({exc})")
            continue

        valid = set(ordering.ticket_order)
        out.append(
            Critique(
                critic=critic["key"],
                posture_voice=critic["posture_voice"],
                objection=result.objection,
                specific_tickets=[t for t in result.specific_tickets if t in valid],
                strongest_point=result.strongest_point,
                what_it_would_cost_to_agree=result.what_it_would_cost_to_agree,
            )
        )

    if not out:
        return StageResult.fallback([], f"all critics failed ({'; '.join(failures)}); ranking is unreviewed")
    if failures:
        return StageResult(
            value=out, degraded=True,
            reason=f"{len(failures)} of {len(critics)} critics failed: {'; '.join(failures)}",
        )
    return StageResult.ok(out)


# ---------------------------------------------------------- stage 9: adjudicate


ADJUDICATE_ROLE = """\
You have ranked a batch of tickets and four critics have attacked your ranking.

Read all of them and decide whether to change your mind. Both outcomes are respectable
and neither is the safe answer:

  - Changing your mind on a specific ticket, for a specific reason, is the point of
    running critics at all.
  - Defending the ranking against a critic who is right about the value but wrong about
    this batch is also correct, and you should say so plainly.

Two hard constraints:
  - Revisions are applied and then re-checked against the charter in code. A revision
    the charter would immediately undo is wasted.
  - You must fill `conceded_but_not_acted`. There is always something a critic got
    right that you are not acting on, and pretending otherwise is the failure mode
    this stage exists to prevent.
"""


def adjudicate(
    client: LLMClient,
    ordering: Ordering,
    critiques: list[Critique],
    charter: dict[str, Any],
) -> StageResult:
    """Stage 9. Revise or defend."""
    if not client.available or not critiques:
        return StageResult.fallback(
            Adjudication(changed_mind=False, defence="No critique stage ran; ranking stands unreviewed.",
                         source="fallback"),
            "no critiques to adjudicate; ranking is unreviewed",
        )

    rendered = "\n\n".join(
        f"CRITIC: {c.critic} (arguing for {c.posture_voice})\n"
        f"  objection: {c.objection}\n"
        f"  tickets: {', '.join(c.specific_tickets) or 'none named'}\n"
        f"  strongest point: {c.strongest_point}\n"
        f"  what it costs to agree: {c.what_it_would_cost_to_agree}"
        for c in critiques
    )

    try:
        result = client.structured(
            stage="adjudicate",
            system=prompts.system_prompt(role=ADJUDICATE_ROLE, charter=charter),
            user=f"{prompts.render_ordering(ordering)}\n\n{rendered}\n\nRevise or defend.",
            schema=AdjudicationResult,
            tool_name="record_adjudication",
            tool_description="Record whether the ranking changes in response to critique.",
            max_tokens=6144,
        )
    except LLMUnavailable as exc:
        return StageResult.fallback(
            Adjudication(changed_mind=False, defence=f"Adjudication failed: {exc}", source="fallback"),
            f"adjudication failed ({exc}); ranking stands unreviewed",
        )

    valid = {r.ticket_id: r.rank for r in ordering.ranked}
    revisions = [
        Revision(
            ticket_id=r.ticket_id,
            from_rank=valid[r.ticket_id],
            to_rank=max(1, min(len(ordering.ranked), r.to_rank)),
            because_of_critic=r.because_of_critic,
            reasoning=r.reasoning,
        )
        for r in result.revisions
        if r.ticket_id in valid
    ]
    revisions = [r for r in revisions if r.from_rank != r.to_rank]

    return StageResult.ok(
        Adjudication(
            changed_mind=bool(revisions),
            revisions=revisions,
            defence=result.defence,
            conceded_but_not_acted=result.conceded_but_not_acted,
            source="llm",
        )
    )


# ------------------------------------------------------------- stage 10: narrate


NARRATE_ROLE = """\
You write the opening of a triage report for an engineering leader who has ninety
seconds. Plain sentences, no restating of numbers already in the tables below you, and
no reassurance. If the ranking is uncomfortable, say why it is uncomfortable.
"""


def narrate(
    client: LLMClient,
    ordering: Ordering,
    company_summary: str,
    conflicts: list[Conflict],
    adjudication: Adjudication | None,
    charter: dict[str, Any],
) -> StageResult:
    if not client.available:
        return StageResult.fallback("", "no API key and no cassettes; report is tables only")

    try:
        result = client.structured(
            stage="narrate",
            system=prompts.system_prompt(role=NARRATE_ROLE, charter=charter),
            user=(
                f"{prompts.render_company_state(company_summary)}\n\n"
                f"{prompts.render_ordering(ordering)}\n\n"
                f"Contradictions found: {len(conflicts)}\n"
                f"Mind changed after critique: {bool(adjudication and adjudication.changed_mind)}"
            ),
            schema=NarrativeResult,
            tool_name="record_narrative",
            tool_description="Record a short narrative summary of this batch's triage.",
            max_tokens=2048,
        )
    except LLMUnavailable as exc:
        return StageResult.fallback("", f"narrative failed ({exc}); report is tables only")

    return StageResult.ok(
        f"{result.situation}\n\n"
        f"**The decision that matters:** {result.headline_decision}\n\n"
        f"**What would change my mind:** {result.what_would_change_my_mind}"
    )
