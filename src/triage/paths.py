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


def batches_dir() -> Path:
    return data_dir() / "batches"


def sources_dir() -> Path:
    return data_dir() / "sources"


def history_dir() -> Path:
    return data_dir() / "history"


def state_dir() -> Path:
    return repo_root() / "state"


def reports_dir() -> Path:
    return repo_root() / "reports"


def cassette_dir() -> Path:
    override = os.environ.get("TRIAGE_CASSETTE_DIR")
    if override:
        return Path(override)
    return repo_root() / "tests" / "cassettes"


def decision_log_path() -> Path:
    return repo_root() / "reports" / "decision_log.jsonl"
