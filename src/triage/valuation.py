"""VALUATION -- worth, declared. Never learned, never testable, never inferred.

This module answers: is a $480k account worth more than 2,000 free users. There is no
experiment that settles that, so nothing in here is fitted to data. Every number
originates in `config/valuation.yaml` or `config/postures/*.yaml`, both of which a
human wrote on purpose.

Hard rule, enforced by `tests/test_separation.py`: this module may not import
`estimation` or `state`. It consumes a `TicketEstimate` -- a bag of facts in natural
units -- and returns dimensionless worth. It cannot look up who filed the ticket, and
it cannot learn that some accounts complain more than others.

Why the separation matters: if history could rewrite values, the system would be
laundering a judgment call as data. "We deprioritise free-tier merchants because the
model learned to" is a sentence that means nothing, and it is the exact failure mode
worth designing against.
"""

from __future__ import annotations

import math
from typing import Any

from .config import load_valuation_config
from .models import Posture, TicketEstimate, ValueComponents


# ------------------------------------------------------------------- the axes


def account_value(est: TicketEstimate, cfg: dict[str, Any] | None = None) -> float:
    """What the relationship is worth on paper, independent of today.

    A blend of tier and ARR, because neither alone is right: pure ARR says a free
    merchant is worth literally zero, and pure tier says every enterprise account is
    interchangeable.
    """
    c = (cfg or load_valuation_config())["revenue"]
    tier_v = float(c["tier_value"][est.tier.value])
    arr_norm = _clamp01(est.arr / float(c["arr_full_scale"]))
    share = float(c["tier_share"])
    return _clamp01(share * tier_v + (1.0 - share) * arr_norm)


def gmv_value(est: TicketEstimate, cfg: dict[str, Any] | None = None) -> float:
    """What is bleeding right now, per hour.

    During a flash sale this disagrees with `account_value` -- a mid-tier merchant
    mid-sale is losing more money per minute than the enterprise account whose ARR
    dwarfs theirs. Two revenue signals in genuine disagreement, which is a sharper
    conflict than revenue-versus-fairness.
    """
    c = (cfg or load_valuation_config())["revenue"]
    return _clamp01(est.gmv_at_risk_per_hour / float(c["gmv_full_scale"]))


def revenue_value(
    est: TicketEstimate, posture: Posture, cfg: dict[str, Any] | None = None
) -> float:
    """The ARR-vs-GMV dial. `revenue_mix` = 1.0 protects the relationship,
    0.0 stops the bleeding. Postures set it; nothing learns it."""
    c = cfg or load_valuation_config()
    mix = posture.revenue_mix
    if mix is None:
        mix = float(c["revenue"]["default_mix"])
    mix = _clamp01(mix)
    return _clamp01(mix * account_value(est, c) + (1.0 - mix) * gmv_value(est, c))


def fairness_value(est: TicketEstimate, cfg: dict[str, Any] | None = None) -> float:
    """Waiting debt. `max` of the two debt measures, not their average.

    Six skips at low wall-clock and one very long wait are both grievances, and a
    merchant who has been passed over repeatedly should not have that diluted by the
    clock being kind to them.
    """
    c = (cfg or load_valuation_config())["fairness"]
    by_clock = est.waiting_hours / float(c["waiting_ceiling_hours"])
    by_skips = est.times_skipped / float(c["skip_full_scale"])
    return _clamp01(max(by_clock, by_skips))


def speed_value(est: TicketEstimate, cfg: dict[str, Any] | None = None) -> float:
    """Quick wins score high. Log scale: the gap between 1h and 4h matters far more
    than the gap between 90h and 96h."""
    c = (cfg or load_valuation_config())["speed"]
    full = float(c["hours_full_scale"])
    h = max(0.0, est.expected_resolution_hours)
    return _clamp01(1.0 - math.log1p(h) / math.log1p(full))


# --------------------------------------------------------------- the value bag


def components(
    est: TicketEstimate,
    severity: float,
    posture: Posture,
    cfg: dict[str, Any] | None = None,
) -> ValueComponents:
    """Four dimensionless axes.

    `severity` arrives pre-computed from scorer.severity() rather than being derived
    here, because it is the single value that both halves of the system touch and it
    should be visible at the crossing point, not buried in a valuation helper.
    """
    c = cfg or load_valuation_config()
    return ValueComponents(
        revenue=revenue_value(est, posture, c),
        criticality=_clamp01(severity),
        fairness=fairness_value(est, c),
        speed=speed_value(est, c),
    )


def weighted_score(comp: ValueComponents, posture: Posture) -> float:
    """The weights ARE the ethics. This function is four multiplications and a sum;
    everything interesting about it happened in the YAML."""
    d = comp.as_dict()
    return sum(posture.weight(axis) * value for axis, value in d.items())


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))
