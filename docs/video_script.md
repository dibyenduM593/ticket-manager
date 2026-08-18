# Video script — 3:00 hard cap

They stop watching at 3:00. Every beat below is timed, and the last one is the
most important.

**Before recording**

```bash
python scripts/run_demo.py --runs 10
```

That reseeds state, regenerates every report, and leaves the terminal clean. Then
open a fresh terminal so the scrollback starts empty.

Everything here runs without an API key. If cassettes have been recorded, drop
`--no-llm` and add `--replay` throughout — same commands, fuller output, no network
risk on camera.

---

## 0:00–0:20 · The problem, in one ticket

Show `TKT-4482` in [`data/batches/batch_1.json`](../data/batches/batch_1.json).

> "Free tier. Zero revenue. The merchant says — and I quote — *probably just a display
> glitch on my end, no rush at all*. Telemetry says confirmed cross-tenant exposure of
> partial card data across 340 merchants.
>
> Every signal the brief names points a different direction on this one ticket. No
> weighting of those five fields settles it, because the disagreement isn't about
> arithmetic. It's about what the company thinks is worth protecting."

---

## 0:20–0:50 · Values are declared, not learned

```bash
python -m triage plan --no-llm
```

> "The business declares its situation in a YAML file: 91% system load, active
> incident, Series B in six weeks, largest account at renewal. The agent ranks all six
> value postures *for that situation*, recommends one, and names what the
> recommendation costs.
>
> Then it stops and asks a human. That's deliberate. Estimation is factual and learned
> from history. Valuation is declared and never learned. They live in separate modules
> that cannot write to each other, and a test enforces it. If history could rewrite
> values, the system would be laundering a judgment call as data — which is the exact
> failure mode this task is probing for."

---

## 0:50–1:40 · The run, and the override

```bash
python -m triage run --batch data/batches/batch_1.json --posture revenue_first --no-llm -y
```

Let the ranking table land. Then point at the ⚖ on rank 3.

> "Under `revenue_first`, that data-exposure ticket scores **fifth of six**. Capacity
> is three agents, so fifth means deferred — nobody looks at it today.
>
> The charter overrides it to rank 3. Note *3*, not 1: promotion is minimal, to the
> lowest rank that satisfies the rule. The charter is a floor, not a preference — it
> doesn't get an opinion about what should be first, only about what can't be last.
>
> And the report says which rule fired, what it cost, and who got displaced."

Scroll to the charter-override block in the output.

> "The instruction was 'maximise revenue'. The system applied it to five tickets and
> refused on one, and told you exactly where and why."

---

## 1:40–2:15 · The same batch, six different ethics

```bash
python -m triage compare --batch data/batches/batch_1.json
```

> "Same evidence, same four sources, six value postures. Four distinct orderings.
> The postures aren't decorative — and the tool tells you when they *are*, because if
> all six agreed it would say so.
>
> Look at rank 1. `crisis_mode` leads with Bloom & Vine; `revenue_first` leads with
> NorthPeak. NorthPeak has thirteen times the ARR. Bloom & Vine is mid-flash-sale and
> losing more money per hour. That's not revenue versus fairness — that's **revenue
> disagreeing with itself**, and which one you mean is a values question wearing a
> metrics costume."

---

## 2:15–2:40 · Fairness is a property of a series

```bash
python -m triage run --all --no-llm
```

Let it run all three batches, then stop on the audit.

> "Three batches in sequence. Verdant Home gets skipped, and the ledger remembers —
> fairness debt only accrues for tickets past the capacity line, so 'skipped six
> times' is a true statement rather than a figure of speech. By batch 43 the charter's
> waiting ceiling fires and promotes them.
>
> And then the audit tells me I failed. It says **BREACHED**: the worst wait was 5.3
> days against a 5-day ceiling. The rule caught it; it didn't prevent it, because
> promotion only happens at a batch boundary the ticket had already crossed.
>
> I left that in. A system that only reports the ways it succeeded isn't an audit."

---

## 2:40–3:00 · What's unfinished

> "Honestly: the cassettes aren't recorded, so every number in that README is from the
> deterministic fallback path — including conflict recall, which is 9 of 21 planted
> contradictions. The fallback finds the arithmetic ones and misses the ones that need
> two sources held in mind at once. Closing that gap is the entire argument for the LLM
> stage, and right now it's a hypothesis with a number attached to its null case rather
> than a result.
>
> The stability figure is 10 out of 10 and means almost nothing — the deterministic
> core is stable by construction. It's a control, not a result, and the harness says so
> before it prints the number.
>
> No counterfactual is ever observed. If we deprioritise a merchant and they churn, we
> can never know whether faster service would have saved them. So this system can
> validate its estimates. It can never validate its values — and that's not a gap I can
> close with more code."

---

## If a beat has to go

Cut in this order:

1. The `plan` beat (0:20–0:50) — compress to one sentence over the `run` output.
2. The `compare` beat — keep the ARR-vs-GMV line, drop the table.

Never cut: the charter override, the audit breach, or the closing honesty beat.
Those three are the submission.
