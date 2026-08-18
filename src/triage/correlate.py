"""Correlation -- tickets are not independent.

Three merchants reporting checkout failures in one batch are not three mid-severity
tickets. They are ONE platform incident affecting three merchants, and the correct
severity is the aggregate.

This module holds the deterministic clusterer: group by identical error signature in
the same region within a time window. That is not a weaker version of the LLM stage,
it is the evidence the LLM stage is supposed to be citing anyway. The LLM adds a named
root cause and can spot clusters that share a cause without sharing a signature; when
it is unavailable, this runs alone and the pipeline proceeds unchanged.

Clustering also creates a fourth class of contradiction: tickets that disagree with
each OTHER about what is happening -- one merchant says payments are down, another on
the same infrastructure says everything is fine.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Cluster, Telemetry

#: Two tickets with the same signature more than this far apart are probably two
#: recurrences of a known bug rather than one live incident.
WINDOW_HOURS = 6.0


def cluster_by_signature(
    ticket_ids: list[str],
    telemetry: dict[str, Telemetry],
) -> list[Cluster]:
    """Deterministic clustering on shared error signature + region + time window.

    Returns only multi-member clusters; singletons are implied and are materialised by
    scorer._membership, so a caller can always pass this result straight through.
    """
    buckets: dict[tuple[str, str | None], list[str]] = {}
    for tid in ticket_ids:
        t = telemetry.get(tid)
        if t is None or not t.error_signature:
            continue
        buckets.setdefault((t.error_signature, t.region), []).append(tid)

    clusters: list[Cluster] = []
    for (signature, region), members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        for group in _split_by_window(members, telemetry):
            if len(group) < 2:
                continue
            clusters.append(
                Cluster(
                    cluster_id=f"sig::{signature}",
                    ticket_ids=sorted(group),
                    root_cause=f"shared error signature {signature} in {region or 'unknown region'}",
                    shared_evidence=[
                        f"{tid}: signature={signature}, region={region}, "
                        f"first_error_at={_iso(telemetry[tid].first_error_at)}"
                        for tid in sorted(group)
                    ],
                    confidence=1.0,
                )
            )
    return clusters


def _split_by_window(members: list[str], telemetry: dict[str, Telemetry]) -> list[list[str]]:
    """Split a signature bucket into groups whose first errors are close in time."""
    timed = [(telemetry[m].first_error_at, m) for m in members]
    known = sorted([(t, m) for t, m in timed if t is not None], key=lambda x: _utc(x[0]))
    unknown = [m for t, m in timed if t is None]

    groups: list[list[str]] = []
    last: datetime | None = None
    for ts, m in known:
        if groups and last is not None and _hours(ts, last) <= WINDOW_HOURS:
            groups[-1].append(m)
        else:
            groups.append([m])
        last = ts

    if unknown:
        # No timestamp means we cannot place it in a window. Grouping it on signature
        # alone is the right call: the signature is the strong evidence, the timestamp
        # was only ever disambiguating recurrences.
        if groups:
            groups[0].extend(unknown)
        else:
            groups.append(unknown)
    return groups


def merge_llm_clusters(
    deterministic: list[Cluster],
    proposed: list[Cluster],
    valid_ticket_ids: set[str],
) -> list[Cluster]:
    """Combine signature clusters with LLM-proposed ones.

    Deterministic clusters win every conflict: a ticket already grouped by hard shared
    evidence is not regrouped on a model's say-so. LLM clusters contribute only tickets
    nobody has claimed, and any ticket ID the model invented is dropped -- silently
    here, loudly in tests/test_contract.py::test_no_hallucinated_ticket_ids.
    """
    claimed: set[str] = set()
    out: list[Cluster] = []

    for cl in deterministic:
        members = [t for t in cl.ticket_ids if t in valid_ticket_ids]
        if len(members) < 2:
            continue
        out.append(cl.model_copy(update={"ticket_ids": members}))
        claimed.update(members)

    for cl in proposed:
        members = [t for t in cl.ticket_ids if t in valid_ticket_ids and t not in claimed]
        if len(members) < 2:
            continue
        out.append(cl.model_copy(update={"ticket_ids": sorted(members)}))
        claimed.update(members)

    return out


def _hours(a: datetime, b: datetime) -> float:
    return abs((_utc(a) - _utc(b)).total_seconds()) / 3600.0


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else "unknown"
