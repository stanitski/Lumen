"""Windows one-shot wake task adapter for the isolated reminder service."""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
import re
from datetime import datetime
from pathlib import Path


class WindowsWakeAdapter:
    """Keep one Task Scheduler task pointed at Lumen's nearest reminder."""

    TASK_NAME = "LumenReminderWake"

    def __init__(self, *, db_path: str, logger: logging.Logger | None = None) -> None:
        self._db_path = db_path
        self._working_directory = str(self._runtime_working_directory())
        self._logger = logger or logging.getLogger("lumen.reminder_wake")

    def schedule(self, due_at: datetime | None) -> None:
        if sys.platform != "win32":
            return
        if due_at is None:
            result = self._run_powershell(
                "Unregister-ScheduledTask -TaskName "
                f"{self._powershell_literal(self.TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue"
            )
            if result.returncode:
                self._logger.warning("Could not remove reminder wake task (exit code %s).", result.returncode)
            else:
                self._logger.info("Reminder wake task removed.")
            return
        command = self._wake_command()
        executable, arguments = self._command_parts(command)
        script = (
            "$action = New-ScheduledTaskAction "
            f"-Execute {self._powershell_literal(executable)} "
            f"-Argument {self._powershell_literal(arguments)} "
            f"-WorkingDirectory {self._powershell_literal(self._working_directory)}; "
            "$trigger = New-ScheduledTaskTrigger -Once "
            f"-At ([datetime]{self._powershell_literal(due_at.astimezone().isoformat())}); "
            "$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew; "
            "$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited; "
            "Register-ScheduledTask "
            f"-TaskName {self._powershell_literal(self.TASK_NAME)} "
            "-Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null"
        )
        result = self._run_powershell(script)
        if result.returncode:
            self._logger.warning("Could not schedule reminder wake task (exit code %s).", result.returncode)
            raise RuntimeError((result.stderr or result.stdout or "ScheduledTasks registration failed").strip())
        self._logger.info("Reminder wake task scheduled for %s.", due_at.isoformat())

    @staticmethod
    def _powershell_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
            capture_output=True,
            text=True,
            check=False,
        )

    def _wake_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --run-reminders'
        return f'"{sys.executable}" -m lumen.reminder_wakeup --db-path "{self._db_path}"'

    @staticmethod
    def _runtime_working_directory() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parents[2]

    @property
    def working_directory(self) -> str:
        return self._working_directory

    @staticmethod
    def _command_parts(command: str) -> tuple[str, str]:
        quoted = re.match(r'^"([^"]+)"\s*(.*)$', command)
        return (quoted.group(1), quoted.group(2)) if quoted else (command, "")
