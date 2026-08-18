"""Loaders for the declared side of the system: company state, postures, charter,
valuation curves.

Nothing in here reads `state/`. That separation is enforced by the module boundary
and asserted by `tests/test_separation.py`.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .models import CompanyState, Posture


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_company_state(path: Path | str | None = None) -> CompanyState:
    p = Path(path) if path else paths.config_dir() / "company_state.yaml"
    return CompanyState.model_validate(_read_yaml(p))


@functools.lru_cache(maxsize=None)
def _postures_cached(directory: str) -> dict[str, Posture]:
    out: dict[str, Posture] = {}
    for f in sorted(Path(directory).glob("*.yaml")):
        posture = Posture.model_validate(_read_yaml(f))
        out[posture.name] = posture
    return out


def load_postures(directory: Path | str | None = None) -> dict[str, Posture]:
    d = Path(directory) if directory else paths.postures_dir()
    return dict(_postures_cached(str(d)))


def load_posture(name: str, directory: Path | str | None = None) -> Posture:
    postures = load_postures(directory)
    if name not in postures:
        raise KeyError(
            f"unknown posture {name!r}; available: {', '.join(sorted(postures))}"
        )
    return postures[name]


@functools.lru_cache(maxsize=None)
def load_charter(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else paths.config_dir() / "charter.yaml"
    return _read_yaml(p)


@functools.lru_cache(maxsize=None)
def load_valuation_config(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else paths.config_dir() / "valuation.yaml"
    return _read_yaml(p)


def clear_caches() -> None:
    _postures_cached.cache_clear()
    load_charter.cache_clear()
    load_valuation_config.cache_clear()
