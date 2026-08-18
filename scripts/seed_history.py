"""Generate the resolution history, then derive state/ from it.

This is not "fake data" pretending to be real data. The history's job is to SET UP the
demo: NorthPeak must already have a bad track record before the live batch arrives, so
the audience sees the credibility discount applied and knows where it came from. Real
data cannot do that, because real data does not know what you are about to demonstrate.

What is designed vs what is generated:

    DESIGNED    the per-merchant claim/confirmation profiles below, and the per-category
                shape (typical resolution time, how often this kind of thing is real)
    GENERATED   ~250 individual resolved tickets consistent with those profiles

And critically: state/customers.json and state/categories.json are DERIVED by counting
the generated tickets, not written by hand. So the numbers in state/ are true of the
history that sits next to them, and `was_actually_severe` -- an operational definition
you can argue with -- is what decides each verdict. The category stats therefore land
NEAR the design targets, not exactly on them, which is the honest outcome.

Fixed seed. Reproducible. Run:

    python scripts/seed_history.py
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from triage.estimation import was_actually_severe  # noqa: E402
from triage.models import CategoryStats, CustomerHistory, Observation, ResolvedTicket  # noqa: E402

SEED = 20260819
ROOT = Path(__file__).resolve().parents[1]
HISTORY_BATCHES = (1, 41)          # live batches start at 42
HISTORY_END = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)

# --------------------------------------------------------------------- profiles
#
# claims      = how many times this merchant filed a high/critical ticket
# confirmed   = how many of those turned out to be genuine, per was_actually_severe
#
# credibility = (confirmed + 1) / (claims + 2)
#
#   northpeak    3/11 = 0.27   chronic inflator, and the point of the whole demo
#   kitecopper   2/9  = 0.22   noisy, low signal
#   bloomvine    4/7  = 0.57
#   tidewater    4/6  = 0.67   credible, low volume
#   verdant      4/6  = 0.67   credible, and repeatedly skipped anyway
#   soloartisan  3/4  = 0.75   rarely complains; when they do it is real

PROFILES: dict[str, dict[str, int]] = {
    "northpeak":   {"claims": 9, "confirmed": 2, "quiet": 22},
    "tidewater":   {"claims": 4, "confirmed": 3, "quiet": 14},
    "bloomvine":   {"claims": 5, "confirmed": 3, "quiet": 26},
    "verdant":     {"claims": 4, "confirmed": 3, "quiet": 19},
    "soloartisan": {"claims": 2, "confirmed": 2, "quiet": 6},
    "kitecopper":  {"claims": 7, "confirmed": 1, "quiet": 39},
}

# Per-category shape. `target_n` sizes the category; `median_hours` and `severe_rate`
# are the distribution the generator samples toward.
CATEGORIES: dict[str, dict[str, float]] = {
    "checkout_failure":   {"median_hours": 3.5,  "severe_rate": 0.82, "target_n": 28},
    "payment_processing": {"median_hours": 4.5,  "severe_rate": 0.78, "target_n": 22},
    "data_integrity":     {"median_hours": 6.0,  "severe_rate": 0.71, "target_n": 17},
    "security":           {"median_hours": 2.5,  "severe_rate": 0.94, "target_n": 12},
    "account_access":     {"median_hours": 8.0,  "severe_rate": 0.45, "target_n": 19},
    "performance":        {"median_hours": 14.0, "severe_rate": 0.31, "target_n": 24},
    "integration_api":    {"median_hours": 22.0, "severe_rate": 0.38, "target_n": 31},
    "billing":            {"median_hours": 30.0, "severe_rate": 0.22, "target_n": 26},
    "shipping_config":    {"median_hours": 48.0, "severe_rate": 0.11, "target_n": 20},
    "ui_cosmetic":        {"median_hours": 96.0, "severe_rate": 0.04, "target_n": 51},
    "compliance":         {"median_hours": 12.0, "severe_rate": 0.66, "target_n": 9},
}

# Which categories a merchant plausibly files under, weighted. Keeps the history from
# looking like uniform noise -- Kite & Copper really does file mostly cosmetic tickets.
CATEGORY_TASTE: dict[str, dict[str, float]] = {
    "northpeak":   {"checkout_failure": 3, "performance": 4, "integration_api": 3, "billing": 2, "ui_cosmetic": 3, "account_access": 2, "payment_processing": 2},
    "tidewater":   {"integration_api": 4, "billing": 2, "shipping_config": 3, "data_integrity": 2, "account_access": 1},
    "bloomvine":   {"checkout_failure": 3, "payment_processing": 3, "ui_cosmetic": 4, "shipping_config": 2, "performance": 2},
    "verdant":     {"integration_api": 4, "data_integrity": 2, "checkout_failure": 2, "billing": 2, "account_access": 2},
    "soloartisan": {"data_integrity": 2, "ui_cosmetic": 2, "account_access": 1, "billing": 1},
    "kitecopper":  {"ui_cosmetic": 6, "billing": 3, "shipping_config": 3, "account_access": 2, "performance": 2},
}

# Categories where a "claim" (high/critical filing) is plausible at all.
CLAIMABLE = {
    "checkout_failure", "payment_processing", "data_integrity", "security",
    "account_access", "performance", "integration_api", "compliance",
}


def main() -> None:
    rng = random.Random(SEED)
    tickets: list[ResolvedTicket] = []
    counter = 3000

    # ---- 1. per-merchant claim tickets, forced to hit the designed profile -------
    for cust, profile in PROFILES.items():
        taste = {k: v for k, v in CATEGORY_TASTE[cust].items() if k in CLAIMABLE}
        if not taste:
            taste = {"account_access": 1}
        verdicts = [True] * profile["confirmed"] + [False] * (profile["claims"] - profile["confirmed"])
        rng.shuffle(verdicts)
        for verdict in verdicts:
            counter += 1
            cat = _weighted_choice(rng, taste)
            tickets.append(
                _make_ticket(
                    rng, f"TKT-{counter}", cust, cat,
                    claimed_urgent=True,
                    severe=verdict,
                    stated_urgency=rng.choice(["high", "critical", "critical"]),
                )
            )

    # ---- 2. quiet tickets: fill out the categories, no urgency claim attached ----
    for cust, profile in PROFILES.items():
        taste = CATEGORY_TASTE[cust]
        for _ in range(profile["quiet"]):
            counter += 1
            cat = _weighted_choice(rng, taste)
            severe = rng.random() < CATEGORIES[cat]["severe_rate"]
            tickets.append(
                _make_ticket(
                    rng, f"TKT-{counter}", cust, cat,
                    claimed_urgent=False,
                    severe=severe,
                    stated_urgency=rng.choice(["low", "medium", "medium"]),
                )
            )

    # ---- 3. top categories up to target_n with platform-wide tickets ------------
    #
    # The fill is steered rather than sampled i.i.d.: merchant claim tickets carry the
    # MERCHANT's verdict, not the category's, so a category that absorbed several of
    # NorthPeak's false alarms is already below its designed severe rate. We close the
    # gap here so the generated population matches the designed profile. The stats in
    # state/categories.json are still DERIVED by counting -- they are just counting a
    # population that was built to be the shape we said it was.
    for cat, spec in CATEGORIES.items():
        rows = [t for t in tickets if t.category == cat]
        need = int(spec["target_n"]) - len(rows)
        if need <= 0:
            continue
        have_severe = sum(was_actually_severe(t) for t in rows)
        want_severe = round(float(spec["severe_rate"]) * int(spec["target_n"]))
        severe_fills = max(0, min(need, want_severe - have_severe))
        plan = [True] * severe_fills + [False] * (need - severe_fills)
        rng.shuffle(plan)
        for severe in plan:
            counter += 1
            cust = rng.choice(list(PROFILES))
            tickets.append(
                _make_ticket(
                    rng, f"TKT-{counter}", cust, cat,
                    claimed_urgent=False,
                    severe=severe,
                    stated_urgency=rng.choice(["low", "medium", "high"]),
                )
            )

    tickets.sort(key=lambda t: t.batch)

    # ---- 4. derive state/ by COUNTING the history --------------------------------
    customers = _derive_customers(tickets)
    categories = _derive_categories(tickets)
    ledger = _seed_ledger()

    _write(ROOT / "data" / "history" / "resolved_tickets.json",
           [t.model_dump(mode="json") for t in tickets])
    _write(ROOT / "state" / "customers.json",
           {k: v.model_dump(mode="json") for k, v in customers.items()})
    _write(ROOT / "state" / "categories.json",
           {k: v.model_dump(mode="json") for k, v in categories.items()})
    _write(ROOT / "state" / "ledger.json", ledger)

    _report(tickets, customers, categories)


# ------------------------------------------------------------------ generation


def _make_ticket(
    rng: random.Random,
    tid: str,
    customer_id: str,
    category: str,
    claimed_urgent: bool,
    severe: bool,
    stated_urgency: str,
) -> ResolvedTicket:
    """Sample operational facts that DERIVE to the intended verdict.

    We never write `was_severe`. We write users affected, whether code changed, and
    how long it took, then let estimation.was_actually_severe decide -- and assert
    below that it agrees. If someone moves that definition, this generator's output
    changes with it, which is the correct coupling.
    """
    median = CATEGORIES[category]["median_hours"]

    if severe:
        # over 10 users, or a code change taking more than a working day
        if rng.random() < 0.7:
            users = rng.randint(11, 900)
            code_changed = rng.random() < 0.6
            hours = _lognormal(rng, median, 0.55)
        else:
            users = rng.randint(0, 10)
            code_changed = True
            hours = max(8.5, _lognormal(rng, max(median, 10.0), 0.5))
    else:
        users = rng.randint(0, 10)
        code_changed = rng.random() < 0.35
        hours = _lognormal(rng, median, 0.5)
        if code_changed and hours > 8:
            # Would derive as severe. Drop the code change rather than truncating the
            # duration -- a slow ticket that turned out to need no code change is an
            # ordinary outcome, whereas rewriting 90h down to 3h would quietly destroy
            # the category's time distribution (and did, on the first run).
            code_changed = False

    t = ResolvedTicket(
        id=tid,
        customer_id=customer_id,
        category=category,
        stated_urgency=stated_urgency,
        claimed_urgent=claimed_urgent,
        actual_users_affected=users,
        code_changed=code_changed,
        hours_to_resolve=round(hours, 1),
        batch=rng.randint(*HISTORY_BATCHES),
    )
    assert was_actually_severe(t) == severe, (
        f"generator produced facts inconsistent with intended verdict for {tid}"
    )
    return t


def _lognormal(rng: random.Random, median: float, sigma: float) -> float:
    import math
    return max(0.2, median * math.exp(rng.gauss(0.0, sigma)))


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


# --------------------------------------------------------------------- derivation


def _derive_customers(tickets: list[ResolvedTicket]) -> dict[str, CustomerHistory]:
    out: dict[str, CustomerHistory] = {}
    for t in sorted(tickets, key=lambda x: (x.customer_id, x.batch, x.id)):
        if not t.claimed_urgent:
            continue
        hist = out.setdefault(t.customer_id, CustomerHistory())
        verdict = was_actually_severe(t)
        hist.observations.append(
            Observation(ticket=t.id, claimed=t.stated_urgency, verdict=verdict, batch=t.batch)
        )
        hist.urgency_claims += 1
        if verdict:
            hist.confirmed_severe += 1
        hist.last_updated_batch = max(hist.last_updated_batch, t.batch)
    for hist in out.values():
        hist.last_updated_batch = HISTORY_BATCHES[1]
    return out


def _derive_categories(tickets: list[ResolvedTicket]) -> dict[str, CategoryStats]:
    buckets: dict[str, list[ResolvedTicket]] = {}
    for t in tickets:
        buckets.setdefault(t.category, []).append(t)
    return {
        cat: CategoryStats(
            median_hours=round(statistics.median(x.hours_to_resolve for x in rows), 1),
            severe_rate=round(sum(was_actually_severe(x) for x in rows) / len(rows), 2),
            n=len(rows),
        )
        for cat, rows in sorted(buckets.items())
    }


def _seed_ledger() -> dict:
    """Two tickets carried into the live batches with existing waiting debt.

    TKT-4471 is the fairness case: Verdant Home, first seen in batch 35, skipped six
    times since. At the `as_of` clock it has waited ~112h -- inside the charter's
    120h ceiling, but one batch away from breaching it. That is deliberate: batch 42
    shows the debt climbing, batch 43 shows the charter catching it.
    """
    return {
        "TKT-4471": {
            "first_seen_batch": 35,
            "times_skipped": 6,
            "first_seen_at": "2026-08-14T22:00:00Z",
            "last_batch_seen": 41,
        },
        "TKT-4463": {
            "first_seen_batch": 39,
            "times_skipped": 2,
            "first_seen_at": "2026-08-17T08:00:00Z",
            "last_batch_seen": 41,
        },
    }


# ------------------------------------------------------------------------ output


def _counts(tickets: list[ResolvedTicket]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tickets:
        out[t.category] = out.get(t.category, 0) + 1
    return out


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _report(tickets, customers, categories) -> None:
    print(f"seeded {len(tickets)} resolved tickets  (seed={SEED})\n")
    print("credibility, derived by counting the history:")
    for cust, hist in sorted(customers.items()):
        cred = (hist.confirmed_severe + 1) / (hist.urgency_claims + 2)
        print(f"  {cust:<12} {hist.confirmed_severe}/{hist.urgency_claims} confirmed -> {cred:.2f}")
    print("\ncategory stats, derived by counting the history:")
    for cat, st in sorted(categories.items()):
        target = CATEGORIES[cat]
        print(
            f"  {cat:<20} median {st.median_hours:>5.1f}h (target {target['median_hours']:>5.1f})  "
            f"severe {st.severe_rate:.2f} (target {target['severe_rate']:.2f})  n={st.n}"
        )
    print("\nwrote data/history/resolved_tickets.json, state/{customers,categories,ledger}.json")


if __name__ == "__main__":
    main()
