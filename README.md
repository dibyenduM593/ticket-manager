# Conflicting-signal ticket triage agent

A support-ticket triage system for a hosted e-commerce platform, built around one
structural commitment:

> **Estimation and valuation live in separate modules and never write to each other.**
>
> *Estimation* — how credible is this claim, how many users are affected, how long
> will this really take, how long has this waited. Factual. Learned from history.
> Empirically testable.
>
> *Valuation* — is a $480k account worth more than 2,000 free users. Declared, never
> learned, never testable.

History writes only to estimation. The business declares only valuation. They meet at
exactly one line of code, in `scorer.severity()`. If history could rewrite values, the
system would be laundering a judgment call as data.

The separation is enforced by [`tests/test_separation.py`](tests/test_separation.py),
not just asserted here.

---

## Quickstart

No API key needed — the deterministic core is the whole system's floor, and it runs
alone.

```bash
pip install -e ".[dev]"
```

```bash
python -m triage run --batch data/batches/batch_1.json --posture revenue_first --no-llm
```

Score one batch under all six value postures, side by side:

```bash
python -m triage compare --batch data/batches/batch_1.json
```

Interrogate any past decision, including its counterfactuals:

```bash
python -m triage why TKT-4471
```

Regenerate the synthetic history and derived state:

```bash
python scripts/seed_history.py
```

With an API key in `.env` (see `.env.example`), drop `--no-llm` for the full pipeline:
conflict detection, posture advice, four adversarial critics, and adjudication.

---

## What it does

Six merchants, three batches, during a flash-sale traffic peak. Four independent
context sources that are allowed to contradict each other:

| Source | What it knows | How it contradicts |
|---|---|---|
| Ticket message | what the merchant *claims* | subjective, inflatable |
| Telemetry | error rates, users affected, GMV at risk | says "nothing wrong" when the merchant says outage |
| CRM / billing | tier, ARR, renewal date | says "tiny account" when impact is huge |
| Resolution history | track record, real fix times | says "they always say this" |

The demo turns on a few planted conflicts:

- **`TKT-4482`** — free tier, $0 ARR, merchant says *"probably a display glitch, no
  rush."* Telemetry says confirmed cross-tenant exposure of partial card data across
  340 merchants. Under `revenue_first` it scores 5th of 6 and would be deferred; the
  charter force-promotes it into the served set and the report shows exactly what that
  cost.
- **`TKT-4477` vs `TKT-4491`** — the same enterprise account, the same "CRITICAL"
  claim, 0.27 credibility both times. In batch 42 telemetry corroborates and it ranks
  first. In batch 43 telemetry does not and it is deferred. Credibility is a
  tiebreaker for *unsupported claims only*.
- **ARR vs GMV** — NorthPeak has 13× the ARR; Bloom & Vine, mid-flash-sale, has more
  money bleeding per hour. `revenue_first` leads with one, `crisis_mode` with the
  other. Two revenue signals in genuine disagreement.
- **`TKT-4490`** — a prompt-injection attempt in a ticket body. It lands exactly where
  an identical clean ticket would, and the attempt is surfaced in the report.

### The scorer

```python
claimed  = stated_urgency * clamp(credibility, 0.3, 1.0)
observed = max(blast_radius, category_severe_rate, charter.severity_floor(ticket))
severity = max(claimed, observed)      # max, NOT average
```

`max` guarantees `severity >= observed`: a measurement is never diluted by the
reputation of whoever reported it. Under averaging, a blast radius measured at 0.94 is
scored 0.62 because of who filed the ticket. Both the property and its limits are
pinned in [`tests/test_scorer.py`](tests/test_scorer.py).

### The charter

Four non-negotiable rules no posture can tune, in
[`config/charter.yaml`](config/charter.yaml). Promotion is **minimal** — a protected
ticket moves to the lowest rank that satisfies the rule, never to the top. The charter
is a floor, not a preference.

---

## Status

Built so far — the deterministic core and the LLM layer:

- Estimation / valuation separation, scorer, charter enforcement, capacity-aware
  fairness debt, correlation clustering
- Seeded history (260 resolved tickets, fixed seed) deriving `state/` by counting
- Three batches with 21 planted conflicts, telemetry and CRM
- LLM client with forced-schema tool use, temperature 0, prompt caching, cassette
  record/replay, and a deterministic fallback for every stage
- Conflict detection, posture advisor, four adversarial critics, adjudication
- Report renderer (markdown + JSON), counterfactual regret table, posture audit,
  append-only decision log
- 111 tests: deterministic core, LLM contract (including hallucinated-entity and
  hostile-response cases), and behavioural guarantees

Not done yet:

- **Cassettes are not recorded.** `--replay` works but has nothing to serve; the
  contract tests currently run against a programmable test double instead. Recording
  needs an API key.
- Planted-conflict recall eval, and the 10× stability measurement.
- Committed example reports — the ones generated so far are from degraded (`--no-llm`)
  runs and would misrepresent the full pipeline.

### Known limitations

- No counterfactual is ever observed. If we deprioritise and the merchant churns, we
  cannot know faster service would have saved them. The system can validate its
  *estimates* but never its *values*.
- History is synthetic, by design: real data cannot set up a demonstration of a
  credibility discount, because it does not know what you are about to demonstrate.
- Batch, not stream. A ticket arriving mid-batch that engages a charter rule should
  preempt immediately; anything else waits for the next boundary. Described, not built.
- Capacity is treated as fungible — a billing specialist cannot actually take a
  checkout bug.
- Per-ticket contractual SLA deadlines are carried as a field but not enforced. They
  are distinct from fairness: a contractual clock is not a moral one.

---

## Licence

MIT — see [LICENSE](LICENSE).
