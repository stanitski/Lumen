"""Timezone loading that remains available in the packaged Windows runtime."""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


KYIV_ZONE_KEY = "Europe/Kyiv"


def _kyiv_tzif_path() -> Path:
    """Return the bundled IANA TZif path for source and PyInstaller one-file runs."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "lumen" / "data" / "zoneinfo" / "Europe" / "Kyiv"
    return Path(__file__).resolve().parent / "data" / "zoneinfo" / "Europe" / "Kyiv"


def _system_kyiv_timezone() -> ZoneInfo:
    return ZoneInfo(KYIV_ZONE_KEY)


def kyiv_timezone() -> ZoneInfo:
    """Load IANA Europe/Kyiv, falling back to the bundled TZif database entry."""
    try:
        return _system_kyiv_timezone()
    except ZoneInfoNotFoundError as error:
        try:
            with _kyiv_tzif_path().open("rb") as tzif_file:
                return ZoneInfo.from_file(tzif_file, key=KYIV_ZONE_KEY)
        except FileNotFoundError as asset_error:
            raise ZoneInfoNotFoundError(
                "Europe/Kyiv timezone data is unavailable; package lumen/data/zoneinfo/Europe/Kyiv."
            ) from asset_error
