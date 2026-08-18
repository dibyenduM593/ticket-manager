"""Conflicting-signal ticket triage agent.

Estimation and valuation live in separate modules and never write to each other.
History writes only to estimation. The business declares only valuation. They meet
at exactly one line of code, in `scorer.severity()`.
"""

__version__ = "0.1.0"
