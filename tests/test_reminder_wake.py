from __future__ import annotations

import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from lumen.services.reminder_wake import WindowsWakeAdapter


class WindowsWakeAdapterTests(unittest.TestCase):
    def test_schedule_allows_wake_and_battery_execution(self) -> None:
        adapter = WindowsWakeAdapter(db_path=r"C:\\Lumen\\data\\lumen.db")
        completed = subprocess.CompletedProcess([], 0, "", "")

        with patch("lumen.services.reminder_wake.sys.platform", "win32"), patch.object(
            adapter, "_run_powershell", return_value=completed
        ) as run_powershell, self.assertLogs("lumen.reminder_wake", "INFO") as logs:
            adapter.schedule(datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc))

        script = run_powershell.call_args.args[0]
        self.assertIn("-WakeToRun", script)
        self.assertIn("-StartWhenAvailable", script)
        self.assertIn("-AllowStartIfOnBatteries", script)
        self.assertIn("-DontStopIfGoingOnBatteries", script)
        self.assertIn("Reminder wake task scheduled", "\n".join(logs.output))

