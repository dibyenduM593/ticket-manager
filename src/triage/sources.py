"""The four independent context sources.

    A  Ticket message      data/batches/*.json      what the merchant CLAIMS
    B  Telemetry           data/sources/telemetry   what the platform MEASURED
    C  CRM / billing       data/sources/crm.json    what the relationship is WORTH
    D  Resolution history  state/*.json             what this account/category DOES

They are independent in the sense that matters: no one of them can be derived from
another, and each is authored by a different party with different incentives. That is
what makes them capable of contradicting each other.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths
from .models import CrmRecord, Telemetry


class SourceBundle:
    """B and C, loaded. D lives in state.State; A arrives with the batch."""

    def __init__(self, telemetry: dict[str, Telemetry], crm: dict[str, CrmRecord]) -> None:
        self.telemetry = telemetry
        self.crm = crm

    @classmethod
    def load(cls, directory: Path | str | None = None) -> "SourceBundle":
        d = Path(directory) if directory else paths.sources_dir()
        tele_raw = _read(d / "telemetry.json")
        crm_raw = _read(d / "crm.json")
        return cls(
            telemetry={k: Telemetry.model_validate({"ticket_id": k, **v}) for k, v in tele_raw.items()},
            crm={k: CrmRecord.model_validate({"customer_id": k, **v}) for k, v in crm_raw.items()},
        )

    def telemetry_for(self, ticket_id: str) -> Telemetry:
        """A ticket with no telemetry row is not an error -- it means the instruments
        saw nothing, which is itself a finding. Returning zeros makes the scorer treat
        it as an unsupported claim, which is exactly right."""
        return self.telemetry.get(ticket_id, Telemetry(ticket_id=ticket_id, notes="no telemetry recorded"))

    def crm_for(self, customer_id: str) -> CrmRecord:
        rec = self.crm.get(customer_id)
        if rec is None:
            raise KeyError(f"no CRM record for customer {customer_id!r}")
        return rec

    def has_telemetry(self, ticket_id: str) -> bool:
        return ticket_id in self.telemetry


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
