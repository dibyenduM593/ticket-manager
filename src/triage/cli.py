"""Command line interface.

    triage plan                          posture advice only, no ranking
    triage run --batch data/batches/batch_1.json
    triage run --batch ... --posture fairness_first     human override
    triage compare --batch ...           all six postures side by side
    triage why TKT-4471                  interrogate a past decision
    triage audit                         what the posture actually did, across batches
    triage run --replay                  zero setup, no API key
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import audit as audit_mod
from . import paths, report as report_mod
from .config import load_postures
from .models import PostureAdvice, Posture
from .pipeline import (
    Context,
    RunOptions,
    advise_posture_deterministic,
    order_under_all_postures,
    run_batch,
)

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()


def _default_batch() -> Path:
    return paths.batches_dir() / "batch_1.json"


# --------------------------------------------------------------------- plan


@app.command()
def plan(
    state: Path = typer.Option(None, help="company_state.yaml to reason about"),
    batch: Path = typer.Option(None, help="batch to cost the recommendation against"),
    replay: bool = typer.Option(False, "--replay", help="serve LLM calls from cassettes"),
    no_llm: bool = typer.Option(False, "--no-llm", help="deterministic decision tree only"),
) -> None:
    """Read the company state, rank all postures for it, recommend one, name the cost."""
    from .llm.client import LLMClient
    from .llm import stages as llm_stages

    ctx = Context.load(batch or _default_batch(), company_path=state)
    estimates = ctx.estimates()

    client = LLMClient(cassette_mode="replay" if replay else None)
    if no_llm:
        client.api_key = None
        client.cassette_mode = "off"

    result = llm_stages.advise_posture(
        client, ctx.summary_line(), load_postures(), ctx.charter,
        ctx.batch.tickets, estimates, advise_posture_deterministic(ctx),
    )
    if result.degraded and result.reason:
        console.print(f"[yellow]degraded:[/yellow] {result.reason}\n")
    _print_advice(result.value, ctx.summary_line())


def _print_advice(advice: PostureAdvice, situation: str) -> None:
    console.print(f"[dim]{situation}[/dim]\n")
    console.print(f"[bold green]Recommended posture: {advice.recommended}[/bold green]\n")
    console.print(advice.reasoning + "\n")
    console.print(f"[bold]What it costs you:[/bold] {advice.what_it_costs}\n")
    if advice.charter_collision_warning:
        console.print(f"[bold yellow]⚠ {advice.charter_collision_warning}[/bold yellow]\n")
    if advice.ranked_alternatives:
        table = Table("rank", "posture", "reasoning", "the trade it makes", box=None, pad_edge=False)
        for alt in sorted(advice.ranked_alternatives, key=lambda a: a.rank):
            table.add_row(str(alt.rank), alt.posture, alt.reasoning, alt.trade_off)
        console.print(table)


# ---------------------------------------------------------------------- run


@app.command()
def run(
    batch: Path = typer.Option(None, help="batch json to triage"),
    posture: str = typer.Option(None, help="override the recommendation"),
    state: Path = typer.Option(None, help="company_state.yaml override"),
    replay: bool = typer.Option(False, "--replay", help="serve LLM calls from cassettes; no API key needed"),
    no_llm: bool = typer.Option(False, "--no-llm", help="deterministic core only"),
    assume_yes: bool = typer.Option(False, "--assume-yes", "-y", help="skip the human confirmation prompt"),
    dry_run: bool = typer.Option(False, "--dry-run", help="do not write state/ or reports/"),
) -> None:
    """Full pipeline: contradictions, posture, ranking, charter, critique, report."""
    ctx = Context.load(batch or _default_batch(), company_path=state)

    options = RunOptions(
        posture=posture,
        use_llm=not no_llm,
        replay=replay,
        assume_yes=assume_yes,
        persist_state=not dry_run,
        confirm=None if assume_yes else _confirm_posture,
    )
    result = run_batch(ctx, options)

    if not dry_run:
        md_path, json_path = report_mod.write(result)
        console.print(f"[dim]wrote {md_path} and {json_path}[/dim]\n")

    _print_report(result)


def _confirm_posture(advice: PostureAdvice, postures: dict[str, Posture]) -> str:
    """The human confirmation step.

    The agent advises on values; it does not set them. Letting a model autonomously
    choose a company's ethics is exactly the thing not to build.
    """
    _print_advice(advice, "")
    console.print()
    choice = typer.prompt(
        f"Confirm posture [{advice.recommended}] or type another "
        f"({', '.join(sorted(postures))})",
        default=advice.recommended,
    ).strip()
    return choice if choice in postures else advice.recommended


def _print_report(result) -> None:
    console.print(f"[bold]Batch {result.batch_id}[/bold] — {result.label}")
    console.print(f"[dim]{result.company_state_summary}[/dim]")
    console.print(f"posture [bold]{result.posture}[/bold] ({result.posture_chosen_by})\n")

    if result.degraded_stages:
        console.print("[yellow]⚠ degraded run — some stages did not execute:[/yellow]")
        for reason in result.degraded_stages:
            console.print(f"  [yellow]- {reason}[/yellow]")
        console.print()

    table = Table("#", "ticket", "merchant", "score", "outcome", "why")
    for r in result.ordering.ranked:
        outcome = "[green]served[/green]" if r.served else "[dim]deferred[/dim]"
        if r.charter_promoted:
            outcome += " [bold yellow]⚖[/bold yellow]"
        table.add_row(str(r.rank), r.ticket_id, r.customer_id, f"{r.score:.3f}", outcome, r.justification)
    console.print(table)

    for o in result.ordering.overrides:
        console.print(
            f"\n[bold yellow]CHARTER OVERRIDE[/bold yellow] {o.rule_id}/{o.clause_id} "
            f"[bold]{o.ticket_id}[/bold] rank {o.from_rank} → {o.to_rank}"
        )
        console.print(f"  {o.statement}")
        console.print(f"  [dim]{o.detail}[/dim]")

    if result.conflicts:
        console.print(f"\n[bold]{len(result.conflicts)} contradictions found.[/bold] "
                      "Full table in the markdown report.")
    if result.adjudication and result.adjudication.changed_mind:
        console.print("\n[bold magenta]The ranking changed after critique:[/bold magenta]")
        for rev in result.adjudication.revisions:
            console.print(f"  {rev.ticket_id}: {rev.from_rank} → {rev.to_rank} "
                          f"(conceding to {rev.because_of_critic})")
    if result.escalations:
        console.print(f"\n[bold]{len(result.escalations)} escalations[/bold] — "
                      "tickets the system declines to decide on.")


# ------------------------------------------------------------------ compare


@app.command()
def compare(
    batch: Path = typer.Option(None, help="batch json to score under every posture"),
    state: Path = typer.Option(None, help="company_state.yaml override"),
) -> None:
    """Score one batch under all six postures, side by side. Deterministic, no API."""
    ctx = Context.load(batch or _default_batch(), company_path=state)
    orderings = order_under_all_postures(ctx)

    names = sorted(orderings)
    table = Table("rank", *names)
    depth = max(len(o.ranked) for o in orderings.values())
    for i in range(depth):
        cells = []
        for name in names:
            ranked = orderings[name].ranked
            if i >= len(ranked):
                cells.append("")
                continue
            r = ranked[i]
            label = f"{r.ticket_id}"
            if r.charter_promoted:
                label = f"[bold yellow]{label} ⚖[/bold yellow]"
            elif r.served:
                label = f"[green]{label}[/green]"
            else:
                label = f"[dim]{label}[/dim]"
            cells.append(label)
        table.add_row(str(i + 1), *cells)
    console.print(table)
    console.print("\n[green]green[/green] = served, [dim]dim[/dim] = deferred, "
                  "[bold yellow]⚖[/bold yellow] = charter override")

    distinct = {tuple(o.ticket_order) for o in orderings.values()}
    console.print(f"\n{len(distinct)} distinct orderings from {len(names)} postures.")
    if len(distinct) == 1:
        console.print("[yellow]All postures agree. Either the evidence is decisive here, "
                      "or the postures are decorative — worth knowing which.[/yellow]")


# ---------------------------------------------------------------------- why


@app.command()
def why(ticket_id: str) -> None:
    """Interrogate a past decision from the append-only decision log."""
    decisions = [d for d in audit_mod.load_decisions() if d["ticket_id"] == ticket_id]
    if not decisions:
        console.print(f"No decision recorded for {ticket_id}. Run `triage run` first.")
        raise typer.Exit(1)

    for d in decisions:
        est = d["estimate"]
        sev = d["severity"]
        console.print(f"\n[bold]{ticket_id}[/bold] — batch {d['batch_id']}, posture {d['posture']}")
        console.print(f"  rank {d['rank']} ({'served' if d['served'] else 'deferred'}), "
                      f"score {d['score']:.3f}"
                      + ("  [bold yellow]⚖ charter[/bold yellow]" if d["charter_promoted"] else ""))
        console.print(f"\n  [bold]severity {sev['severity']:.2f}[/bold] — driven by the {sev['driver']}")
        console.print(f"    claimed  {sev['claimed']:.2f}  = stated '{est['stated_urgency']}' "
                      f"x credibility {est['credibility']:.2f}")
        console.print(f"    observed {sev['observed']:.2f}  = max(blast {est['blast_radius']:.2f}, "
                      f"category severe rate {est['category_severe_rate']:.2f}, "
                      f"charter floor {sev['charter_floor']:.2f})")
        if sev.get("charter_floor_reason"):
            console.print(f"      charter floor applies: {sev['charter_floor_reason']}")

        console.print("\n  [bold]value components[/bold]")
        for axis, value in sorted(d["components"].items()):
            console.print(f"    {axis:<12} {value:.3f}")

        console.print("\n  [bold]facts[/bold]")
        console.print(f"    {est['tier']} tier, ${est['arr']:,.0f} ARR, "
                      f"${est['gmv_at_risk_per_hour']:,.0f}/h at risk")
        console.print(f"    {est['users_affected']} users affected, "
                      f"credibility {est['credibility']:.2f} ({est['credibility_evidence']})")
        console.print(f"    waited {est['waiting_hours']:.0f}h, skipped {est['times_skipped']}x")

        if d["counterfactuals"]:
            console.print("\n  [bold]under other postures[/bold]")
            for cf in d["counterfactuals"]:
                console.print(f"    {cf['posture']:<18} rank {cf['rank_here']} — {cf['cost_sentence']}")


# -------------------------------------------------------------------- audit


@app.command()
def audit(
    posture: str = typer.Option(None, help="restrict to one posture"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """What the chosen posture actually did, across every batch run so far."""
    result = audit_mod.audit(audit_mod.load_decisions(), posture)
    if as_json:
        console.print_json(json.dumps(result))
    else:
        console.print(audit_mod.render(result))


if __name__ == "__main__":
    app()
