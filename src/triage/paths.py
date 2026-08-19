"""Where things live. One place, so tests can redirect the whole tree at a fixture."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_ROOT = "TRIAGE_ROOT"


def repo_root() -> Path:
    """Project root. Overridable with TRIAGE_ROOT so tests can run against fixtures."""
    override = os.environ.get(_ENV_ROOT)
    if override:
        return Path(override).resolve()
    # src/triage/paths.py -> src/triage -> src -> root
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return repo_root() / "config"


def postures_dir() -> Path:
    return config_dir() / "postures"


def data_dir() -> Path:
    return repo_root() / "data"


def eval_dir() -> Path:
    """Ground-truth fixtures for measuring conflict-detection recall.

    These are NOT the system's input. The system's input is whatever a person types
    into the web form, attributed to a merchant they pick -- see `web/`. What lives
    here is a set of ticket batches with hand-authored `planted_conflicts`, which
    exist so `triage eval` can measure recall against contradictions we know are
    present. Ground truth is the whole point of the directory, and it is also why
    nothing under `src/triage/llm/` may read that field.
    """
    return data_dir() / "eval"


def sources_dir() -> Path:
    return data_dir() / "sources"


def history_dir() -> Path:
    return data_dir() / "history"


def state_dir() -> Path:
    return repo_root() / "state"


def reports_dir() -> Path:
    return repo_root() / "reports"


def decision_log_path() -> Path:
    return repo_root() / "reports" / "decision_log.jsonl"
