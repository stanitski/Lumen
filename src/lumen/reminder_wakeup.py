"""Entry point invoked by the Windows one-shot reminder wake task."""

from __future__ import annotations

import argparse
import asyncio

from lumen.config import Settings
from lumen.services.delivery import DeliveryService
from lumen.services.reminder_wake import WindowsWakeAdapter
from lumen.services.reminders import ReminderService
from lumen.storage.db import Database


def run(db_path: str | None = None) -> int:
    settings = Settings()
    database = Database(db_path or settings.lumen_db_path)
    database.init()
    service = ReminderService(database.session, DeliveryService(settings), WindowsWakeAdapter(db_path=database.db_path))
    asyncio.run(service.run_due())
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path")
    args = parser.parse_args()
    raise SystemExit(run(args.db_path))
