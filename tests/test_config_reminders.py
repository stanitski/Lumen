from __future__ import annotations

import unittest

from pydantic import ValidationError

from lumen.config import Settings


class ReminderSettingsTests(unittest.TestCase):
    def test_reminder_policy_defaults(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(30, settings.reminder_grace_minutes)
        self.assertEqual(15, settings.reminder_snooze_minutes)
        self.assertEqual(10, settings.reminder_management_session_ttl_minutes)

    def test_reminder_policy_requires_positive_minutes(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(reminder_grace_minutes=0)
        with self.assertRaises(ValidationError):
            Settings(reminder_snooze_minutes=-1)
        with self.assertRaises(ValidationError):
            Settings(reminder_management_session_ttl_minutes=0)
