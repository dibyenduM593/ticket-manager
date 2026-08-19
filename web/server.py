"""A local demo server for the triage agent. Stdlib only -- no new dependencies.

WHY STDLIB: the whole claim of this repo is that the deterministic core runs with
no setup and no API key. A demo front end that needs `pip install fastapi` would
undercut that on the first line of the README. `python web/server.py` is the
entire installation story.

WHY A SERVER AT ALL, rather than a static page with the scorer ported to JS: a
JavaScript reimplementation of `scorer.severity()` is a second implementation that
can silently disagree with the first. Then the demo is showing you a system that
does not exist. Every number this server returns comes from importing and calling
the same `src/triage/` code the CLI calls -- `estimation`, `valuation`, `scorer`,
`charter` -- so the page cannot show a ranking the CLI would not produce.

WHERE THE LLM DOES AND DOES NOT ACT: the page takes plain ticket text, so something
has to turn that into four sources. `web/autofill.py` does, using the model when a
key is present and keyword heuristics when it is not. That step is READING ONLY --
it never touches the ranking. Scoring, the charter and the ordering stay entirely
deterministic, and the page renders which half of each ticket was read from the text
and which half was invented, because those are not the same kind of claim.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from triage.env import load_dotenv  # noqa: E402

load_dotenv()

from triage import correlate, paths, scorer  # noqa: E402
from triage.config import load_charter, load_company_state, load_posture, load_postures  # noqa: E402
from triage.models import (  # noqa: E402
    Batch,
    CrmRecord,
    Telemetry,
    Ticket,
    Urgency,
)
from triage.pipeline import Context, detect_conflicts_deterministic  # noqa: E402
from triage.sources import SourceBundle  # noqa: E402
from triage.state import State  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from autofill import autofill  # noqa: E402
from triage.llm.client import LLMClient  # noqa: E402

WEB = Path(__file__).resolve().parent
AS_OF = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)

#: Categories the scorer has base rates for. Offered as a closed list because a
#: typo'd category silently falls back to a zero severe-rate, which looks like a
#: judgement rather than the missing data it is.
CATEGORIES = [
    "checkout_failure",
    "integration_api",
    "data_integrity",
    "security",
    "compliance",
    "billing",
    "performance",
    "ui_cosmetic",
]


# --------------------------------------------------------------- building a run


def _build_context(payload: dict) -> tuple[Context, list[str]]:
    """Turn front-end JSON into a real Context. Notes explain any defaulting, so a
    number on screen is never the product of a silent assumption."""
    notes: list[str] = []
    rows = payload.get("tickets", [])[:5]
    if not rows:
        raise ValueError("no tickets submitted")

    tickets: list[Ticket] = []
    telemetry: dict[str, Telemetry] = {}
    crm: dict[str, CrmRecord] = {}
    ledger_skips: dict[str, int] = {}
    waited: dict[str, float] = {}

    #: Attributing a ticket to a merchant who actually exists is the difference
    #: between a demo where credibility is inert and one where it bites. A known id
    #: brings its REAL billing record and its REAL claim history, so the same words
    #: from NorthPeak (2 of 9 claims confirmed) and from soloartisan (2 of 2) are
    #: scored differently -- which is the entire point of the credibility term.
    real = SourceBundle.load()

    for i, r in enumerate(rows):
        tid = f"TKT-U{i + 1}"
        claimed_cid = (r.get("customer_id") or "").strip()
        known = claimed_cid in real.crm
        cid = claimed_cid if known else f"cust-u{i + 1}"
        body = (r.get("message") or "").strip()
        if not body:
            continue

        category = r.get("category") or "integration_api"
        if category not in CATEGORIES:
            notes.append(f"{tid}: unknown category {category!r}, used integration_api")
            category = "integration_api"

        hours_waited = _f(r.get("waited_hours"), 1.0)
        waited[tid] = hours_waited
        ledger_skips[tid] = int(_f(r.get("times_skipped"), 0.0))

        compliance_h = r.get("compliance_deadline_hours")
        compliance_at = (
            AS_OF + timedelta(hours=_f(compliance_h, 0.0))
            if compliance_h not in (None, "", False)
            else None
        )

        tickets.append(
            Ticket(
                id=tid,
                customer_id=cid,
                subject=(r.get("subject") or body[:60]).strip() or "(no subject)",
                body=body,
                category=category,
                stated_urgency=Urgency(r.get("stated_urgency") or "medium"),
                blocks_paying_workflow=bool(r.get("blocks_paying_workflow")),
                submitted_at=AS_OF - timedelta(hours=hours_waited),
                compliance_deadline_at=compliance_at,
            )
        )
        telemetry[tid] = Telemetry(
            ticket_id=tid,
            users_affected=int(_f(r.get("users_affected"), 0.0)),
            error_rate=_clamp01(_f(r.get("error_rate"), 0.0)),
            gmv_at_risk_per_hour=_f(r.get("gmv_at_risk_per_hour"), 0.0),
            error_signature=(r.get("error_signature") or None) or None,
            security_exposure=bool(r.get("security_exposure")),
            data_loss=bool(r.get("data_loss")),
        )
        if known:
            # Real billing record wins outright. Nothing the model imagined about
            # tier or ARR may overwrite what the CRM actually says.
            crm[cid] = real.crm[cid]
            notes.append(
                f"{tid}: attributed to {real.crm[cid].name} -- real tier, ARR and "
                f"claim history apply; the model's guesses at those were discarded"
            )
        else:
            crm[cid] = CrmRecord(
                customer_id=cid,
                name=(r.get("merchant") or f"merchant {i + 1}").strip(),
                tier=r.get("tier") or "growth",
                arr=_f(r.get("arr"), 0.0),
            )

    if not tickets:
        raise ValueError("every ticket was blank")

    batch = Batch(batch_id=900, label="ad-hoc batch from the demo front end", tickets=tickets, as_of=AS_OF)

    # A fresh ledger, then the waiting/skip history the user declared. Credibility
    # stays at the 0.5 prior for these invented merchants: we have no history on
    # them, and saying so is more honest than defaulting to trust or suspicion.
    state = State.load(paths.state_dir())
    state = state.clone()
    state.ledger.clear()
    state.see_tickets([t.id for t in tickets], batch.batch_id, {t.id: t.submitted_at for t in tickets})
    for tid, skips in ledger_skips.items():
        entry = state.ledger.get(tid)
        if entry is not None:
            entry.times_skipped = skips

    company = load_company_state()
    ctx = Context(batch=batch, company=company, sources=SourceBundle(telemetry, crm), state=state)
    ctx.capacity = max(0, int(_f(payload.get("capacity"), 3.0)))
    if any(t.customer_id.startswith("cust-u") for t in tickets):
        notes.append(
            "unattributed tickets sit at the 0.5 credibility prior -- an invented "
            "merchant has no claim history, and saying so is more honest than "
            "defaulting to trust or to suspicion"
        )
    return ctx, notes


def _run(payload: dict) -> dict:
    #: The page now sends raw text and nothing else. Reading it into four sources is
    #: a separate, clearly-labelled step -- see web/autofill.py for why the claimed
    #: half and the simulated half must not be confused.
    fill_note, used_llm = None, False
    if payload.get("autofill"):
        sent = [t for t in payload.get("tickets", [])[:5] if (t.get("message") or "").strip()]
        rows, fill_note, used_llm = autofill([t.get("message", "") for t in sent])
        if not rows:
            raise ValueError("write at least one ticket")
        # The sender is chosen by the person using the page, not read out of the
        # prose. Autofill returns fresh rows, so carry the attribution across --
        # dropping it here silently reverted every ticket to an invented merchant
        # at the 0.5 prior, which looks like the credibility term doing nothing.
        known_crm = SourceBundle.load().crm
        for row, original in zip(rows, sent):
            cid = original.get("customer_id")
            if not cid:
                continue
            row["customer_id"] = cid
            if cid in known_crm:
                # Overwrite what the model imagined about identity and billing, so
                # the panel showing "what was read" cannot display an invented name
                # and ARR beside a ticket whose real record the scorer actually used.
                rec = known_crm[cid]
                row["merchant"], row["tier"], row["arr"] = rec.name, rec.tier.value, rec.arr
        payload = {**payload, "tickets": rows}

    ctx, notes = _build_context(payload)
    requested = payload.get("posture") or "revenue_first"

    clusters = correlate.cluster_by_signature([t.id for t in ctx.batch.tickets], ctx.sources.telemetry)
    estimates = ctx.estimates()
    est_by_id = {e.ticket_id: e for e in estimates}
    conflicts = detect_conflicts_deterministic(estimates)

    crm_names = {cid: rec.name for cid, rec in ctx.sources.crm.items()}
    orderings: dict[str, dict] = {}
    for name, posture in sorted(load_postures().items()):
        scored = scorer.score_tickets(estimates, posture, clusters, ctx.charter)
        ordering = scorer.rank(scored, posture, ctx.capacity, ctx.charter)
        orderings[name] = _ordering_json(ordering, est_by_id, crm_names)

    return {
        "posture": requested,
        "capacity": ctx.capacity,
        "orderings": orderings,
        "resolved": payload.get("tickets", []),
        "autofill_note": fill_note,
        "autofill_used_llm": used_llm,
        "postures": {n: p.model_dump() for n, p in sorted(load_postures().items())},
        "conflicts": [c.model_dump() for c in conflicts],
        "clusters": [c.model_dump() for c in clusters],
        "notes": notes,
        "degraded": [
            "no API key: conflicts came from the rule-based detector, which finds "
            "9 of 21 planted contradictions in the shipped batches",
            "no LLM critique, adjudication or narration -- this is the deterministic floor",
        ],
    }


def _ordering_json(ordering, est_by_id, names: dict[str, str] | None = None) -> dict:
    names = names or {}
    rows = []
    for r in ordering.ranked:
        est = est_by_id[r.ticket_id]
        comp = r.scored.components
        rows.append(
            {
                "rank": r.rank,
                "ticket_id": r.ticket_id,
                "merchant": r.scored.estimate.customer_id,
                "merchant_name": names.get(r.scored.estimate.customer_id, r.scored.estimate.customer_id),
                "score": round(r.score, 4),
                "served": r.served,
                "charter_promoted": r.charter_promoted,
                "why": r.justification,
                "severity": round(r.scored.severity.severity, 4),
                "claimed": round(r.scored.severity.claimed, 4),
                "observed": round(r.scored.severity.observed, 4),
                "charter_floor": round(r.scored.severity.charter_floor, 4),
                "components": {k: round(v, 4) for k, v in comp.as_dict().items()},
                "facts": {
                    "tier": est.tier.value,
                    "arr": est.arr,
                    "users_affected": est.users_affected,
                    "error_rate": est.error_rate,
                    "gmv_at_risk_per_hour": est.gmv_at_risk_per_hour,
                    "waiting_hours": round(est.waiting_hours, 1),
                    "times_skipped": est.times_skipped,
                    "credibility": round(est.credibility, 3),
                    "credibility_evidence": est.credibility_evidence,
                    "attributed": not est.customer_id.startswith("cust-u"),
                    "security_exposure": est.security_exposure,
                    "data_loss": est.data_loss,
                    "category": est.category,
                    "category_severe_rate": round(est.category_severe_rate, 3),
                },
            }
        )
    return {
        "ranked": rows,
        "overrides": [
            {
                "ticket_id": o.ticket_id,
                "rule_id": o.rule_id,
                "clause_id": o.clause_id,
                "rule_name": o.rule_name,
                "statement": o.statement,
                "from_rank": o.from_rank,
                "to_rank": o.to_rank,
                "detail": o.detail,
            }
            for o in ordering.overrides
        ],
    }


# ------------------------------------------------------------ the shipped demo


def _shipped() -> dict:
    """The three committed batches, read from reports/. Read, not recomputed: this
    tab is showing what the CLI actually produced and committed."""
    out = []
    for n in (42, 43, 44):
        p = REPO / "reports" / f"batch_{n}.json"
        if p.exists():
            out.append(json.loads(p.read_text(encoding="utf-8")))
    audit_p = REPO / "reports" / "posture_audit.json"
    evalp = REPO / "reports" / "eval_conflicts.json"
    return {
        "batches": out,
        "audit": json.loads(audit_p.read_text(encoding="utf-8")) if audit_p.exists() else None,
        "eval": json.loads(evalp.read_text(encoding="utf-8")) if evalp.exists() else None,
        "charter": load_charter(),
        "postures": {n: p.model_dump() for n, p in sorted(load_postures().items())},
    }


# ------------------------------------------------------------------------ http


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._file(WEB / "index.html", "text/html; charset=utf-8")
        if self.path == "/api/meta":
            client = LLMClient()
            bundle = SourceBundle.load()
            st = State.load(paths.state_dir())
            merchants = []
            for cid, rec in bundle.crm.items():
                h = st.customers.get(cid)
                claims = getattr(h, "urgency_claims", 0) if h else 0
                confirmed = getattr(h, "confirmed_severe", 0) if h else 0
                merchants.append({
                    "id": cid, "name": rec.name, "tier": rec.tier.value, "arr": rec.arr,
                    "credibility": round((confirmed + 1) / (claims + 2), 2),
                    "claims": claims, "confirmed": confirmed,
                })
            return self._json(
                {
                    "merchants": sorted(merchants, key=lambda m: -m["arr"]),
                    "llm_available": client.available,
                    "model": client.model,
                    "categories": CATEGORIES,
                    "postures": {n: p.model_dump() for n, p in sorted(load_postures().items())},
                    "charter": load_charter(),
                    "tiers": ["free", "starter", "growth", "enterprise"],
                    "urgencies": [u.value for u in Urgency],
                }
            )
        if self.path == "/api/shipped":
            return self._json(_shipped())
        self.send_error(404)

    def do_POST(self):
        if self.path != "/api/triage":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            return self._json(_run(payload))
        except Exception as exc:  # surfaced to the page, not swallowed
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, status=400)

    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        if not path.exists():
            return self.send_error(404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _f(v, default: float) -> float:
    try:
        if v in (None, "", False):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def main(port: int = 8000) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"triage demo on http://127.0.0.1:{port}")
    print("deterministic path only -- no API key, no LLM stages")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
