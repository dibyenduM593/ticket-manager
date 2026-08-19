"""Load `.env` into the process environment. Stdlib only.

The README and `.env.example` both promise that putting a key in `.env` switches the
LLM path on. Nothing read the file, so the promise was false: the key sat on disk and
`LLMClient.available` stayed False, which presents as "the model is not answering"
rather than as "your configuration was never loaded". That is the worst kind of bug --
it looks like a capability problem and it is a plumbing problem.

A dependency (`python-dotenv`) would fix it and cost the zero-install claim in the
first line of the README. This is thirty lines and no dependency.

REAL ENVIRONMENT WINS. Anything already exported is left alone, so `.env` cannot
silently override a key set deliberately for one command, and CI -- which sets no key
on purpose, to prove the deterministic path stands alone -- is unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths


def load_dotenv(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    """Read KEY=VALUE lines into os.environ. Returns what it set.

    Deliberately small: no interpolation, no export keyword, no multi-line values.
    A config format nobody can predict the behaviour of is worse than one that only
    does the obvious thing.
    """
    p = Path(path) if path else paths.repo_root() / ".env"
    if not p.exists():
        return {}

    applied: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        # Strip one matched pair of surrounding quotes, nothing cleverer.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            continue  # an empty assignment is not a value; leave the environment alone
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
