"""Eager-write helpers for fraud pattern entity creation.

Only devices can be safely created here without an existing seeded user:
payment methods and addresses carry user_id foreign keys.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

_APP_VERSIONS = ["4.30.1", "4.31.0", "4.32.1", "4.33.0", "4.34.2", "4.35.0"]
_IOS_BROWSERS = ["Safari", "Chrome", "Firefox"]
_ANDROID_BROWSERS = ["Chrome", "Firefox", "Samsung"]
_DESKTOP_BROWSERS = ["Chrome", "Safari", "Firefox", "Edge"]


@dataclass(frozen=True)
class _DeviceProfile:
    device_type: str
    platform: str
    os_version: str
    app_version: str
    browser_name: str
    browser_version: str
    screen_resolution: str


def _select_device_type_and_platform(
    rng: random.Random,
    platform_bias: str | None,
) -> tuple[str, str]:
    if platform_bias == "iOS":
        return "MOBILE_APP", "iOS"
    if platform_bias == "Android":
        return "MOBILE_APP", "Android"
    if platform_bias == "Web":
        return rng.choice([("MOBILE_WEB", ""), ("DESKTOP_WEB", "")])

    choices = [
        ("MOBILE_APP", "iOS", 50),
        ("MOBILE_APP", "Android", 35),
        ("MOBILE_WEB", "", 10),
        ("DESKTOP_WEB", "", 4),
        ("TABLET", "", 1),
    ]
    weighted: list[tuple[str, str]] = []
    for device_type, platform, weight in choices:
        weighted.extend([(device_type, platform)] * weight)
    return rng.choice(weighted)


def _build_device_profile(
    rng: random.Random,
    platform_bias: str | None,
) -> _DeviceProfile:
    device_type, platform = _select_device_type_and_platform(rng, platform_bias)

    if device_type == "MOBILE_APP" and platform == "iOS":
        return _DeviceProfile(
            device_type=device_type,
            platform=platform,
            os_version=f"iOS {rng.randint(14, 17)}.{rng.randint(0, 5)}",
            app_version=rng.choice(_APP_VERSIONS),
            browser_name="",
            browser_version="",
            screen_resolution=rng.choice(["390x844", "414x896", "375x667", "428x926"]),
        )

    if device_type == "MOBILE_APP" and platform == "Android":
        return _DeviceProfile(
            device_type=device_type,
            platform=platform,
            os_version=f"Android {rng.randint(10, 14)}",
            app_version=rng.choice(_APP_VERSIONS),
            browser_name="",
            browser_version="",
            screen_resolution=rng.choice(["360x800", "390x844", "412x915", "360x780"]),
        )

    if device_type == "MOBILE_WEB":
        mobile_platform = rng.choice(["iOS", "Android"])
        return _DeviceProfile(
            device_type=device_type,
            platform=mobile_platform,
            os_version=(
                f"iOS {rng.randint(14, 17)}"
                if mobile_platform == "iOS"
                else f"Android {rng.randint(10, 14)}"
            ),
            app_version="",
            browser_name=rng.choice(_IOS_BROWSERS + _ANDROID_BROWSERS),
            browser_version=f"{rng.randint(100, 120)}.0",
            screen_resolution=rng.choice(["390x844", "414x896", "360x800", "412x915"]),
        )

    if device_type == "DESKTOP_WEB":
        desktop_platform = rng.choice(["Windows", "macOS", "Linux"])
        return _DeviceProfile(
            device_type=device_type,
            platform=desktop_platform,
            os_version=desktop_platform,
            app_version="",
            browser_name=rng.choice(_DESKTOP_BROWSERS),
            browser_version=f"{rng.randint(100, 120)}.0",
            screen_resolution=rng.choice(["1920x1080", "2560x1440", "1440x900", "1280x720"]),
        )

    tablet_platform = rng.choice(["iOS", "Android"])
    return _DeviceProfile(
        device_type="TABLET",
        platform=tablet_platform,
        os_version=(
            f"iPadOS {rng.randint(14, 17)}"
            if tablet_platform == "iOS"
            else f"Android {rng.randint(10, 14)}"
        ),
        app_version=rng.choice(_APP_VERSIONS),
        browser_name="",
        browser_version="",
        screen_resolution=rng.choice(["768x1024", "1024x1366", "810x1080"]),
    )


async def create_fresh_device(
    rng: random.Random,
    conn: asyncpg.Connection,
    now: datetime,
    *,
    platform_bias: str | None = None,
) -> uuid.UUID:
    """INSERT a new devices row with realistic attributes and return its UUID."""
    device_id = uuid.UUID(int=rng.getrandbits(128))
    profile = _build_device_profile(rng, platform_bias)
    fp_raw = (
        f"fraud:{device_id}:{profile.device_type}:"
        f"{profile.platform}:{rng.getrandbits(64)}"
    )
    fingerprint = hashlib.sha256(fp_raw.encode()).hexdigest()

    await conn.execute(
        """
        INSERT INTO devices (
            device_id, device_fingerprint, device_type, platform,
            os_version, app_version, browser_name, browser_version,
            screen_resolution, timezone, language,
            is_rooted_jailbroken, is_emulator, is_vpn_detected,
            first_seen_at, last_seen_at, unique_users_count, risk_score
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6, $7, $8,
            $9, $10, $11,
            $12, $13, $14,
            $15, $16, $17, $18
        )
        ON CONFLICT (device_id) DO NOTHING
        """,
        device_id,
        fingerprint,
        profile.device_type,
        profile.platform,
        profile.os_version,
        profile.app_version,
        profile.browser_name,
        profile.browser_version,
        profile.screen_resolution,
        "Europe/London",
        "en-GB",
        rng.random() < 0.02,
        rng.random() < 0.005,
        rng.random() < 0.15,
        now,
        now,
        1,
        Decimal("0.0"),
    )
    return device_id
