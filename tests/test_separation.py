"""The thesis, enforced as a test rather than asserted in a README.

    Estimation and valuation live in separate modules and never write to each other.

If history could rewrite values, the system would be laundering a judgment call as
data. These tests are what stops that happening by accident during a refactor at
11pm on a Tuesday.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import make_estimate

from triage import estimation, paths, valuation
from triage.config import load_posture
from triage.models import Tier, Urgency


SRC = Path(paths.repo_root()) / "src" / "triage"


def imported_modules(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import: `from .valuation import x`
                base = base or ""
                out.add(base)
                out.update(a.name for a in node.names)
            else:
                out.add(base)
    return {m.split(".")[-1] for m in out if m}


def test_estimation_does_not_import_valuation():
    """Facts must not be able to see the weights. A company with the opposite ethics
    would compute identical numbers from estimation.py."""
    imports = imported_modules(SRC / "estimation.py")
    assert "valuation" not in imports
    assert "Posture" not in imports


def test_valuation_does_not_import_estimation_or_state():
    """Values must not be able to see the history. This is the direction that matters
    most: it is what stops 'we deprioritise free-tier merchants because the model
    learned to' from ever being a true sentence about this system."""
    imports = imported_modules(SRC / "valuation.py")
    assert "estimation" not in imports
    assert "state" not in imports
    assert "State" not in imports


def code_identifiers(module_path: Path) -> set[str]:
    """Every name and attribute referenced in actual CODE.

    Deliberately AST-based rather than a substring search over the file: the first
    version of this test failed on estimation.py's own docstring explaining that it
    must not call load_postures. Prose about a rule is not a violation of it.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def test_estimation_does_not_read_the_posture_config():
    names = code_identifiers(SRC / "estimation.py") | imported_modules(SRC / "estimation.py")
    for forbidden in ("load_postures", "load_posture", "Posture", "weights", "valuation"):
        assert forbidden not in names, f"estimation.py references {forbidden}"


def test_valuation_does_not_reference_customer_identity_in_code():
    """Valuation consumes units. It must never branch on WHO filed something."""
    names = code_identifiers(SRC / "valuation.py")
    for forbidden in ("customer_id", "credibility", "times_skipped_by_customer", "State"):
        if forbidden == "times_skipped_by_customer":
            continue
        assert forbidden not in names, f"valuation.py references {forbidden}"


def test_only_the_scorer_imports_both_halves():
    """One crossing point, and it is the file whose whole job is being the crossing
    point. If a second module starts importing both, the separation has quietly
    stopped being real."""
    both = []
    for path in sorted(SRC.rglob("*.py")):
        imports = imported_modules(path)
        if "estimation" in imports and "valuation" in imports:
            both.append(path.name)
    assert both == ["scorer.py"], f"unexpected modules see both halves: {both}"


def test_the_llm_package_cannot_write_to_state():
    """An LLM may PROPOSE an observation as a constrained enum; it may not increment
    a counter. Deterministic code validates and does the arithmetic."""
    llm_dir = SRC / "llm"
    if not llm_dir.exists():
        pytest.skip("llm package not present")
    for path in sorted(llm_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("state.save", "record_observation", "accrue_skips", "State.load"):
            assert forbidden not in text, f"{path.name} touches state via {forbidden}"


# ----------------------------------------------- behavioural side of the same thing


def test_valuation_output_depends_only_on_declared_config():
    """Two estimates identical in every UNIT-BEARING field score identically, no
    matter which account they came from. Valuation cannot see the customer id."""
    posture = load_posture("revenue_first")
    a = make_estimate(ticket_id="TKT-A", customer_id="northpeak", arr=50000.0, tier=Tier.growth)
    b = make_estimate(ticket_id="TKT-B", customer_id="soloartisan", arr=50000.0, tier=Tier.growth)
    assert valuation.components(a, 0.5, posture) == valuation.components(b, 0.5, posture)


def test_changing_the_posture_cannot_change_a_single_estimate(batch1_ctx):
    """The other direction: swapping the company's ethics leaves every fact intact."""
    from triage.pipeline import Context

    def estimates_for(_posture_name: str):
        working = Context(batch1_ctx.batch, batch1_ctx.company, batch1_ctx.sources,
                          batch1_ctx.state.clone())
        return {e.ticket_id: e.model_dump(mode="json") for e in working.estimates()}

    assert estimates_for("revenue_first") == estimates_for("fairness_first")


def test_credibility_cannot_be_moved_by_any_declared_value():
    """Credibility is computed on read from counts. There is no code path from a
    posture to a credibility score, and this pins that there never will be."""
    from triage.models import CustomerHistory

    h = CustomerHistory(urgency_claims=9, confirmed_severe=2)
    before = estimation.credibility(h)
    for name in ("revenue_first", "fairness_first", "crisis_mode"):
        load_posture(name)
        assert estimation.credibility(h) == before


def test_the_one_declared_reach_into_an_estimate_is_the_charter_floor_and_it_is_visible():
    """The single deliberate exception. It is applied in the scorer, reported in the
    severity breakdown, and named in the report -- not smuggled into estimation.py as
    a magic constant."""
    from triage import scorer

    exposure = make_estimate(security_exposure=True, blast_radius=0.02, stated_urgency=Urgency.low)
    bd = scorer.severity(exposure)
    assert bd.charter_floor == 1.0
    assert bd.charter_floor_reason  # the report can always say WHY
    assert "trust boundary" in bd.charter_floor_reason

    ordinary = make_estimate(blast_radius=0.02)
    assert scorer.severity(ordinary).charter_floor == 0.0
    assert scorer.severity(ordinary).charter_floor_reason is None
