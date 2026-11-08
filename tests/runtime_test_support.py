"""Import the API with isolated storage and disabled external integrations."""

from __future__ import annotations

import atexit
import importlib
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


_directory = tempfile.TemporaryDirectory(prefix="lumen-api-tests-", ignore_cleanup_errors=True)
_root = Path(_directory.name)


def _cleanup() -> None:
    logging.shutdown()
    _directory.cleanup()


atexit.register(_cleanup)

# main initializes SQLite at import time. Set these before importing it, even
# when the test is launched directly without the operator's shell overrides.
with patch.dict(os.environ, {
    "LUMEN_DB_PATH": str(_root / "test.db"),
    "LOG_FILE_PATH": str(_root / "lumen.log"),
    "OLLAMA_DEBUG_DUMP_PATH": str(_root / "ollama.json"),
    "OLLAMA_BASE_URL": "http://127.0.0.1:1",
    "HOME_ASSISTANT_URL": "",
    "HOME_ASSISTANT_TOKEN": "",
    "HOME_ASSISTANT_WEBHOOK_URL": "",
    "HOME_ASSISTANT_WEBHOOK_ID": "",
    "HOME_ASSISTANT_CALENDAR_ENTITY_IDS": "",
    "HOME_ASSISTANT_CALENDAR_WRITE_ENTITY_ID": "",
    "HOME_ASSISTANT_CALENDAR_TARGET_CHAT_ID": "",
    "TELEGRAM_API_ID": "0",
    "TELEGRAM_API_HASH": "",
    "TELEGRAM_SESSION_PATH": str(_root / "telegram"),
    "NUTRITION_DIRECTORY": str(_root / "nutrition"),
}):
    main = importlib.import_module("lumen.main")
