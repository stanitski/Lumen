"""Create or refresh the local Telegram MTProto session used by Lumen."""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lumen.config import Settings  # noqa: E402


async def main() -> None:
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ImportError as exc:
        raise SystemExit("Telethon is not installed. Run: .\\.venv\\Scripts\\pip.exe install -e .") from exc

    settings = Settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash.strip():
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")

    session_path = Path(settings.telegram_session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_path), settings.telegram_api_id, settings.telegram_api_hash.strip())
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            username = getattr(me, "username", "") or getattr(me, "phone", "") or getattr(me, "id", "")
            print(f"Telegram session is already authorized: {username}")
            return

        phone = input("Telegram phone number: ").strip()
        if not phone:
            raise SystemExit("Phone number is required.")
        await client.send_code_request(phone)
        code = input("Telegram login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = getpass.getpass("Telegram 2FA password: ")
            await client.sign_in(password=password)

        me = await client.get_me()
        username = getattr(me, "username", "") or getattr(me, "phone", "") or getattr(me, "id", "")
        print(f"Telegram session authorized: {username}")
        print(f"Session path: {session_path}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
