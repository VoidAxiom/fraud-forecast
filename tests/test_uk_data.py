"""Static-data invariants for shared/uk_data.py — no DB."""

from __future__ import annotations

import math
import random
import re

from shared.uk_data import (
    CARD_BRANDS,
    CUISINE_WEIGHTS,
    EMAIL_DOMAINS,
    POS_SYSTEMS,
    UK_CARD_ISSUERS,
    UK_CITIES,
    random_uk_postcode,
)

UK_POSTCODE_REGEX = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}$")


def _assert_sums_to_one(label: str, weights: list[float]) -> None:
    total = sum(weights)
    assert math.isclose(total, 1.0, abs_tol=1e-9), f"{label} sums to {total}"


def test_uk_cities_weights_sum_to_one() -> None:
    _assert_sums_to_one("UK_CITIES", [c[1] for c in UK_CITIES])


def test_cuisine_weights_sum_to_one() -> None:
    _assert_sums_to_one("CUISINE_WEIGHTS", list(CUISINE_WEIGHTS.values()))


def test_pos_systems_weights_sum_to_one() -> None:
    _assert_sums_to_one("POS_SYSTEMS", [p[1] for p in POS_SYSTEMS])


def test_card_brands_weights_sum_to_one() -> None:
    _assert_sums_to_one("CARD_BRANDS", [b[1] for b in CARD_BRANDS])


def test_uk_card_issuers_weights_sum_to_one() -> None:
    _assert_sums_to_one("UK_CARD_ISSUERS", [c[4] for c in UK_CARD_ISSUERS])


def test_email_domains_weights_sum_to_one() -> None:
    _assert_sums_to_one("EMAIL_DOMAINS", [d[1] for d in EMAIL_DOMAINS])


def test_random_uk_postcode_deterministic() -> None:
    p1 = random_uk_postcode("London", rng=random.Random(42))
    p2 = random_uk_postcode("London", rng=random.Random(42))
    assert p1 == p2, f"non-deterministic: {p1} vs {p2}"


def test_random_uk_postcode_matches_regex() -> None:
    for city in ["London", "Birmingham", "Manchester", "Glasgow", "Leeds"]:
        for seed in [1, 7, 42, 100, 1000]:
            pc = random_uk_postcode(city, rng=random.Random(seed))
            assert UK_POSTCODE_REGEX.match(pc), (
                f"postcode {pc!r} for {city} (seed {seed}) does not match UK regex"
            )
