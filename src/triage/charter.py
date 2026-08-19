"""Charter enforcement -- non-negotiable floors that no posture can tune.

Two things this module does:

1. `severity_floor(est)` -- declared minimum severity for certain kinds of ticket.
   This is the ONE place the declared side reaches into an estimate, and it is
   deliberate and documented in config/charter.yaml. A confirmed security exposure
   is severe by declaration regardless of how small its measured blast radius is.

2. `enforce_full(ordering)` -- rewrites an ordering so no rule is violated, using
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


def enforce_full(
    ranked: list[RankedTicket],
    capacity: int,
    charter: dict[str, Any] | None = None,
) -> CharterResult:
    """Two passes, iterated together to a fixed point:

    1. `must_be_served` -- handled COLLECTIVELY. An earlier version promoted each
       protected ticket individually to `capacity - 1`, which made two protected
       tickets fight over the same slot and ping-pong forever. The set of tickets the
       charter protects is a set; it has to be placed as one.

    2. `rank_above_category` -- genuinely pairwise, iterated to its own fixed point.

    The outer loop exists because pass 2 moves tickets and could, under a charter
    whose `rank_above_category` triggers are not all also `must_be_served`, push a
    pass-1-protected ticket back across the capacity line. Today's charter cannot
    produce that, but enforcement must not depend on config discipline it does not
    check.

    Minimum intervention throughout: a ticket moves only when a rule requires it to.
    A protected ticket already inside the served set stays exactly where its score
    put it -- extracting the whole protected set and re-inserting it as a block would
    demote a top-scored exposure below lower-scored unprotected tickets, and log an
    override for a ticket the charter never needed to touch.
    """
    c = charter or load_charter()
    order = list(ranked)
    overrides: list[CharterOverride] = []
    oversubscribed: list[str] = []

    for _ in range(len(order) + 2):
        order, served_overrides, oversubscribed_now = _enforce_must_be_served(order, capacity, c)
        overrides.extend(served_overrides)
        if oversubscribed_now:
            oversubscribed = oversubscribed_now

        rank_overrides = _enforce_rank_above(order, c)
        overrides.extend(rank_overrides)

        if not served_overrides and not rank_overrides:
            break

    for i, item in enumerate(order):
        item.rank = i + 1
        item.served = i < capacity
        item.charter_promoted = any(o.ticket_id == item.ticket_id for o in overrides)

    return CharterResult(order, overrides, oversubscribed)


def _enforce_must_be_served(
    order: list[RankedTicket],
    capacity: int,
    c: dict[str, Any],
) -> tuple[list[RankedTicket], list[CharterOverride], list[str]]:
    """One collective placement of every `must_be_served` ticket.

    Single walk from the top: protected tickets are always admitted to the served
    block; unprotected ones are admitted while quota remains, and the displaced slide
    down IN ORDER. Relative order is preserved everywhere, so nothing is ever
    reported as moved unless a rule moved it.
    """
    if capacity <= 0:
        return order, [], []

    protected_idx: dict[int, tuple[dict, dict]] = {}
    for rule in c.get("rules", []):
        for clause in rule.get("clauses", []):
            if clause.get("kind") != "must_be_served":
                continue
            for i, item in enumerate(order):
                if i not in protected_idx and _triggers(clause, item.scored):
                    protected_idx[i] = (rule, clause)

    if not protected_idx or all(i < capacity for i in protected_idx):
        return order, [], []

    from_rank = {item.ticket_id: i + 1 for i, item in enumerate(order)}

    #: Served slots left over for unprotected tickets. Negative means the charter
    #: demands more service than there is capacity: every protected ticket is placed
    #: first and the conflict is surfaced to a human, because the charter does not
    #: get to invent capacity.
    quota = capacity - len(protected_idx)
    oversubscribed = (
        [order[i].ticket_id for i in sorted(protected_idx)] if quota < 0 else []
    )

    served_block: list[RankedTicket] = []
    deferred_block: list[RankedTicket] = []
    admitted_unprotected = 0
    for i, item in enumerate(order):
        if i in protected_idx:
            served_block.append(item)
        elif admitted_unprotected < quota:
            served_block.append(item)
            admitted_unprotected += 1
        else:
            deferred_block.append(item)
    new_order = served_block + deferred_block

    to_rank = {item.ticket_id: i + 1 for i, item in enumerate(new_order)}
    overrides: list[CharterOverride] = []
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
    return new_order, overrides, oversubscribed


def _enforce_rank_above(
    order: list[RankedTicket],
    c: dict[str, Any],
) -> list[CharterOverride]:
    """Pairwise `rank_above_category` moves, iterated to a fixed point. Mutates
    `order` in place and returns the overrides it generated."""
    overrides: list[CharterOverride] = []
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
    return overrides


def _first_rank_violation(order: list[RankedTicket], charter: dict[str, Any]):
    for rule in charter.get("rules", []):
        for clause in rule.get("clauses", []):
            if clause.get("kind") != "rank_above_category":
                continue
            must_outrank = set(clause.get("must_outrank_categories", []))
            for i, item in enumerate(order):
                if not _triggers(clause, item.scored):
                    continue
                # A ticket that itself carries the protected condition is not a
                # member of the outranked class, whatever category it was filed
                # under. "Exposure never ranks below a cosmetic ISSUE" -- a ticket
                # filed as ui_cosmetic on which telemetry has confirmed an exposure
                # is not a cosmetic issue, and treating it as one makes two
                # exposures ping-pong over each other generating no-op overrides.
                above = [
                    j
                    for j in range(i)
                    if order[j].scored.estimate.category in must_outrank
                    and not _triggers(clause, order[j].scored)
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
