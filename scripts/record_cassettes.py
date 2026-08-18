"""Record cassettes for every LLM call the demo makes, in one command.

    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/record_cassettes.py

Cassettes are recorded API responses keyed by a hash of (model, stage, tool name,
system prompt, user prompt). They do three jobs with one artefact: stable CI, a free
test suite, and the reviewer's zero-setup path.

The key is a hash of the exact prompts, which means **cassettes are invalidated by any
prompt edit**. That is deliberate. A cassette that survived a prompt change would be
replaying an answer to a question the system no longer asks, and `--replay` would be
quietly demonstrating an older version of the software. Re-record after touching
`llm/prompts.py`.

What gets recorded: every stage of a full run over every batch, plus the standalone
`plan` call, plus the eval's detection pass. Run this once and `--replay` covers the
whole demo.

Cost, roughly: ~10 calls per batch x 3 batches on claude-sonnet-5, most of the system
prompt served from cache after the first call. Single-digit cents.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from triage import evaluate as evaluate_mod  # noqa: E402
from triage import paths  # noqa: E402
from triage.llm.client import LLMClient  # noqa: E402
from triage.pipeline import Context, RunOptions, run_batch  # noqa: E402


def batches() -> list[Path]:
    found = list(paths.batches_dir().glob("batch_*.json"))
    return sorted(found, key=lambda p: json.loads(p.read_text(encoding="utf-8"))["batch_id"])


def count(cassette_dir: Path) -> dict[str, int]:
    if not cassette_dir.exists():
        return {}
    return {
        stage.name: len(list(stage.glob("*.json")))
        for stage in sorted(cassette_dir.iterdir())
        if stage.is_dir()
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", action="store_true",
                    help="delete existing cassettes first; use after a prompt edit")
    ap.add_argument("--model", default=None, help="override TRIAGE_MODEL")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set.\n\n"
            "Recording cassettes means making real API calls; there is no way to\n"
            "produce them without a key, and hand-writing files into tests/cassettes/\n"
            "would make `--replay` a re-enactment rather than a recording.\n\n"
            "Set the key and run this again. Until then `--no-llm` is the honest path,\n"
            "and every report it produces says so at the top.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    cassette_dir = paths.cassette_dir()
    if args.clean and cassette_dir.exists():
        shutil.rmtree(cassette_dir)
        print(f"removed {cassette_dir.relative_to(ROOT)}")
    cassette_dir.mkdir(parents=True, exist_ok=True)

    if args.model:
        os.environ["TRIAGE_MODEL"] = args.model

    # State is NOT persisted while recording. A recording pass that also advanced the
    # fairness ledger would leave the repo in a state no committed run produced.
    for path in batches():
        print(f"\nrecording {path.name} ...")
        ctx = Context.load(path)
        report = run_batch(
            ctx,
            RunOptions(use_llm=True, replay=False, assume_yes=True, persist_state=False),
        )
        print(f"   batch {report.batch_id}: posture {report.posture}, "
              f"{len(report.conflicts)} conflicts, "
              f"{len(report.degraded_stages)} degraded stage(s)")
        for reason in report.degraded_stages:
            print(f"   ! {reason}")

    print("\nrecording the eval's detection pass ...")
    evaluate_mod.evaluate(use_llm=True)

    print("\ncassettes recorded:")
    total = 0
    for stage, n in sorted(count(cassette_dir).items()):
        print(f"   {stage:<14} {n}")
        total += n
    print(f"   {'total':<14} {total}")
    print(f"\nin {cassette_dir.relative_to(ROOT)}")
    print("\nVerify with:  python -m triage run --all --replay")


if __name__ == "__main__":
    # Recording sets record mode for every client the pipeline constructs internally.
    os.environ.setdefault("TRIAGE_CASSETTE_MODE", "record")
    main()
