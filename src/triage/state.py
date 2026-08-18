"""Read and write `state/` -- the learned, factual side of the system.

Two invariants enforced here:

1. The LLM never writes to state/. Every mutation goes through `apply_batch_outcome`
   or `record_observation`, both of which take validated primitives and do the
   arithmetic in Python. An LLM may *propose* an observation as a constrained enum;
   it may not increment a counter.

2. Nothing derived is stored. Credibility is computed on read (see estimation.py),
   never persisted, so it can never drift from the counts it claims to summarise.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .models import CategoryStats, CustomerHistory, LedgerEntry, Observation, Urgency


class State:
    """The three state files, loaded together and saved together."""

    def __init__(
        self,
        customers: dict[str, CustomerHistory],
        categories: dict[str, CategoryStats],
        ledger: dict[str, LedgerEntry],
        root: Path | None = None,
    ) -> None:
        self.customers = customers
        self.categories = categories
        self.ledger = ledger
        self.root = root or paths.state_dir()

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, root: Path | str | None = None) -> "State":
        d = Path(root) if root else paths.state_dir()
        customers = {
            k: CustomerHistory.model_validate(v)
            for k, v in _read_json(d / "customers.json", {}).items()
        }
        categories = {
            k: CategoryStats.model_validate(v)
            for k, v in _read_json(d / "categories.json", {}).items()
        }
        ledger = {
            k: LedgerEntry.model_validate(v)
            for k, v in _read_json(d / "ledger.json", {}).items()
        }
        return cls(customers, categories, ledger, root=d)

    def save(self, root: Path | str | None = None) -> None:
        d = Path(root) if root else self.root
        d.mkdir(parents=True, exist_ok=True)
        _write_json(d / "customers.json", {k: v.model_dump(mode="json") for k, v in self.customers.items()})
        _write_json(d / "categories.json", {k: v.model_dump(mode="json") for k, v in self.categories.items()})
        _write_json(d / "ledger.json", {k: v.model_dump(mode="json") for k, v in self.ledger.items()})

    def clone(self) -> "State":
        """Deep copy. Used by `compare`, which must never mutate real state."""
        return State(
            {k: v.model_copy(deep=True) for k, v in self.customers.items()},
            {k: v.model_copy(deep=True) for k, v in self.categories.items()},
            {k: v.model_copy(deep=True) for k, v in self.ledger.items()},
            root=self.root,
        )

    # ------------------------------------------------------------------ reading

    def customer(self, customer_id: str) -> CustomerHistory:
        """An account we have never seen gets an empty history, not an error.

        With Beta(1,1) smoothing that lands them at credibility 0.5 -- neutral --
        rather than at 0.0 or at an undefined division by zero.
        """
        return self.customers.get(customer_id, CustomerHistory())

    def category(self, name: str) -> CategoryStats:
        """Unknown category falls back to a deliberately uninformative prior:
        24h median, 0.5 severe rate, n=0. The n=0 is the honest part -- it lets the
        report say "we have no history for this category" instead of inventing one."""
        return self.categories.get(name, CategoryStats(median_hours=24.0, severe_rate=0.5, n=0))

    def ledger_entry(self, ticket_id: str) -> LedgerEntry | None:
        return self.ledger.get(ticket_id)

    # ------------------------------------------------------------------ writing

    def see_tickets(self, ticket_ids: list[str], batch_id: int, submitted_at: dict[str, datetime]) -> None:
        """Register tickets as present in this batch. Creates ledger rows for new ones."""
        for tid in ticket_ids:
            entry = self.ledger.get(tid)
            if entry is None:
                self.ledger[tid] = LedgerEntry(
                    first_seen_batch=batch_id,
                    times_skipped=0,
                    first_seen_at=submitted_at.get(tid),
                    last_batch_seen=batch_id,
                )
            else:
                entry.last_batch_seen = batch_id

    def accrue_skips(self, deferred_ticket_ids: list[str]) -> None:
        """Fairness debt accrues only for tickets that were actually skipped.

        A ranking is not a decision. Without a capacity line, `times_skipped` would
        increment off a number grounded in nothing -- see scorer.capacity_split.
        """
        for tid in deferred_ticket_ids:
            entry = self.ledger.get(tid)
            if entry is not None:
                entry.times_skipped += 1

    def retire_served(self, served_ticket_ids: list[str]) -> None:
        """Served tickets leave the queue. They stay in customers.json as history."""
        for tid in served_ticket_ids:
            self.ledger.pop(tid, None)

    def record_observation(
        self,
        customer_id: str,
        ticket_id: str,
        claimed: Urgency,
        verdict: bool,
        batch_id: int,
    ) -> None:
        """Append one urgency claim and its verdict. Deterministic arithmetic only.

        `verdict` must come from `estimation.was_actually_severe`, which reads
        operational facts. Nothing here trusts a model's opinion of severity.
        """
        hist = self.customers.setdefault(customer_id, CustomerHistory())
        if any(o.ticket == ticket_id for o in hist.observations):
            return  # idempotent: re-running a batch must not double-count
        hist.observations.append(
            Observation(ticket=ticket_id, claimed=claimed, verdict=verdict, batch=batch_id)
        )
        hist.urgency_claims += 1
        if verdict:
            hist.confirmed_severe += 1
        hist.last_updated_batch = max(hist.last_updated_batch, batch_id)


# --------------------------------------------------------------------- file i/o


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
