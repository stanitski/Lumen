from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import lumen.timezones as timezones


class KyivTimezoneTests(unittest.TestCase):
    def test_packaged_tzif_path_uses_pyinstaller_bundle_root(self) -> None:
        with patch.object(timezones.sys, "_MEIPASS", r"C:\\bundle", create=True):
            path = timezones._kyiv_tzif_path()

        self.assertEqual(Path(r"C:\\bundle") / "lumen" / "data" / "zoneinfo" / "Europe" / "Kyiv", path)

    def test_bundled_tzif_preserves_kyiv_dst_when_system_database_is_missing(self) -> None:
        with patch.object(timezones, "_system_kyiv_timezone", side_effect=ZoneInfoNotFoundError("missing")):
            kyiv = timezones.kyiv_timezone()

        self.assertEqual("Europe/Kyiv", kyiv.key)
        self.assertEqual(timedelta(hours=2), datetime(2026, 1, 20, 12, tzinfo=kyiv).utcoffset())
        self.assertEqual(timedelta(hours=3), datetime(2026, 7, 20, 12, tzinfo=kyiv).utcoffset())


if __name__ == "__main__":
    unittest.main()
