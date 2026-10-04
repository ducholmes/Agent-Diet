"""APR outcome taxonomy based only on verified baseline/post test IDs."""
from __future__ import annotations

from dataclasses import dataclass


VALID_OUTCOMES = frozenset({"plausible", "cleanfix", "noisefix", "nonefix", "negfix"})


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    fixed_test_ids: tuple[str, ...] = ()
    regression_test_ids: tuple[str, ...] = ()


def classify(initial_failures: tuple[str, ...], post_failures: tuple[str, ...], *, valid: bool) -> Outcome:
    if not valid:
        return Outcome("invalid")
    initial, post = set(initial_failures), set(post_failures)
    fixed, regressions = tuple(sorted(initial - post)), tuple(sorted(post - initial))
    if not post:
        return Outcome("plausible" if initial else "nonefix", fixed, regressions)
    if fixed and regressions:
        return Outcome("noisefix", fixed, regressions)
    if fixed:
        return Outcome("cleanfix", fixed, regressions)
    if regressions:
        return Outcome("negfix", fixed, regressions)
    return Outcome("nonefix", fixed, regressions)
