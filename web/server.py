"""A local demo server for the triage agent. Stdlib only -- no new dependencies.

WHY STDLIB: the whole claim of this repo is that the deterministic core runs with
no setup and no API key. A demo front end that needs `pip install fastapi` would
undercut that on the first line of the README. `python web/server.py` is the
entire installation story.

WHY A SERVER AT ALL, rather than a static page with the scorer ported to JS: a
JavaScript reimplementation of `scorer.severity()` is a second implementation that
can silently disagree with the first. Then the demo is showing you a system that
does not exist.

This file used to make a weaker version of that promise and quietly break it. It
shared the scoring arithmetic but reimplemented the run around it: it hand-built its
Context, so ticket text skipped `ingest.sanitise` -- on the one surface a stranger can
type into; it called `state.ledger.clear()`, so the real fairness debt was discarded
and no carried-over ticket could ever appear; and it stopped after stage 7, so nothing
was ever written back. It now calls `pipeline.run_batch_full`, the same entry point
`triage run` calls, against a Context assembled by `Context.build`. A submission is a
real batch: sanitised, carried onto the live backlog, and persisted.

BECAUSE IT PERSISTS, submitting mutates `state/` exactly as the CLI does. That is the
point -- fairness debt only exists across a series of decisions -- but it means the
demo drifts as you use it. `POST /api/reset` reseeds, and the response carries a
receipt naming what was written.

WHERE THE LLM DOES AND DOES NOT ACT: the page takes plain ticket text, so something
has to turn that into four sources. `web/autofill.py` does, using the model when a
key is present and keyword heuristics when it is not. That step is READING ONLY --
it never touches the ranking. Scoring, the charter and the ordering stay entirely
deterministic, and the page renders which half of each ticket was read from the text
and which half was invented, because those are not the same kind of claim. The
pipeline's OWN judgement stages now run too when a key is present; `degraded_stages`
names the ones that did not.
"""

from __future__ import annotations

import json
import math
import random
import sys
import threading
import traceback
from datetime import timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from triage.env import load_dotenv  # noqa: E402

load_dotenv()

from triage import estimation, paths  # noqa: E402
from triage.config import load_charter, load_company_state, load_posture, load_postures  # noqa: E402
from triage.models import (  # noqa: E402
    Batch,
    CrmRecord,
    Telemetry,
    Ticket,
    Tier,
    Urgency,
)
from triage.pipeline import (  # noqa: E402
    Context,
    NoPostureChosen,
    RunOptions,
    next_ad_hoc_batch,
    run_batch_full,
)
from triage.report import write as write_report  # noqa: E402
from triage.sources import SourceBundle  # noqa: E402
from triage.state import State  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from autofill import autofill  # noqa: E402
from triage.llm.client import LLMClient  # noqa: E402

WEB = Path(__file__).resolve().parent

#: A submission reads state/, decides, and writes it back. Two in flight at once would
#: interleave those steps and lose one run's fairness debt -- ThreadingHTTPServer makes
#: that reachable by double-clicking the button.
_RUN_LOCK = threading.Lock()

def categories() -> list[str]:
    """The categories the scorer actually has base rates for.

    Read from state/, never hardcoded. The hardcoded list this replaces named eight
    and claimed to be "categories the scorer has base rates for"; state/categories.json
    holds eleven. `payment_processing` (severe_rate 0.73), `account_access` and
    `shipping_config` could not be expressed through the form at all, and anything
    unlisted was silently rewritten to `integration_api` -- which swaps an honest n=0
    prior for a real measurement of a different category.
    """
    return sorted(State.load(paths.state_dir()).categories)


# --------------------------------------------------------------- building a run


def _build_context(payload: dict) -> tuple[Context, list[str]]:
    """Turn front-end JSON into a real Context. Notes explain any defaulting, so a
    number on screen is never the product of a silent assumption.

    Every structural decision here now belongs to `Context.build`: sanitisation, the
    per-batch company state, loading source D, and dragging in the backlog. What is
    left is the one thing genuinely local to this path -- turning five free-text
    messages into sources A, B and C, and being explicit about which of those were
    read and which were invented.
    """
    notes: list[str] = []
    rows = payload.get("tickets", [])[:5]
    if not rows:
        raise ValueError("no tickets submitted")

    state = State.load(paths.state_dir())
    batch_id, as_of = next_ad_hoc_batch(state, step_hours=_f(payload.get("advance_hours"), 4.0))
    known_categories = sorted(state.categories)

    tickets: list[Ticket] = []
    telemetry: dict[str, Telemetry] = {}
    crm: dict[str, CrmRecord] = {}

    #: Attributing a ticket to a merchant who actually exists is the difference
    #: between a demo where credibility is inert and one where it bites. A known id
    #: brings its REAL billing record and its REAL claim history, so the same words
    #: from NorthPeak (2 of 9 claims confirmed) and from soloartisan (2 of 2) are
    #: scored differently -- which is the entire point of the credibility term.
    real = SourceBundle.load()

    for i, r in enumerate(rows):
        #: Scoped to the batch. `TKT-U1` was fine while every run was thrown away, but
        #: a persisted submission leaves ledger and pending rows behind, and the next
        #: submission's `TKT-U1` would inherit the previous one's debt.
        tid = f"TKT-W{batch_id}-{i + 1}"
        claimed_cid = (r.get("customer_id") or "").strip()
        known = claimed_cid in real.crm
        cid = claimed_cid if known else f"cust-w{batch_id}-{i + 1}"
        body = (r.get("message") or "").strip()
        if not body:
            continue

        category = r.get("category") or "integration_api"
        if category not in known_categories:
            notes.append(f"{tid}: unknown category {category!r}, used integration_api")
            category = "integration_api"

        hours_waited = _f(r.get("waited_hours"), 1.0)

        # Same trap as _f: `not in (None, "", False)` dropped a deadline of 0, which
        # is the single most urgent value this field can carry.
        compliance_h = r.get("compliance_deadline_hours")
        has_deadline = not (
            compliance_h is None or compliance_h is True or compliance_h is False
            or (isinstance(compliance_h, str) and not compliance_h.strip())
        )
        compliance_at = as_of + timedelta(hours=_f(compliance_h, 0.0)) if has_deadline else None

        tickets.append(
            Ticket(
                id=tid,
                customer_id=cid,
                subject=(r.get("subject") or body[:60]).strip() or "(no subject)",
                body=body,
                category=category,
                stated_urgency=Urgency(r.get("stated_urgency") or "medium"),
                blocks_paying_workflow=bool(r.get("blocks_paying_workflow")),
                submitted_at=as_of - timedelta(hours=hours_waited),
                compliance_deadline_at=compliance_at,
            )
        )
        r["ticket_id"] = tid

        #: The badge must mean what it says. `telemetry_confirmed: False` is a claim
        #: that instruments found NOTHING -- so the simulated evidence has to actually
        #: read that way, not just carry a label next to unchanged numbers. Defaults
        #: True: a direct API call that never set the flag is not silently zeroed.
        #:
        #: A manually-checked "telemetry confirms exposure" is a declared fact from
        #: the operator, not a random assignment -- it must survive being marked a
        #: false positive by the (unrelated) random draw, or checking the box could
        #: be silently overruled by chance.
        confirmed = r.get("telemetry_confirmed", True)
        declared_exposure = bool(r.get("security_exposure"))
        telemetry[tid] = (
            Telemetry(
                ticket_id=tid,
                security_exposure=declared_exposure,
                notes="marked false positive -- instruments found nothing"
                if not declared_exposure else "false positive, except a manually confirmed exposure",
            )
            if not confirmed
            else Telemetry(
                ticket_id=tid,
                users_affected=int(_f(r.get("users_affected"), 0.0)),
                error_rate=_clamp01(_f(r.get("error_rate"), 0.0)),
                gmv_at_risk_per_hour=_f(r.get("gmv_at_risk_per_hour"), 0.0),
                error_signature=(r.get("error_signature") or None) or None,
                security_exposure=declared_exposure,
                data_loss=bool(r.get("data_loss")),
            )
        )
        if not confirmed:
            notes.append(
                f"{tid}: marked a false positive -- simulated instruments read as "
                f"absent rather than whatever autofill invented, so the claim stands "
                f"or falls on credibility alone"
            )
        if declared_exposure:
            notes.append(f"{tid}: telemetry confirms exposure -- manually declared, not read from the text")
        if known:
            # Real billing record wins outright. Nothing the model imagined about
            # tier or ARR may overwrite what the CRM actually says.
            crm[cid] = real.crm[cid]
            notes.append(
                f"{tid}: attributed to {real.crm[cid].name} -- real tier, ARR and "
                f"claim history apply; the model's guesses at those were discarded"
            )
        else:
            # An unclaimed sender has no billing record. If the model simulated one
            # it is used and the page labels it as simulated; if nothing supplied one,
            # it reads as ABSENT -- free tier, zero ARR -- rather than being invented
            # here. Mirrors SourceBundle.telemetry_for, which returns zeros for missing
            # instruments rather than making a measurement up.
            tier = r.get("tier")
            if tier not in [t.value for t in Tier]:
                tier = "free"
                notes.append(f"{tid}: no billing record -- tier and ARR read as absent, not guessed")
            crm[cid] = CrmRecord(
                customer_id=cid,
                name=(r.get("merchant") or f"merchant {i + 1}").strip(),
                tier=tier,
                arr=_f(r.get("arr"), 0.0),
            )

    if not tickets:
        raise ValueError("every ticket was blank")

    batch = Batch(
        batch_id=batch_id,
        label=f"batch {batch_id} - submitted from the demo front end",
        tickets=tickets,
        as_of=as_of,
    )

    # The real state, and the real backlog. This used to clone the state and then call
    # `state.ledger.clear()`, which threw away the only record of who has been waiting
    # -- so charter rule R2, the one rule that reads the ledger, had nothing to read
    # but numbers autofill had invented seconds earlier.
    ctx = Context.build(
        batch,
        sources=SourceBundle.merged(real, telemetry=telemetry, crm=crm),
        carry_backlog=True,
    )

    #: Capacity is the company's, not the page's -- but the slider is the whole point
    #: of the demo, so an explicit value still wins.
    if payload.get("capacity") is None:
        notes.append(
            f"capacity {ctx.capacity} taken from company state, not from the page"
        )
    else:
        ctx.capacity = max(0, int(_f(payload.get("capacity"), ctx.capacity)))

    if ctx.carried:
        notes.append(
            f"{len(ctx.carried)} ticket(s) carried in from previous batches: "
            f"{', '.join(ctx.carried)} -- these were deferred earlier and are still "
            f"waiting, and their fairness debt is real rather than declared"
        )
    if any(t.customer_id.startswith("cust-w") for t in tickets):
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
    telemetry_confirmed_map: list[bool] = []   # one entry per textbox slot (0-4)
    if payload.get("autofill"):
        sent = [t for t in payload.get("tickets", [])[:5] if (t.get("message") or "").strip()]
        rows, fill_note, used_llm = autofill([t.get("message", "") for t in sent])
        if not rows:
            raise ValueError("write at least one ticket")

        # ── telemetry confirmation ──────────────────────────────────────
        # Randomly designate exactly one of the submitted textbox inputs as a
        # false positive (telemetry_confirmed = false). The rest are confirmed
        # real claims.  Linked to textbox indices, not ticket IDs.
        n = len(rows)
        false_positive_idx = random.randrange(n)
        telemetry_confirmed_map = [True] * n
        telemetry_confirmed_map[false_positive_idx] = False
        for idx, row in enumerate(rows):
            row["telemetry_confirmed"] = telemetry_confirmed_map[idx]

        # The sender is chosen by the person using the page, not read out of the
        # prose. Autofill returns fresh rows, so carry the attribution across --
        # dropping it here silently reverted every ticket to an invented merchant
        # at the 0.5 prior, which looks like the credibility term doing nothing.
        known_crm = SourceBundle.load().crm
        for row, original in zip(rows, sent):
            cid = original.get("customer_id")
            if cid:
                row["customer_id"] = cid
                if cid in known_crm:
                    # Overwrite what the model imagined about identity and billing, so
                    # the panel showing "what was read" cannot display an invented name
                    # and ARR beside a ticket whose real record the scorer actually used.
                    rec = known_crm[cid]
                    row["merchant"], row["tier"], row["arr"] = rec.name, rec.tier.value, rec.arr
            if original.get("security_exposure"):
                # A declared fact, not a reading of the text -- the "telemetry
                # confirms exposure" checkbox. It can only ADD an exposure autofill
                # did not simulate, never remove one autofill did: the checkbox is
                # unchecked by default, so absence means "not asserted", not
                # "confirmed clean".
                row["security_exposure"] = True
        payload = {**payload, "tickets": rows}

    #: No default. `pipeline.run_batch` refuses to pick a posture because the weights
    #: ARE the ethics, and this file used to quietly pick `revenue_first` on the user's
    #: behalf -- the one thing the whole system says it will not do.
    requested = payload.get("posture")
    if requested:
        load_posture(requested)  # KeyError here is a clean 400, not a stage-5 traceback

    #: The pipeline's judgement stages CAN run here now -- that is the point of calling
    #: run_batch rather than reimplementing stages 6 and 7. But they are ~10 serial
    #: model calls (extract, correlate, conflicts, advise, four critics, adjudicate,
    #: narrate) behind one blocking POST, which measured over two minutes per
    #: submission. Defaulting them on makes the page feel broken, so the form opts in
    #: and `degraded_stages` names what was skipped either way.
    client = LLMClient()
    use_llm = bool(payload.get("use_llm", False)) and client.available

    # Build, score and persist as one critical section. Two submissions in flight would
    # both read the ledger, both decide, and the second would overwrite the first.
    with _RUN_LOCK:
        ctx, notes = _build_context(payload)
        # Snapshot before the run: stage 11 retires served tickets out of the ledger,
        # so reading `first_seen_batch` afterwards reports None for exactly the carried
        # tickets that did best -- the ones most worth explaining.
        first_seen = {
            tid: e.first_seen_batch for tid, e in ctx.state.ledger.items()
        }
        report, orderings_by_posture = run_batch_full(
            ctx,
            RunOptions(
                posture=requested,
                use_llm=use_llm,
                assume_yes=True,   # the dropdown IS the confirmation
                persist_state=True,
                confirm=None,
            ),
        )
        md_path, _ = write_report(report)
        served, deferred = [r.ticket_id for r in report.ordering.ranked if r.served], [
            r.ticket_id for r in report.ordering.ranked if not r.served
        ]

    crm_names = {cid: rec.name for cid, rec in ctx.sources.crm.items()}
    carried = set(ctx.carried)
    orderings = {
        name: _ordering_json(o, crm_names, carried, first_seen)
        for name, o in sorted(orderings_by_posture.items())
    }
    est_by_id = {r.ticket_id: r.scored.estimate for r in report.ordering.ranked}

    return {
        **report.model_dump(mode="json"),
        "posture": report.posture,
        "capacity": ctx.capacity,
        "orderings": orderings,
        "resolved": payload.get("tickets", []),
        "carried": [_carried_json(ctx, tid, est_by_id, first_seen) for tid in ctx.carried],
        "autofill_note": fill_note,
        "autofill_used_llm": used_llm,
        "telemetry_confirmed": telemetry_confirmed_map,
        "postures": {n: p.model_dump() for n, p in sorted(load_postures().items())},
        # A mutation with no receipt is how a demo drifts without anyone noticing.
        "persisted": {
            "batch_id": report.batch_id,
            "as_of": report.as_of.isoformat(),
            "served": served,
            "deferred": deferred,
            "report": str(md_path.relative_to(paths.repo_root())),
        },
        "notes": notes,
    }


def _carried_json(ctx: Context, tid: str, est_by_id: dict, first_seen: dict[str, int | None]) -> dict:
    """A ticket dragged in from the waiting room, with the text it arrived with.

    Rendered above the ranking so an old ticket never appears unexplained. This is the
    thing the page could not previously show at all.

    `first_seen` is a snapshot taken BEFORE the run: stage 11 pops served tickets out
    of the ledger, so reading it afterwards would report None for exactly the carried
    tickets that did best.
    """
    ticket = ctx.ticket(tid)
    est = est_by_id.get(tid)
    return {
        "ticket_id": tid,
        "merchant": ticket.customer_id,
        "merchant_name": ctx.sources.crm[ticket.customer_id].name
        if ticket.customer_id in ctx.sources.crm else ticket.customer_id,
        "subject": ticket.subject,
        "body": ticket.body,
        "category": ticket.category,
        "stated_urgency": ticket.stated_urgency.value,
        "first_seen_batch": first_seen.get(tid),
        "times_skipped": est.times_skipped if est else 0,
        "waiting_hours": round(est.waiting_hours, 1) if est else None,
        "live_telemetry": ctx.sources.has_telemetry(tid),
    }


def _ordering_json(
    ordering,
    names: dict[str, str] | None = None,
    carried: set[str] | None = None,
    first_seen: dict[str, int] | None = None,
) -> dict:
    """The page's view of an Ordering.

    Kept, and kept here: it is the only producer of the `facts` block the page reads,
    and a rendering adapter is not a second implementation of the logic. What was
    deleted is the scoring loop that used to sit above it.
    """
    names = names or {}
    carried = carried or set()
    first_seen = first_seen or {}
    rows = []
    for r in ordering.ranked:
        est = r.scored.estimate
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
                    "attributed": not est.customer_id.startswith("cust-w"),
                    "carried": est.ticket_id in carried,
                    "first_seen_batch": first_seen.get(est.ticket_id),
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


def _reset(payload: dict) -> dict:
    """Put the demo back to its seeded baseline.

    Necessary rather than a nicety: submissions persist, so without this the demo
    drifts with use and the numbers in the README stop matching what the page shows.
    Runs the same seeder CI runs, so "reset" and "reproducible" mean the same thing.

    Deliberately does NOT touch data/eval/, data/sources/ or config/ -- those are
    inputs, not state. The decision log is a delete, so it needs its own opt-in.
    """
    if not payload.get("confirm"):
        raise ValueError("reset requires {\"confirm\": true} -- it discards the current state")

    sys.path.insert(0, str(REPO / "scripts"))
    import seed_history

    #: `paths.repo_root()`, not the module-level `REPO` -- the latter is fixed to
    #: this file's real location on disk. A test that redirects TRIAGE_ROOT to an
    #: isolated tmpdir must have reset act on that tmpdir, not reseed the real repo
    #: out from under every other test and the developer's own working tree.
    root = paths.repo_root()
    with _RUN_LOCK:
        seed_history.main(root=root, quiet=True)
        cleared_log = False
        if payload.get("clear_log"):
            log = paths.decision_log_path()
            if log.exists():
                log.unlink()
                cleared_log = True

    st = State.load(paths.state_dir())
    return {
        "reset": True,
        "restored": ["state/customers.json", "state/categories.json", "state/ledger.json",
                     "state/pending.json", "state/meta.json", "data/history/resolved_tickets.json"],
        "decision_log_cleared": cleared_log,
        "next_batch_id": st.meta.last_batch_id + 1,
        "as_of": st.meta.as_of.isoformat() if st.meta.as_of else None,
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
                # `State.customer` already returns an empty history for an unknown id,
                # and `estimation.credibility` is the one definition of this number.
                # Computing it a second time here, at a different precision, meant the
                # dropdown and the ranking could disagree in the last digit about the
                # single figure the whole demo is built to showcase.
                h = st.customer(cid)
                merchants.append({
                    "id": cid, "name": rec.name, "tier": rec.tier.value, "arr": rec.arr,
                    "credibility": round(estimation.credibility(h), 3),
                    "claims": h.urgency_claims, "confirmed": h.confirmed_severe,
                })
            return self._json(
                {
                    "merchants": sorted(merchants, key=lambda m: -m["arr"]),
                    "llm_available": client.available,
                    "model": client.model,
                    "categories": categories(),
                    "postures": {n: p.model_dump() for n, p in sorted(load_postures().items())},
                    "charter": load_charter(),
                    "tiers": [t.value for t in Tier],
                    "urgencies": [u.value for u in Urgency],
                    # The page shows where the sequence has got to, because every
                    # submission now moves it.
                    "batch": {
                        "next_batch_id": st.meta.last_batch_id + 1,
                        "as_of": st.meta.as_of.isoformat() if st.meta.as_of else None,
                        "waiting": sorted(st.pending),
                    },
                }
            )
        self.send_error(404)

    def do_POST(self):
        if self.path not in ("/api/triage", "/api/reset"):
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(
                self.rfile.read(n) or b"{}",
                # python's json accepts bare NaN/Infinity on the way IN as well.
                # Refuse them at the boundary rather than letting one reach the scorer.
                parse_constant=_reject_constant,
            )
            if self.path == "/api/reset":
                return self._json(_reset(payload))
            return self._json(_run(payload))
        except NoPostureChosen as exc:
            # User error, not a crash. The message already names the six postures.
            return self._json({"error": str(exc)}, status=400)
        except Exception as exc:  # surfaced to the page, not swallowed
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, status=400)

    def _json(self, obj, status=200):
        # allow_nan=False: python happily emits bare NaN and Infinity, which JSON
        # forbids. Any strict parser rejects the whole response, and a browser's
        # parser accepts it and renders "NaN" in the score column as though it were
        # a number the scorer meant. Better to fail here than to ship either.
        body = json.dumps(obj, default=str, allow_nan=False).encode("utf-8")
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
    """Read a number, or fall back to `default` when the field is genuinely absent.

    ABSENT means None, an empty string, or a bare boolean. It does NOT mean zero, and
    the distinction is not academic. `v in (None, "", False)` compares by equality, and
    `0 == False` in Python, so every zero a caller sent was silently replaced by the
    default: capacity 0 became 3 agents, `waited_hours: 0` became 1 hour, and a
    compliance deadline of 0 -- due right now -- was dropped so the charter never saw
    it. Zero is a measurement. Only `is` may be used here.
    """
    if v is None or v is True or v is False or (isinstance(v, str) and not v.strip()):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    # NaN and +/-inf survive float() and then poison every arithmetic they touch,
    # producing a score of NaN that sorts unpredictably and renders as "NaN".
    return f if math.isfinite(f) else default


def _reject_constant(name: str):
    raise ValueError(f"{name} is not a number this system will accept as an input")


#: One definition, in estimation.py. This was a third copy.
_clamp01 = estimation._clamp01


def main(port: int = 8000) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    client = LLMClient()
    print(f"triage demo on http://127.0.0.1:{port}")
    if client.available:
        print(f"full pipeline via run_batch() -- {client.model}")
    else:
        print("no API key: deterministic core only; the report names the stages that did not run")
    print("submissions WRITE to state/ -- POST /api/reset, or `python scripts/seed_history.py`, restores it")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
