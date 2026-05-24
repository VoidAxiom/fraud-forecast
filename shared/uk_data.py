from __future__ import annotations

import random
from datetime import date

UK_CITIES: list[tuple[str, float, float, float, str]] = [
    ("London", 0.35, 51.5074, -0.1278, "Greater London"),
    ("Birmingham", 0.08, 52.4862, -1.8904, "West Midlands"),
    ("Manchester", 0.08, 53.4808, -2.2426, "Greater Manchester"),
    ("Glasgow", 0.06, 55.8642, -4.2518, "Glasgow City"),
    ("Leeds", 0.06, 53.8008, -1.5491, "West Yorkshire"),
    ("Liverpool", 0.05, 53.4084, -2.9916, "Merseyside"),
    ("Bristol", 0.05, 51.4545, -2.5879, "Bristol"),
    ("Edinburgh", 0.05, 55.9533, -3.1883, "City of Edinburgh"),
    ("Sheffield", 0.04, 53.3811, -1.4701, "South Yorkshire"),
    ("Newcastle upon Tyne", 0.04, 54.9783, -1.6178, "Tyne and Wear"),
    ("Cardiff", 0.02, 51.4816, -3.1791, "Cardiff"),
    ("Belfast", 0.015, 54.5973, -5.9301, "Belfast"),
    ("Nottingham", 0.015, 52.9548, -1.1581, "Nottinghamshire"),
    ("Southampton", 0.015, 50.9097, -1.4044, "Hampshire"),
    ("Brighton", 0.015, 50.8225, -0.1372, "East Sussex"),
    ("Cambridge", 0.01, 52.2053, 0.1218, "Cambridgeshire"),
    ("Oxford", 0.01, 51.7520, -1.2577, "Oxfordshire"),
    ("Reading", 0.01, 51.4543, -0.9781, "Berkshire"),
    ("Leicester", 0.015, 52.6369, -1.1398, "Leicestershire"),
    ("Coventry", 0.015, 52.4068, -1.5197, "West Midlands"),
]

UK_POSTCODE_AREAS: dict[str, list[str]] = {
    "London": ["E", "EC", "N", "NW", "SE", "SW", "W", "WC"],
    "Birmingham": ["B"],
    "Manchester": ["M"],
    "Glasgow": ["G"],
    "Leeds": ["LS"],
    "Liverpool": ["L"],
    "Bristol": ["BS"],
    "Edinburgh": ["EH"],
    "Sheffield": ["S"],
    "Newcastle upon Tyne": ["NE"],
    "Cardiff": ["CF"],
    "Belfast": ["BT"],
    "Nottingham": ["NG"],
    "Southampton": ["SO"],
    "Brighton": ["BN"],
    "Cambridge": ["CB"],
    "Oxford": ["OX"],
    "Reading": ["RG"],
    "Leicester": ["LE"],
    "Coventry": ["CV"],
}

CUISINE_WEIGHTS: dict[str, float] = {
    "Indian": 0.15,
    "Chinese": 0.12,
    "Italian": 0.08,
    "Pizza": 0.07,
    "Kebab": 0.06,
    "Turkish": 0.04,
    "Fish & Chips": 0.08,
    "Burger": 0.06,
    "American": 0.04,
    "Thai": 0.05,
    "Japanese": 0.03,
    "Sushi": 0.02,
    "Caribbean": 0.03,
    "Lebanese": 0.03,
    "Polish": 0.02,
    "British": 0.03,
    "Pub": 0.02,
    "Vietnamese": 0.02,
    "Other": 0.05,
}

POS_SYSTEMS: list[tuple[str, float]] = [
    ("Lightspeed", 0.15),
    ("Square", 0.15),
    ("Epos Now", 0.12),
    ("Toast", 0.08),
    ("Clover", 0.08),
    ("Vita Mojo", 0.05),
    ("Deliveroo Tablet", 0.10),
    ("Uber Eats Tablet", 0.10),
    ("In-House", 0.17),
]

CARD_BRANDS: list[tuple[str, float]] = [
    ("VISA", 0.45),
    ("MASTERCARD", 0.35),
    ("AMEX", 0.08),
    ("MAESTRO", 0.05),
    ("OTHER", 0.07),
]

UK_CARD_ISSUERS: list[tuple[str, list[str], str, bool, float]] = [
    ("Barclays", ["492181", "492182", "492183", "492184", "492185"], "DEBIT", False, 0.15),
    ("HSBC UK", ["453978", "453979", "465859", "465860"], "DEBIT", False, 0.12),
    ("Lloyds Bank", ["454313", "454314", "454742", "454743"], "DEBIT", False, 0.10),
    ("NatWest", ["465902", "465903", "465904", "465905"], "DEBIT", False, 0.08),
    ("Santander UK", ["491880", "491881", "511234"], "DEBIT", False, 0.07),
    ("Halifax", ["454742", "465925"], "DEBIT", False, 0.05),
    ("Nationwide", ["492930", "492931"], "DEBIT", False, 0.05),
    ("Monzo Bank", ["535522", "535523", "539923"], "DEBIT", True, 0.10),
    ("Revolut", ["516842", "535410", "537956"], "DEBIT", True, 0.08),
    ("Starling Bank", ["548175", "548176"], "DEBIT", True, 0.05),
    ("Chase UK", ["483138"], "DEBIT", True, 0.03),
    ("American Express UK", ["374622", "374623", "378282"], "CREDIT", False, 0.05),
    ("Foreign EU", ["435060", "424519"], "DEBIT", False, 0.04),
    ("Foreign US", ["414709", "424519"], "CREDIT", False, 0.03),
]

EMAIL_DOMAINS: list[tuple[str, float]] = [
    ("gmail.com", 0.35),
    ("outlook.com", 0.12),
    ("hotmail.com", 0.15),
    ("yahoo.co.uk", 0.08),
    ("icloud.com", 0.10),
    ("btinternet.com", 0.05),
    ("googlemail.com", 0.03),
    ("live.co.uk", 0.02),
    ("hotmail.co.uk", 0.05),
    ("sky.com", 0.03),
    ("virginmedia.com", 0.02),
]

DISPOSABLE_EMAIL_DOMAINS: list[str] = [
    "mailinator.com",
    "guerrillamail.com",
    "tempmail.io",
    "throwawaymail.com",
    "10minutemail.com",
    "mailsac.com",
]

DISPOSABLE_DOMAIN_RATE: float = 0.05

PAYMENT_GATEWAYS: list[str] = [
    "ADYEN",
    "STRIPE",
    "BRAINTREE",
    "WORLDPAY",
    "CHECKOUT_COM",
]

UK_BANK_HOLIDAYS_2026: list[date] = [
    date(2026, 1, 1),
    date(2026, 4, 3),
    date(2026, 4, 6),
    date(2026, 5, 4),
    date(2026, 5, 25),
    date(2026, 8, 31),
    date(2026, 12, 25),
    date(2026, 12, 28),
]


_POSTCODE_SUBDISTRICT_CHARS = "ABCDEFGHJKPSTUW"
_POSTCODE_UNIT_CHARS = "ABDEFGHJLNPQRSTUVWXYZ"


def random_uk_postcode(city: str, *, rng: random.Random) -> str:
    area = rng.choice(UK_POSTCODE_AREAS.get(city, ["GIR"]))
    district_num = rng.randint(1, 9)
    subdistrict = ""
    if rng.random() < 0.5:
        subdistrict = rng.choice(_POSTCODE_SUBDISTRICT_CHARS)
    sector = rng.randint(0, 9)
    unit = (
        rng.choice(_POSTCODE_UNIT_CHARS)
        + rng.choice(_POSTCODE_UNIT_CHARS)
    )
    return f"{area}{district_num}{subdistrict} {sector}{unit}"
