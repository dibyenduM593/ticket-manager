"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from triage import paths
from triage.config import load_posture
from triage.models import (
    URGENCY_SCALE,
    CategoryStats,
    CrmRecord,
    CustomerHistory,
    LedgerEntry,
    Telemetry,
    Ticket,
    TicketEstimate,
    Tier,
    Urgency,
)
from triage.pipeline import Context
from triage.state import State

AS_OF = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo_root() -> Path:
    return paths.repo_root()


@pytest.fixture
def batch1_ctx() -> Context:
    """Real batch 1 against real state. State is cloned so tests never mutate state/."""
    ctx = Context.load(paths.batches_dir() / "batch_1.json")
    return Context(ctx.batch, ctx.company, ctx.sources, ctx.state.clone())


@pytest.fixture
def batch2_ctx() -> Context:
    ctx = Context.load(paths.batches_dir() / "batch_2.json")
    return Context(ctx.batch, ctx.company, ctx.sources, ctx.state.clone())


@pytest.fixture
def batch3_ctx() -> Context:
    ctx = Context.load(paths.batches_dir() / "batch_3.json")
    return Context(ctx.batch, ctx.company, ctx.sources, ctx.state.clone())


@pytest.fixture
def revenue_first():
    return load_posture("revenue_first")


@pytest.fixture
def balanced():
    return load_posture("balanced")


def make_estimate(**overrides) -> TicketEstimate:
    """A synthetic estimate with sane defaults, for unit tests that need to isolate
    one variable. Everything is explicit so a test reads as a statement about the
    thing it is testing."""
    stated = overrides.pop("stated_urgency", Urgency.medium)
    if isinstance(stated, str):
        stated = Urgency(stated)
    base = dict(
        ticket_id="TKT-0001",
        customer_id="acme",
        category="integration_api",
        stated_urgency=stated,
        claimed_urgency_value=URGENCY_SCALE[stated.value],
        urgency_intensity=0.0,
        credibility=0.5,
        credibility_evidence="synthetic",
        users_affected=0,
        error_rate=0.0,
        blast_radius=0.0,
        category_severe_rate=0.0,
        category_median_hours=24.0,
        category_n=10,
        security_exposure=False,
        data_loss=False,
        tier=Tier.growth,
        arr=50000.0,
        gmv_at_risk_per_hour=0.0,
        waiting_hours=1.0,
        times_skipped=0,
        expected_resolution_hours=24.0,
        compliance_deadline_hours=None,
        sla_deadline_hours=None,
        blocks_paying_workflow_claimed=False,
        blocks_paying_workflow_observed=False,
    )
    base.update(overrides)
    if "claimed_urgency_value" not in overrides:
        base["claimed_urgency_value"] = URGENCY_SCALE[base["stated_urgency"].value]
    return TicketEstimate(**base)


def make_ticket(**overrides) -> Ticket:
    base = dict(
        id="TKT-0001",
        customer_id="acme",
        subject="s",
        body="b",
        category="integration_api",
        stated_urgency=Urgency.medium,
        blocks_paying_workflow=False,
        submitted_at=AS_OF,
    )
    base.update(overrides)
    return Ticket(**base)


def make_state(**overrides) -> State:
    customers = overrides.get("customers", {})
    categories = overrides.get(
        "categories",
        {"integration_api": CategoryStats(median_hours=24.0, severe_rate=0.3, n=20)},
    )
    ledger = overrides.get("ledger", {})
    return State(customers, categories, ledger)


__all__ = [
    "AS_OF",
    "make_estimate",
    "make_ticket",
    "make_state",
    "CategoryStats",
    "CrmRecord",
    "CustomerHistory",
    "LedgerEntry",
    "Telemetry",
]
