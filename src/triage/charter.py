"""Charter enforcement -- non-negotiable floors that no posture can tune.

Two things this module does:

1. `severity_floor(est)` -- declared minimum severity for certain kinds of ticket.
   This is the ONE place the declared side reaches into an estimate, and it is
   deliberate and documented in config/charter.yaml. A confirmed security exposure
   is severe by declaration regardless of how small its measured blast radius is.

2. `enforce(ordering)` -- rewrites an ordering so no rule is violated, using
   MINIMUM INTERVENTION: a ticket is promoted to the lowest rank that satisfies the
   rule, never straight to the top. The charter is a floor, not a preference, and
   promoting to rank 1 would quietly turn it into one.

The LLM never touches this module. Overrides are computed, not argued for.
"""

from __future__ import annotations

from typing import Any

from .config import load_charter
from .models import CharterOverride, RankedTicket, ScoredTicket, TicketEstimate


# ------------------------------------------------------------- severity floors


def severity_floor(est: TicketEstimate, charter: dict[str, Any] | None = None) -> tuple[float, str | None]:
    """Return (floor, reason). Highest matching floor wins."""
    c = charter or load_charter()
    best = 0.0
    reason: str | None = None
    for rule in c.get("severity_floors", []):
        when = rule.get("when", {})
        if _floor_matches(when, est):
            f = float(rule["floor"])
            if f > best:
                best, reason = f, rule.get("reason")
    return best, reason


def _floor_matches(when: dict[str, Any], est: TicketEstimate) -> bool:
    if "telemetry_flag" in when:
        flag = when["telemetry_flag"]
        if flag == "security_exposure" and est.security_exposure:
            return True
        if flag == "data_loss" and est.data_loss:
            return True
        return False
    if "category" in when:
        return est.category == when["category"]
    if "compliance_deadline_hours_lte" in when:
        h = est.compliance_deadline_hours
        return h is not None and h <= float(when["compliance_deadline_hours_lte"])
    return False


def credibility_clamp(charter: dict[str, Any] | None = None) -> tuple[float, float]:
    """Rule R4: the discount may dampen a claim, never erase it."""
    c = charter or load_charter()
    for rule in c.get("rules", []):
        for clause in rule.get("clauses", []):
            if clause.get("kind") == "clamp" and clause.get("applies_to") == "credibility_multiplier":
                return float(clause["floor"]), float(clause["ceiling"])
    return 0.3, 1.0


def cosmetic_categories(charter: dict[str, Any] | None = None) -> set[str]:
    return set((charter or load_charter()).get("cosmetic_categories", []))


# ------------------------------------------------------------------ enforcement


def _triggers(clause: dict[str, Any], scored: ScoredTicket) -> bool:
    est = scored.estimate
    if "trigger_flags" in clause:
        flags = clause["trigger_flags"]
        if ("security_exposure" in flags and est.security_exposure) or (
            "data_loss" in flags and est.data_loss
        ):
            return True
        return False
    if "trigger_waiting_hours_gte" in clause:
        return est.waiting_hours >= float(clause["trigger_waiting_hours_gte"])
    if "trigger_compliance_deadline_hours_lte" in clause:
        h = est.compliance_deadline_hours
        return h is not None and h <= float(clause["trigger_compliance_deadline_hours_lte"])
    return False


class CharterResult:
    """Enforcement outcome, including the case the charter cannot satisfy."""

    def __init__(
        self,
        ranked: list[RankedTicket],
        overrides: list[CharterOverride],
        oversubscribed: list[str],
    ) -> None:
        self.ranked = ranked
        self.overrides = overrides
        #: Tickets the charter requires be served when there are more such tickets
        #: than agents. The charter does not get to invent capacity, so this is
        #: surfaced to a human rather than resolved by picking a favourite.
        self.oversubscribed = oversubscribed


def enforce(
    ranked: list[RankedTicket],
    capacity: int,
    charter: dict[str, Any] | None = None,
) -> tuple[list[RankedTicket], list[CharterOverride]]:
    """Apply the charter. Thin wrapper kept for callers that only want the ordering."""
    result = enforce_full(ranked, capacity, charter)
    return result.ranked, result.overrides


def enforce_full(
    ranked: list[RankedTicket],
    capacity: int,
    charter: dict[str, Any] | None = None,
) -> CharterResult:
    """Two passes, in this order:

    1. `must_be_served` -- handled COLLECTIVELY. An earlier version promoted each
       protected ticket individually to `capacity - 1`, which made two protected
       tickets fight over the same slot and ping-pong forever. The set of tickets the
       charter protects is a set; it has to be placed as one.

    2. `rank_above_category` -- genuinely pairwise, iterated to a fixed point.

    Minimum intervention throughout: unprotected high scorers keep the top slots and
    protected tickets fill the remaining served ones. The charter is a floor, not a
    preference, and promoting to rank 1 would quietly turn it into one.
    """
    c = charter or load_charter()
    order = list(ranked)
    overrides: list[CharterOverride] = []
    oversubscribed: list[str] = []

    # ---- pass 1: must_be_served, collectively ------------------------------------
    if capacity > 0:
        protected_idx: dict[int, tuple[dict, dict]] = {}
        for rule in c.get("rules", []):
            for clause in rule.get("clauses", []):
                if clause.get("kind") != "must_be_served":
                    continue
                for i, item in enumerate(order):
                    if i not in protected_idx and _triggers(clause, item.scored):
                        protected_idx[i] = (rule, clause)

        if protected_idx and any(i >= capacity for i in protected_idx):
            protected = [order[i] for i in sorted(protected_idx)]
            rest = [item for i, item in enumerate(order) if i not in protected_idx]
            from_rank = {item.ticket_id: i + 1 for i, item in enumerate(order)}

            if len(protected) >= capacity:
                new_order = protected + rest
                if len(protected) > capacity:
                    oversubscribed = [p.ticket_id for p in protected]
            else:
                keep = capacity - len(protected)
                new_order = rest[:keep] + protected + rest[keep:]

            to_rank = {item.ticket_id: i + 1 for i, item in enumerate(new_order)}
            for i in sorted(protected_idx):
                item = order[i]
                if to_rank[item.ticket_id] == from_rank[item.ticket_id]:
                    continue
                rule, clause = protected_idx[i]
                overrides.append(
                    CharterOverride(
                        ticket_id=item.ticket_id,
                        rule_id=rule["id"],
                        clause_id=clause["id"],
                        rule_name=rule["name"],
                        statement=" ".join(rule["statement"].split()),
                        from_rank=from_rank[item.ticket_id],
                        to_rank=to_rank[item.ticket_id],
                        detail=_served_detail(clause, item),
                    )
                )
            order = new_order

    # ---- pass 2: rank_above_category, pairwise to a fixed point -------------------
    for _ in range(len(order) + 2):
        move = _first_rank_violation(order, c)
        if move is None:
            break
        idx, target_idx, rule, clause, detail = move
        item = order.pop(idx)
        order.insert(target_idx, item)
        overrides.append(
            CharterOverride(
                ticket_id=item.ticket_id,
                rule_id=rule["id"],
                clause_id=clause["id"],
                rule_name=rule["name"],
                statement=" ".join(rule["statement"].split()),
                from_rank=idx + 1,
                to_rank=target_idx + 1,
                detail=detail,
            )
        )

    for i, item in enumerate(order):
        item.rank = i + 1
        item.served = i < capacity
        item.charter_promoted = any(o.ticket_id == item.ticket_id for o in overrides)

    return CharterResult(order, overrides, oversubscribed)


def _first_rank_violation(order: list[RankedTicket], charter: dict[str, Any]):
    for rule in charter.get("rules", []):
        for clause in rule.get("clauses", []):
            if clause.get("kind") != "rank_above_category":
                continue
            must_outrank = set(clause.get("must_outrank_categories", []))
            for i, item in enumerate(order):
                if not _triggers(clause, item.scored):
                    continue
                above = [
                    j for j in range(i) if order[j].scored.estimate.category in must_outrank
                ]
                if above:
                    target = min(above)
                    return (
                        i,
                        target,
                        rule,
                        clause,
                        f"ranked below {order[target].ticket_id} "
                        f"({order[target].scored.estimate.category}) despite "
                        f"{_flag_names(item.scored)}",
                    )
    return None


def _flag_names(scored: ScoredTicket) -> str:
    est = scored.estimate
    names = []
    if est.security_exposure:
        names.append("confirmed security exposure")
    if est.data_loss:
        names.append("confirmed data loss")
    return " and ".join(names) or "a charter-protected condition"


def _served_detail(clause: dict[str, Any], item: RankedTicket) -> str:
    est = item.scored.estimate
    if "trigger_flags" in clause:
        return f"deferred past the capacity line despite {_flag_names(item.scored)}"
    if "trigger_waiting_hours_gte" in clause:
        return (
            f"has waited {est.waiting_hours:.0f}h across {est.times_skipped} skips, "
            f"past the {clause['trigger_waiting_hours_gte']:.0f}h ceiling"
        )
    if "trigger_compliance_deadline_hours_lte" in clause:
        return (
            f"statutory deadline in {est.compliance_deadline_hours:.0f}h, "
            f"inside the {clause['trigger_compliance_deadline_hours_lte']:.0f}h hard limit"
        )
    return "charter clause triggered"
