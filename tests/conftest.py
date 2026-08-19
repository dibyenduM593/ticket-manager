"""Shared fixtures. Nothing here touches the network.

All tests live under tests/, and only here -- pyproject.toml pins
`testpaths = ["tests"]`, so a test file dropped anywhere else silently never runs.

The key is stripped from the environment for the whole session -- see
`_no_live_api_calls`. That is enforcement, not decoration: `triage.cli` and the demo
server both load `.env` at import now, so without this a contributor with a key on
disk would silently run a DIFFERENT suite from CI, spending money and passing tests
that CI cannot execute. Tests that exercise the LLM path must inject a fake client.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from triage import paths
from triage.models import (
    URGENCY_SCALE,
    TicketEstimate,
    Tier,
    Urgency,
)
from triage.pipeline import Context


@pytest.fixture(scope="session", autouse=True)
def _no_live_api_calls():
    """No test may reach the API, whatever is sitting in .env or the shell.

    autouse and session-scoped so it cannot be forgotten. `LLMClient.available` is
    then False everywhere, which is the same state CI runs in -- the suite proves the
    deterministic floor, and it must prove the same thing on every machine.
    """
    import os

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(var, None)
    yield


@pytest.fixture
def repo_root() -> Path:
    return paths.repo_root()


@pytest.fixture
def batch1_ctx() -> Context:
    """Real batch 1 against real state. State is cloned so tests never mutate state/."""
    ctx = Context.load(paths.eval_dir() / "batch_1.json")
    return Context(ctx.batch, ctx.company, ctx.sources, ctx.state.clone())


@pytest.fixture
def batch2_ctx() -> Context:
    ctx = Context.load(paths.eval_dir() / "batch_2.json")
    return Context(ctx.batch, ctx.company, ctx.sources, ctx.state.clone())


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
