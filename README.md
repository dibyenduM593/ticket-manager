# Conflicting-signal ticket triage agent

[![ci](https://github.com/dibyenduM593/ticket-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/dibyenduM593/ticket-manager/actions/workflows/ci.yml)

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

Run all three batches in sequence — fairness debt carries between them, which is the
only way it can be observed at all — then audit what the postures actually did:

```bash
python -m triage run --all --no-llm
```

Interrogate any past decision, including its counterfactuals:

```bash
python -m triage why TKT-4471
```

Regenerate every committed artefact from a clean seed, in one command:

```bash
python scripts/run_demo.py
```

With an API key in `.env` (see `.env.example`), drop `--no-llm` for the full pipeline:
conflict detection, posture advice, four adversarial critics, and adjudication.

### The demo front end

```bash
python web/server.py
```

Then open <http://localhost:8000>. Write up to five tickets in plain English — the
page reads them into structured fields for you — and watch the ranking, the charter
overrides, and all six postures side by side.

Each ticket has a **sender** dropdown. Leave it unset and the merchant is invented at
the 0.5 credibility prior. Name one of the six real merchants and their actual billing
record and claim history apply, which is the fastest way to see the credibility term
work: send *identical text* from NorthPeak (2 of 9 urgency claims ever confirmed) and
from soloartisan (2 of 2) and watch the two diverge.

**What reads the tickets, and what does not.** Turning free text into four sources is
a reading step, done by the model when a key is present and by keyword heuristics when
it is not. It never touches the ranking: scoring, the charter and the ordering stay
deterministic. And the page splits every ticket into what was *read* and what was
*invented*, because those are not the same kind of claim. Category and stated urgency
are genuinely read from the words — that is source A. Users affected, error rate,
exposure and waiting time are **simulated**, because no instruments are attached to a
ticket you just typed; a real run reads those from `telemetry.json` and the ledger,
which no model writes to. Tier and ARR are simulated only while the sender is unknown —
name a real merchant and the actual CRM record is used instead, and the page marks it
`real` rather than leaving you to guess. That separation is load-bearing
rather than pedantic: the demo's centrepiece is a merchant saying *"no rush"* while
telemetry reports a confirmed exposure, and deriving the instruments from the ticket
text would make that contradiction impossible to express.

Two deliberate constraints. It is **stdlib only** — no Flask, no FastAPI, nothing to
install — because a front end that needs `pip install` would undercut the zero-setup
claim on the first line of this README. And it **imports the real `src/triage/`
modules** rather than porting the scorer to JavaScript: a second implementation is
free to silently disagree with the first, and then the page is demonstrating a system
that does not exist. Every number on screen came out of the same code `triage run`
calls, so the page cannot show a ranking the CLI would not produce.

The triage pipeline's own LLM stages — conflict detection, posture advice, the four
critics, adjudication — never run there. Reading the tickets is the only place a model
acts, and the page carries a banner saying which path produced what you are looking at
rather than letting you assume.

### Every command

| Command | What it does | Needs a key |
|---|---|---|
| `triage plan` | reads company state, ranks all six postures for it, names the cost | no (`--no-llm`) |
| `triage run --batch ...` | the full pipeline on one batch | no (`--no-llm`) |
| `triage run --all` | every batch in sequence, then the audit | no (`--no-llm`) |
| `triage compare --batch ...` | six postures side by side | never |
| `triage why TKT-4471` | one decision, its inputs and its counterfactuals | never |
| `triage audit` | what the posture did across every batch run so far | never |
| `triage eval` | conflict recall against the planted labels | no (`--no-llm`) |
| `triage stability --runs 10` | what moves when the same batch is rerun | no (`--no-llm`) |
| `scripts/record_cassettes.py` | records cassettes so `--replay` works | **yes** |

---

## Where each requirement is satisfied

So a reviewer does not have to hunt.

| Asked for | Where it lives | Notes |
|---|---|---|
| ≥5 tickets with conflicting signals | [`data/batches/`](data/batches/) | 6 tickets per batch, 3 batches, 21 hand-authored contradictions |
| ≥2 independent, contradicting sources | `ticket` · `telemetry` · `crm` · `resolution history` | 4 sources; the table below names how each one disagrees |
| ≥3 prioritisation strategies, ranked, with reasoning | [`config/postures/`](config/postures/) → `triage plan` | 6 postures; §5 of every report ranks all of them with the trade each makes |
| Defends its choice against the alternatives | §5 and §6 of every batch report | §6 is counterfactual regret: the specific tickets that move, and what that costs in dollars or waiting |
| Structured report per batch | [`reports/batch_42.md`](reports/batch_42.md) | 8 sections; conflicts found → ranking → overrides → strategy ranking → regret → critique → escalations |
| Noticed the conflict | §2 of every report | plus `triage eval`, which measures detection against the planted labels rather than asserting it |

The five severity indicators the brief names, and where each enters the decision:

| Indicator | Enters as |
|---|---|
| Customer account value (free → enterprise) | `tier` and `arr` from CRM, blended in `valuation.account_value()` |
| Stated urgency in the message | `stated_urgency` × credibility → the `claimed` half of severity |
| Historical resolution time for similar issues | `category_median_hours` from resolution history → the `speed` axis |
| **Current system load** | **posture selection, not per-ticket scoring** — see below |
| Whether it blocks a paying workflow | `blocks_paying_workflow`, claimed *and* observed, kept as separate fields so they can disagree |

System load is deliberately not a per-ticket field. Load is a property of the
platform, not of any ticket, so it cannot make one ticket more severe than another —
what it should change is *which strategy you are running*. At ≥85% load with an open
incident the agent recommends `crisis_mode` and says so in §5; it also sets capacity,
which decides how many tickets get served at all. Making it a per-ticket term would
have been easier to point at and wrong.

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

## What the measurements say

Everything in this section was produced by `python scripts/run_demo.py` and is
committed in [`reports/`](reports/). **Every number below is from the deterministic
path**, because this repo has no API key attached to it. Each report carries a banner
naming the seven stages that did not execute, and the eval and stability output both
refuse to print a number without saying which detector produced it.

### Conflict detection: 9 / 21

The only place in this project the word *recall* is honest. We cannot know the right
ranking — the brief's premise is that no such thing exists — but we do know which
contradictions we authored, because we wrote all 21 of them into `planted_conflicts`
in the batch files. Nothing in `src/triage/llm/` reads that field, and
[a test asserts it](tests/test_eval.py).

```
recall            9/21  (43%)   planted conflicts found
  of which exact  6/21  (29%)   same ticket AND same source pair
unmatched         2 detections across 11 total
```

The rule-based fallback finds the contradictions that are arithmetic — a stated
urgency far above the measured one, a declared blocking flag telemetry does not
support, a confirmed exposure on a $0 account. It misses every contradiction that
requires holding two sources in mind at once:

- **`B1-C4`** — ARR says NorthPeak ($480k) outranks Bloom & Vine ($36k); GMV-at-risk
  says the reverse, $11.2k/h against $8.4k/h. Two revenue signals disagreeing.
- **`B1-C7`** — two tickets sharing error signature `PAYMT-503-EU` are one platform
  incident reported twice, not two independent tickets.
- **`B2-C4`** — the prompt-injection attempt. The scorer is immune to it either way,
  but the fallback detector does not *notice* it.
- **`B3-C3`** — the most credible merchant on the platform stating low urgency about
  something genuinely low. High credibility must not become automatic promotion.

That gap is the argument for the LLM stage, stated as a number rather than as a
paragraph. Closing it is what the recorded-cassette run is for.

Unmatched detections are reported as candidates, not as false positives: 21 is a lower
bound on the conflicts present, not a census. Calling an unlabelled detection wrong
would assume we thought of everything.

### Stability: 10 / 10, and it does not mean much yet

```
identical orderings   10/10   (1 distinct ordering)
stable prefix         ranks 1-6 identical in every run
```

The deterministic core is stable by construction, so this is a control, not a result:
it confirms the harness measures the pipeline and not the ledger (ten runs that each
accrued fairness debt would drift). The number worth reporting is the same command
with cassettes recorded, and this repo cannot produce it yet.

The harness already reports the things a bare agreement percentage would hide: which
ranks moved, whether the *served set* changed (a swap inside the served three is
cosmetic; a swap across the capacity line is not), and whether the posture,
conflict count, and adjudicator's mind-change varied while the ordering held still.

### The audit found a real breach

Across the three-batch sequence, under the postures the advisor picked for each
situation:

| tier | median wait | worst | served | deferred |
|---|---|---|---|---|
| enterprise | 6.4h | 14.3h | 4 | 2 |
| free | 5.9h | 7.2h | 1 | 1 |
| growth | 41.8h | 128.0h | 3 | 3 |
| starter | 5.9h | 13.1h | 0 | 3 |

> **BREACHED.** Worst observed wait is 5.3 days against a charter ceiling of 5 days.
> The charter promotes on the batch *after* a ticket crosses the line, because
> promotion happens at batch boundaries and the ticket crossed between them. The rule
> caught it; it did not prevent it.

This is the system reporting its own failure, and it is a genuine one: `TKT-4471`
was force-promoted in batch 43 at 128 hours, eight hours past a ceiling that says
120. A ceiling enforced only at batch boundaries is a ceiling you land on rather than
one you stop below. Fixing it means promoting on *projected* wait at the next
boundary, or processing arrivals as a stream — neither is built, and the audit says
so rather than rounding the finding down to "approaching".

`starter` tier at zero served over three batches is the other finding worth sitting
with. It is what `revenue_first` is *for*, which is exactly why the audit states it.

---

## Why the data is synthetic, and which real datasets were rejected

The obvious criticism of this submission is that I made the data up. I did, deliberately,
and the reasoning is the same reasoning the whole system rests on.

**A labelled dataset ships a ground truth. The brief's premise is that no ground truth
exists.** Every public ticket dataset with a `priority` column is one organisation's
past judgment recorded as fact. Evaluating against it does not answer "how should these
be ranked" -- it answers "how did *they* rank these", and then quietly relabels that as
correctness. Importing the dataset imports the assumption the task asks you to reject.

Four were reviewed properly before I wrote a line of the seeder:

| Dataset | What it has | Why it was rejected |
|---|---|---|
| [Eclipse & Mozilla Defect Tracking](https://github.com/ansymo/msr2013-bug_dataset) (Lamkanfi et al., MSR 2013) | ~200k real bugs, reporter-assigned severity, the full triage lifecycle, real resolution times | The strongest candidate, and it fails on the thing that matters: **one source**. No telemetry, no account value, no reporter track record -- so nothing can contradict anything. Worse, its `severity` field is what the *reporter typed*, which is precisely the unreliable claim this system exists to discount. Treating it as ground truth assumes the thing the system is built to question. |
| [Customer Support Ticket Dataset](https://www.kaggle.com/datasets/suraj520/customer-support-ticket-dataset) | 8.5k tickets with a `Ticket Priority` label (low / medium / high / critical) | The label *is* the answer, handed over. There is no independent signal to set against it, so a model scoring 90% here has learned one support team's habits, not triage. |
| [Customer IT Support -- multilingual tickets](https://www.kaggle.com/datasets/tobiasbueck/multilingual-customer-support-tickets) | Labelled email tickets with priorities and queues | Same problem, and it is itself synthetic -- so it carries every drawback of generated data with none of the control, because its conflicts were not authored to demonstrate anything. |
| [Customer Support on Twitter](https://www.kaggle.com/datasets/thoughtvector/customer-support-on-twitter) | 3M real support conversations | Genuinely real language, which is the appeal. But it is a conversation corpus, not a triage corpus: no priority, no severity, no account value, no telemetry. Nothing to contradict. |

The failure is the same in all four: **one source per ticket.** This task is about what
to do when independent sources disagree, and you cannot stage a disagreement with one
witness.

There is a second reason, narrower but decisive for the demo. The history's job is to
*set up* the argument -- NorthPeak has to already have a bad track record before the
live batch lands, so the audience watches the credibility discount get applied and knows
where the number came from. Real data cannot do that, because real data does not know
what you are about to demonstrate.

**What the synthetic data is not allowed to do.** It is designed profiles, generated
instances: [`scripts/seed_history.py`](scripts/seed_history.py) fixes six merchants'
claim/confirmation rates and a per-category shape, then generates 260 individual
resolved tickets consistent with them. `state/customers.json` is then **derived by
counting those tickets**, never written by hand, so the credibility numbers are true of
the history sitting next to them. `was_actually_severe()` is an operational definition
you can disagree with, and moving it changes every number downstream. CI reseeds from
the fixed seed and fails on any diff against the committed state, so "reproducible" is
checked rather than claimed.

**Where a real dataset would earn its place.** Eclipse/Mozilla resolution *times* are
observed facts rather than judgments, so they could validate the **estimation** layer
alone: does a category's predicted median duration correlate with actual fix time? That
is a falsifiable claim about the world. The valuation layer stays out of it, permanently
-- no dataset can tell you whether a $480k account outranks 2,000 free users.

---

## Status

Built:

- Estimation / valuation separation, scorer, charter enforcement, capacity-aware
  fairness debt, correlation clustering
- Seeded history (260 resolved tickets, fixed seed) deriving `state/` by counting
- Three batches with 21 planted conflicts, telemetry and CRM
- LLM client with forced-schema tool use, temperature 0, prompt caching, cassette
  record/replay, and a deterministic fallback for every stage
- Conflict detection, posture advisor, four adversarial critics, adjudication
- Report renderer (markdown + JSON), counterfactual regret table, posture audit,
  append-only decision log
- Multi-batch sequential run, planted-conflict eval, stability harness, and a
  one-command regeneration of every committed artefact
- 147 tests: deterministic core, LLM contract (including hallucinated-entity and
  hostile-response cases), behavioural guarantees, and the eval and audit themselves
- CI on 3.11 and 3.13 with **no API key configured**, so any accidental reach for the
  network fails there rather than on a reviewer's machine. It also reseeds from the
  fixed seed and fails on any diff against the committed `state/` — "reproducible" is
  checked, not claimed — and greps the full history for a leaked key on every push

Not done:

- **Cassettes are not recorded, so `--replay` has nothing to serve.** Recording is one
  command (`python scripts/record_cassettes.py`) and needs an API key this repo does
  not have. Hand-writing files into `tests/cassettes/` would make `--replay` a
  re-enactment rather than a recording, so it is not done. Contract tests run against
  a programmable test double instead, which tests a different and also necessary
  thing: that the pipeline survives responses the model *might* produce — hallucinated
  ticket IDs, duplicate entries, out-of-range ranks — which no recording contains.
- **Every committed number is therefore from the deterministic path.** The interesting
  measurement — conflict recall with the LLM detector, and stability with real model
  calls — needs `scripts/record_cassettes.py` run once.
- Routing and human handoff are stubbed, as permitted.

### Known limitations

- No counterfactual is ever observed. If we deprioritise and the merchant churns, we
  cannot know faster service would have saved them. The system can validate its
  *estimates* but never its *values*.
- History is synthetic, by design: real data cannot set up a demonstration of a
  credibility discount, because it does not know what you are about to demonstrate.
- Batch, not stream — and it costs something measurable. A ticket arriving mid-batch
  that engages a charter rule should preempt immediately; anything else waits for the
  next boundary. That is described, not built, and the audit above shows the bill:
  the 5-day waiting ceiling was crossed by 8 hours because promotion can only happen
  at a boundary the ticket had already passed. True streaming means an
  arrival-triggered re-rank with a hysteresis threshold so the queue does not thrash.
- Capacity is treated as fungible — a billing specialist cannot actually take a
  checkout bug.
- Per-ticket contractual SLA deadlines are carried as a field but not enforced. They
  are distinct from fairness: a contractual clock is not a moral one.
- `state/` is a working directory, not a fixture. Every run mutates it. The committed
  copy is the seeded baseline; `python scripts/seed_history.py` restores it, and
  `scripts/run_demo.py` does that for you before every regeneration.
- Cassette keys hash the exact prompts, so any edit to `llm/prompts.py` invalidates
  every cassette. Deliberate — a cassette that survived a prompt change would be
  replaying an answer to a question the system no longer asks — but it means
  `--replay` degrades quietly to the deterministic path after a prompt edit, and you
  find out from the degraded banner rather than from an error.

---

## What I would do next

- **Record the cassettes and rerun both measurements.** The 9/21 recall figure is the
  fallback detector's, and the whole argument for the LLM stage is that it closes that
  gap. Right now that argument is a hypothesis with a number attached to its null case.
- **Promote on projected wait**, not elapsed wait, so the charter ceiling stops being
  a line the queue lands on.
- Randomised ranking holdout on a small slice, to measure the causal effect of
  prioritisation — the only way any of this becomes falsifiable.
- Validate the **estimation** layer against real resolution times from the
  Eclipse/Mozilla defect dataset — does a category's predicted median duration
  correlate with actual fix time? Estimation only; the valuation layer stays out of it
  permanently, for the reasons in
  [Why the data is synthetic](#why-the-data-is-synthetic-and-which-real-datasets-were-rejected).
- Human overrides are the missing label. `decision_log.jsonl` is already the substrate
  for treating posture selection as a contextual bandit over them.
- Recency decay on credibility, so an account can rehabilitate. Today NorthPeak's
  0.27 is a life sentence.

---

## Licence

MIT — see [LICENSE](LICENSE).
