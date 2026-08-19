"""Pydantic schemas for the whole system.

Layering note, because it is the point of the project:

    Ticket / Telemetry / CrmRecord / CustomerHistory  -> raw inputs
    TicketEstimate                                    -> FACTS, in natural units
    ValueComponents / ScoredTicket                    -> WORTH, dimensionless

`TicketEstimate` deliberately carries no weights and no notion of importance. It is
the complete factual description of a ticket: how credible the claimant is, how many
users are affected, how long this kind of thing usually takes, how long it has waited.
A different company with opposite values would compute exactly the same TicketEstimate.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- enums


class Tier(str, Enum):
    enterprise = "enterprise"
    growth = "growth"
    starter = "starter"
    free = "free"


class Urgency(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


#: How a stated urgency LABEL becomes a number. This is parsing, not valuation --
#: it records what the merchant said, it does not decide what that is worth.
URGENCY_SCALE: dict[str, float] = {
    "low": 0.25,
    "medium": 0.50,
    "high": 0.75,
    "critical": 1.00,
}


# --------------------------------------------------------------------- raw inputs


class Ticket(BaseModel):
    """Source A -- what the merchant claims. Subjective and inflatable by design."""

    model_config = ConfigDict(extra="forbid")

    id: str
    customer_id: str
    subject: str
    body: str
    category: str
    stated_urgency: Urgency
    blocks_paying_workflow: bool = Field(
        description="Declared by the merchant. Telemetry is allowed to contradict this."
    )
    submitted_at: datetime
    compliance_deadline_at: datetime | None = None
    sla_deadline_at: datetime | None = None

    #: Populated by ingest.py when sanitisation strips instruction-shaped content.
    #: Never silently dropped -- what was removed is reported.
    sanitisation_notes: list[str] = Field(default_factory=list)


class PlantedConflict(BaseModel):
    """Ground truth we actually own.

    We cannot have ground truth for "the right ranking" -- the brief's premise is that
    no such thing exists. We CAN have it for "was there a contradiction here", because
    we authored the contradiction. That is what the eval in scripts/eval_conflicts.py
    measures, and it is the only place in this project the word "recall" is honest.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_id: str
    sources: list[str]
    description: str


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: int
    label: str
    tickets: list[Ticket]
    planted_conflicts: list[PlantedConflict] = Field(default_factory=list)
    #: The moment this batch is triaged. Overrides company_state.as_of when present,
    #: because a multi-batch run is a sequence of moments and waiting times have to
    #: advance between them. Still never datetime.now() -- an injectable clock is the
    #: difference between golden tests that hold and golden tests that rot overnight.
    as_of: datetime | None = None


class Telemetry(BaseModel):
    """Source B -- what the platform measured. Indifferent to who filed the ticket."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    users_affected: int = 0
    error_rate: float = 0.0
    checkout_success_rate: float | None = None
    gmv_at_risk_per_hour: float = 0.0
    error_signature: str | None = None
    region: str | None = None
    first_error_at: datetime | None = None
    security_exposure: bool = False
    data_loss: bool = False
    p95_latency_ms: int | None = None
    notes: str | None = None


class CrmRecord(BaseModel):
    """Source C -- what the relationship is worth on paper."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str
    name: str
    tier: Tier
    arr: float
    renewal_date: str | None = None
    gmv_per_hour_baseline: float = 0.0
    account_notes: str | None = None


class Observation(BaseModel):
    """One historical urgency claim and whether it turned out to be real."""

    model_config = ConfigDict(extra="forbid")

    ticket: str
    claimed: Urgency
    verdict: bool
    batch: int


class CustomerHistory(BaseModel):
    """Source D (part 1) -- who cries wolf. Counts only; credibility is never stored."""

    model_config = ConfigDict(extra="forbid")

    urgency_claims: int = 0
    confirmed_severe: int = 0
    last_updated_batch: int = 0
    observations: list[Observation] = Field(default_factory=list)


class CategoryStats(BaseModel):
    """Source D (part 2) -- what this kind of problem is really like."""

    model_config = ConfigDict(extra="forbid")

    median_hours: float
    severe_rate: float
    n: int


class LedgerEntry(BaseModel):
    """Source D (part 3) -- the waiting-room clipboard.

    Fairness is not a property of one decision, it is a property of a series. Revenue
    and criticality can be read off a single ticket; fairness cannot be computed at
    all without this file.
    """

    model_config = ConfigDict(extra="forbid")

    first_seen_batch: int
    times_skipped: int = 0
    first_seen_at: datetime | None = None
    last_batch_seen: int = 0
    #: The batch that last charged this ticket a skip. Without it `accrue_skips` is
    #: not idempotent, and re-running a batch inflates fairness debt off nothing --
    #: which is how a working tree ended up with `times_skipped: 2` on tickets that
    #: had been deferred exactly once. Mirrors the ticket-id guard in
    #: `State.record_observation`.
    last_skip_batch: int = 0


class PendingTicket(BaseModel):
    """A deferred ticket, kept whole so the next batch can actually triage it.

    LedgerEntry is the waiting-room clipboard: how long, how many skips. This is the
    ticket itself. Without it carry-forward is something a human does by editing a
    fixture -- which is literally how TKT-4471 gets into batch 43 today, and why a
    batch typed into the web form could never show an old ticket at all.

    The snapshots are a fallback, not a measurement. A ticket someone typed has no row
    in data/sources/, so without them a carried web ticket hits `crm_for`'s KeyError on
    the very next run. Where a live row exists it wins -- replaying a stale measurement
    as if it were current is the quiet staleness `company_state_for` exists to avoid.
    """

    model_config = ConfigDict(extra="forbid")

    ticket: Ticket
    deferred_in_batch: int
    telemetry_snapshot: Telemetry | None = None
    crm_snapshot: CrmRecord | None = None


class RunMeta(BaseModel):
    """Where the sequence has got to: the last batch triaged, and when.

    A batch read off disk declares its own id and moment. A batch typed into the web
    form has neither, and inventing a constant for both -- which is what `batch_id=900`
    and a frozen `AS_OF` were -- means ids collide across submissions and the clock
    never advances, so waiting debt can never grow. This is the file that lets an
    ad-hoc batch be the NEXT batch rather than a batch outside time.
    """

    model_config = ConfigDict(extra="forbid")

    last_batch_id: int = 0
    as_of: datetime | None = None


class ResolvedTicket(BaseModel):
    """A row of history. Note what is absent: no `was_severe` field.

    We store the operational facts and derive the verdict, so the definition of
    "genuinely severe" stays visible and arguable rather than asserted.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    customer_id: str
    category: str
    stated_urgency: Urgency
    claimed_urgent: bool
    actual_users_affected: int
    code_changed: bool
    hours_to_resolve: float
    batch: int


# ----------------------------------------------------------------- company state


class FundingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    weeks_away: int


class BusinessState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategic_priority: str
    funding_event: FundingEvent | None = None
    churn_pressure: str
    runway_months: int


class OperationsState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_load: float
    active_incident: bool
    incident_summary: str | None = None
    agents_available: int
    backlog_depth: int


class CommercialState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accounts_at_renewal: list[str] = Field(default_factory=list)
    sla_breaches_this_month: int = 0


class CompanyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    business: BusinessState
    operations: OperationsState
    commercial: CommercialState


# --------------------------------------------------------------------- valuation


class Posture(BaseModel):
    """A named stance. Named, not a slider, because a name carries an English
    rationale -- and that rationale is fed to the critics so they argue in its voice."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rationale: str
    weights: dict[str, float]
    revenue_mix: float | None = None
    cost_summary: str = ""

    def weight(self, axis: str) -> float:
        return float(self.weights.get(axis, 0.0))


# -------------------------------------------------------------------- estimation


class TicketEstimate(BaseModel):
    """Everything factual about one ticket, in natural units. No weights, no worth.

    Every field here is empirically testable in principle. Nothing here encodes a
    preference about who matters.
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    customer_id: str
    category: str

    # -- claim side (source A, discounted by source D)
    stated_urgency: Urgency
    claimed_urgency_value: float          # URGENCY_SCALE lookup, 0-1
    urgency_intensity: float              # 0-1, extracted from the message text
    credibility: float                    # Beta(1,1) posterior mean, 0-1
    credibility_evidence: str             # "2 of 9 urgency claims confirmed severe"

    # -- evidence side (source B + source D)
    users_affected: int
    error_rate: float
    blast_radius: float                   # 0-1, normalised evidence of reach
    category_severe_rate: float
    category_median_hours: float
    category_n: int
    security_exposure: bool
    data_loss: bool

    # -- money, in dollars, unvalued
    tier: Tier
    arr: float
    gmv_at_risk_per_hour: float

    # -- time, in hours, unvalued
    waiting_hours: float
    times_skipped: int
    expected_resolution_hours: float
    compliance_deadline_hours: float | None = None
    sla_deadline_hours: float | None = None

    # -- declared vs measured
    blocks_paying_workflow_claimed: bool
    blocks_paying_workflow_observed: bool


# ------------------------------------------------------------------ correlation


class Cluster(BaseModel):
    """Tickets are not independent. Three merchants reporting checkout failures in one
    batch are one platform incident affecting three merchants."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    ticket_ids: list[str]
    root_cause: str
    shared_evidence: list[str] = Field(default_factory=list)
    confidence: float = 1.0

    @property
    def is_singleton(self) -> bool:
        return len(self.ticket_ids) == 1


# ---------------------------------------------------------------------- scoring


class ValueComponents(BaseModel):
    """The four axes, dimensionless, after valuation has been applied."""

    model_config = ConfigDict(extra="forbid")

    revenue: float
    criticality: float
    fairness: float
    speed: float

    def as_dict(self) -> dict[str, float]:
        return {
            "revenue": self.revenue,
            "criticality": self.criticality,
            "fairness": self.fairness,
            "speed": self.speed,
        }


class SeverityBreakdown(BaseModel):
    """The one line that matters, shown in full so it can be argued with."""

    model_config = ConfigDict(extra="forbid")

    claimed: float                 # stated_urgency * clamp(credibility)
    observed: float                # max(blast_radius, severe_rate, charter_floor)
    charter_floor: float
    charter_floor_reason: str | None = None
    severity: float                # max(claimed, observed)
    driver: Literal["claimed", "observed"]


class ScoredTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    customer_id: str
    estimate: TicketEstimate
    severity: SeverityBreakdown
    components: ValueComponents
    score: float
    cluster_id: str
    cluster_size: int
    cluster_severity: float | None = None


class CharterOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    rule_id: str
    clause_id: str
    rule_name: str
    statement: str
    from_rank: int
    to_rank: int
    detail: str


class RankedTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    ticket_id: str
    customer_id: str
    score: float
    served: bool
    scored: ScoredTicket
    justification: str = ""
    charter_promoted: bool = False


class Ordering(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posture: str
    ranked: list[RankedTicket]
    overrides: list[CharterOverride] = Field(default_factory=list)
    capacity: int = 0
    #: Tickets the charter requires be served when there are more of them than there
    #: are agents. The charter does not get to invent capacity, so this is escalated
    #: rather than resolved by quietly picking a favourite.
    oversubscribed: list[str] = Field(default_factory=list)

    @property
    def ticket_order(self) -> list[str]:
        return [r.ticket_id for r in self.ranked]

    def rank_of(self, ticket_id: str) -> int:
        for r in self.ranked:
            if r.ticket_id == ticket_id:
                return r.rank
        raise KeyError(ticket_id)


# ------------------------------------------------------- LLM stage output models


class Conflict(BaseModel):
    """A detected contradiction between two independent sources."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    source_a: str
    claim_a: str
    source_b: str
    claim_b: str
    trusted: str
    reasoning: str
    severity_of_conflict: Literal["low", "medium", "high"] = "medium"


class PostureRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posture: str
    rank: int
    reasoning: str
    trade_off: str


class PostureAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended: str
    reasoning: str
    what_it_costs: str
    ranked_alternatives: list[PostureRanking]
    charter_collision_warning: str | None = None
    source: Literal["llm", "fallback"] = "llm"


class Critique(BaseModel):
    model_config = ConfigDict(extra="forbid")

    critic: str
    posture_voice: str
    objection: str
    specific_tickets: list[str] = Field(default_factory=list)
    strongest_point: str
    what_it_would_cost_to_agree: str


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    from_rank: int
    to_rank: int
    because_of_critic: str
    reasoning: str


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changed_mind: bool
    revisions: list[Revision] = Field(default_factory=list)
    defence: str = ""
    conceded_but_not_acted: list[str] = Field(default_factory=list)
    source: Literal["llm", "fallback"] = "llm"


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    why: str
    what_would_resolve_it: str


class RegretRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posture: str
    ticket_id: str
    rank_here: int
    rank_under_chosen: int
    cost_sentence: str


class BatchReport(BaseModel):
    """Everything one run produced. Serialised to reports/batch_N.json so the
    contract tests assert on structure rather than prose."""

    model_config = ConfigDict(extra="forbid")

    batch_id: int
    label: str
    as_of: datetime
    posture: str
    posture_chosen_by: str
    company_state_summary: str
    advice: PostureAdvice | None = None
    clusters: list[Cluster] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    ordering: Ordering
    ordering_before_adjudication: list[str] = Field(default_factory=list)
    critiques: list[Critique] = Field(default_factory=list)
    adjudication: Adjudication | None = None
    regret: list[RegretRow] = Field(default_factory=list)
    escalations: list[Escalation] = Field(default_factory=list)
    sanitisation_events: list[dict[str, Any]] = Field(default_factory=list)
    degraded_stages: list[str] = Field(default_factory=list)
    narrative: str = ""
