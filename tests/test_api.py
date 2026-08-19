"""The demo server's HTTP surface.

Two claims are worth a test here, and they are the two that would be embarrassing to
get wrong in front of someone:

  1. The endpoints answer, and `/api/triage` returns a real ordering produced by
     importing `src/triage/` -- not a fixture, not a cached run.
  2. Nothing the server returns was invented by the server. With no key and no model,
     an unattributed sender arrives with no tier and no ARR, and the response says so
     rather than filling in a plausible-looking merchant.

There are no webhooks in this system. Nothing calls out, nothing accepts a callback,
and the only inbound surface is the three routes below; if that changes, the contract
for it belongs in this file.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "web"))

from triage import config  # noqa: E402

import server as web_server  # noqa: E402


@pytest.fixture(scope="module")
def isolated_root(tmp_path_factory, monkeypatch_module):
    """The server now calls `run_batch_full` with `persist_state=True`, so a
    submission writes to `state/` exactly like the CLI does. Running these tests
    against the real repo would mutate the committed seed on every run and make CI's
    `git diff --exit-code -- state/` fail on a PR that never touched state/ by hand.

    Copies the whole input surface into a tmpdir and points TRIAGE_ROOT at it.
    `config.clear_caches()` on both sides of the swap matters: charter and posture
    config are `lru_cache`d against the real path, so without this a test that ran
    after any other config-touching test would score against the wrong charter.
    """
    root = tmp_path_factory.mktemp("triage_root")
    for name in ("state", "config", "data"):
        shutil.copytree(REPO / name, root / name)
    (root / "reports").mkdir()
    monkeypatch_module.setenv("TRIAGE_ROOT", str(root))
    config.clear_caches()
    yield root
    config.clear_caches()


@pytest.fixture(scope="module")
def monkeypatch_module():
    """pytest's `monkeypatch` is function-scoped; `base_url` and `isolated_root` are
    module-scoped so the server thread is not restarted per test. This is the
    documented escape hatch for a module-scoped patch."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def base_url(isolated_root):
    """A real socket on an ephemeral port.

    Calling the handler functions directly would test the functions and not the
    server; the routing table is exactly the part that silently rots.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def _reset_between_tests(base_url):
    """Every test starts from the seeded baseline.

    The server persists on every submission now -- that is the point, fairness debt
    only exists across a sequence -- but it means test order would otherwise decide
    outcomes: a ticket one test defers becomes backlog the next test carries in.
    """
    post(f"{base_url}/api/reset", {"confirm": True})
    yield


def get(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ------------------------------------------------------------------- the routes


def test_index_is_served(base_url):
    with urllib.request.urlopen(base_url, timeout=10) as resp:
        assert resp.status == 200
        assert b"<title" in resp.read()[:2000].lower()


def test_meta_declares_the_postures_and_the_charter(base_url):
    status, body = get(f"{base_url}/api/meta")
    assert status == 200
    assert body["postures"], "postures come from config/postures/, not from this file"
    assert body["charter"]["rules"]
    for posture in body["postures"].values():
        assert pytest.approx(sum(posture["weights"].values()), abs=1e-9) == 1.0


def test_unknown_routes_404(base_url):
    assert get(f"{base_url}/api/shipped")[0] == 404
    assert get(f"{base_url}/api/anything-else")[0] == 404


def test_triage_ranks_every_submitted_ticket(base_url):
    status, body = post(
        f"{base_url}/api/triage",
        {
            "capacity": 1,
            "posture": "revenue_first",
            "autofill": True,
            "tickets": [
                {"message": "Checkout is completely down, we are losing thousands a minute."},
                {"message": "The button is the wrong shade of blue on mobile."},
            ],
        },
    )
    assert status == 200, body
    ordering = body["orderings"]["revenue_first"]
    ranked = ordering["ranked"]

    assert len(ranked) == 2
    assert [r["rank"] for r in ranked] == [1, 2]
    assert len({r["ticket_id"] for r in ranked}) == 2
    assert sum(r["served"] for r in ranked) == 1, "capacity is the line, not a suggestion"
    assert ranked[0]["score"] >= ranked[1]["score"]
    for r in ranked:
        assert 0.0 <= r["score"] <= 1.0, "weights sum to 1, so the score is a 0-1 number"


def test_every_posture_is_scored_not_just_the_requested_one(base_url):
    status, body = post(
        f"{base_url}/api/triage",
        {"capacity": 1, "posture": "balanced", "autofill": True,
         "tickets": [{"message": "Payments failing for about half of attempts."}]},
    )
    assert status == 200
    _, meta = get(f"{base_url}/api/meta")
    assert set(body["orderings"]) == set(meta["postures"])


def test_a_blank_submission_is_a_400_with_a_reason(base_url):
    status, body = post(f"{base_url}/api/triage", {"tickets": [], "autofill": False})
    assert status == 400
    assert body["error"]


# --------------------------------------------------- nothing is invented server-side


def test_an_unattributed_sender_has_no_tier_and_no_arr(base_url):
    """The keyword path reads text. It does not issue anyone a billing record.

    This is the regression that matters most on this endpoint: a heuristic that
    answered "growth tier, $50k ARR" for a sender nobody claimed put an invented
    number into the revenue axis, and the page rendered it beside real ones.
    """
    status, body = post(
        f"{base_url}/api/triage",
        {"capacity": 1, "posture": "revenue_first", "autofill": True,
         "tickets": [{"message": "Our integration webhook stopped firing this morning."}]},
    )
    assert status == 200, body
    facts = body["orderings"]["revenue_first"]["ranked"][0]["facts"]

    assert facts["attributed"] is False
    assert facts["arr"] == 0.0
    assert facts["tier"] == "free"
    assert facts["users_affected"] == 0
    assert facts["error_rate"] == 0.0
    assert facts["gmv_at_risk_per_hour"] == 0.0
    assert any("no billing record" in n for n in body["notes"])


def test_a_named_merchant_brings_its_real_record(base_url):
    """The other half of the same claim: real ids read the real CRM row."""
    _, meta = get(f"{base_url}/api/meta")
    merchant = max(meta["merchants"], key=lambda m: m["arr"])

    status, body = post(
        f"{base_url}/api/triage",
        {"capacity": 1, "posture": "revenue_first", "autofill": True,
         "tickets": [{"message": "Orders are failing at checkout.", "customer_id": merchant["id"]}]},
    )
    assert status == 200, body
    row = body["orderings"]["revenue_first"]["ranked"][0]
    assert row["facts"]["attributed"] is True
    assert row["facts"]["arr"] == merchant["arr"]
    assert row["facts"]["tier"] == merchant["tier"]


def test_the_response_carries_no_prewritten_verdicts(base_url):
    """Without a key there is no critique, no adjudication and no narrative.

    The response now embeds a real BatchReport, whose model always declares these
    fields -- `critiques: []`, `adjudication: None`, `narrative: ""` are how a
    Pydantic model represents "did not run", not a stand-in for one. The claim worth
    testing is that they are genuinely empty, and that degraded_stages says why."""
    status, body = post(
        f"{base_url}/api/triage",
        {"capacity": 1, "posture": "balanced", "autofill": True,
         "tickets": [{"message": "Checkout is down."}]},
    )
    assert status == 200, body
    assert body["critiques"] == []
    assert body["adjudication"] is None
    assert body["narrative"] == ""
    assert body["advice"] is None
    assert body["degraded_stages"], "keyless run must say which judgement stages did not run"


# ------------------------------------------------- zero is a value, not an absence


def test_capacity_zero_serves_nobody(base_url):
    """`0 == False` in Python, so `v in (None, "", False)` treated every zero as a
    missing field. Capacity 0 silently became the default 3 and the page reported
    three tickets served by nobody."""
    status, body = post(
        f"{base_url}/api/triage",
        {"capacity": 0, "posture": "balanced", "autofill": False,
         "tickets": [{"message": "a"}, {"message": "b"}]},
    )
    assert status == 200, body
    assert body["capacity"] == 0
    assert not any(r["served"] for r in body["orderings"]["balanced"]["ranked"])


def test_a_deadline_due_right_now_still_fires_the_charter(base_url):
    """The same bug, in the place it mattered most: a statutory deadline of 0 hours
    was read as no deadline at all, so charter rule R3 never fired for the most
    urgent value the field can hold.

    Ticket ids are batch-scoped (TKT-W{batch}-N), not a fixed TKT-U1/TKT-U2 -- a
    persisted submission advances the clock, so identify tickets by position in
    `resolved` rather than by a literal id. And because it persists, the ticket this
    test defers at capacity 1 would otherwise be carried into the next call within
    this same test -- reset between calls so each is independent.
    """
    def served_and_overrides(deadline):
        post(f"{base_url}/api/reset", {"confirm": True})
        status, body = post(
            f"{base_url}/api/triage",
            {"capacity": 1, "posture": "revenue_first", "autofill": False, "tickets": [
                {"message": "erasure request", "category": "compliance",
                 "stated_urgency": "low", "compliance_deadline_hours": deadline,
                 "tier": "free", "arr": 0.0, "waited_hours": 2.0},
                {"message": "enterprise outage", "category": "checkout_failure",
                 "stated_urgency": "critical", "tier": "enterprise", "arr": 5_000_000.0,
                 "users_affected": 9000, "error_rate": 0.9,
                 "gmv_at_risk_per_hour": 90_000.0, "waited_hours": 2.0},
            ]},
        )
        assert status == 200, body
        deadline_tid = body["resolved"][0]["ticket_id"]
        outage_tid = body["resolved"][1]["ticket_id"]
        o = body["orderings"]["revenue_first"]
        served = [r["ticket_id"] for r in o["ranked"] if r["served"]]
        overrides = [v["rule_id"] for v in o["overrides"]]
        return served, overrides, deadline_tid, outage_tid

    for deadline in (0, 0.5, 6):
        served, overrides, deadline_tid, _ = served_and_overrides(deadline)
        assert served == [deadline_tid], f"deadline {deadline} lost to ARR"
        assert "R3" in overrides, f"deadline {deadline} did not fire the charter"

    served, overrides, _, outage_tid = served_and_overrides(None)
    assert served == [outage_tid], "no deadline must not promote anything"
    assert overrides == []


def test_waiting_zero_hours_is_not_read_as_one(base_url):
    status, body = post(
        f"{base_url}/api/triage",
        {"capacity": 1, "posture": "fairness_first", "autofill": False,
         "tickets": [{"message": "just arrived", "waited_hours": 0}]},
    )
    assert status == 200, body
    assert body["orderings"]["fairness_first"]["ranked"][0]["facts"]["waiting_hours"] == 0.0


def test_non_finite_numbers_are_refused_at_the_boundary(base_url):
    """Python's json reads and writes bare NaN, which JSON forbids. One NaN reaching
    the scorer produces a NaN score that sorts unpredictably and renders as a number."""
    import urllib.request

    req = urllib.request.Request(
        f"{base_url}/api/triage",
        data=b'{"capacity":1,"posture":"balanced","autofill":false,'
             b'"tickets":[{"message":"n","error_rate":NaN}]}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(), exc.code

    assert status == 400, raw
    assert "NaN" not in json.loads(raw)["error"] or "not a number" in json.loads(raw)["error"]
    # and whatever we returned must itself be strictly-parseable JSON
    json.loads(raw, parse_constant=lambda c: pytest.fail(f"emitted bare {c}"))
