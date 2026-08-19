"""One-time interactive helper: log in once, print a Telethon StringSession.

Run this **on your own machine**, never in CI and never on a server:

    pip install -r requirements-collector.txt
    python scripts/make_session_string.py

It asks for your phone number and the login code Telegram sends to your
Telegram app, then prints a session string. That string is equivalent to
your Telegram account password — anyone holding it can act as you. Paste
it straight into the TG_SESSION_STRING GitHub Actions secret and do not
save it to a file, paste it into a chat, or commit it anywhere.

Nothing here is stored on disk: the session lives in memory only and is
printed once.
"""

from __future__ import annotations

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession


def _prompt(label: str, env_var: str) -> str:
    value = os.environ.get(env_var) or input(f"{label}: ").strip()
    if not value:
        sys.exit(f"error: {label} is required")
    return value


async def main() -> int:
    print(__doc__)
    print("-" * 72)

    try:
        api_id = int(_prompt("TG_API_ID (from https://my.telegram.org)", "TG_API_ID"))
    except ValueError:
        sys.exit("error: TG_API_ID must be a number")
    api_hash = _prompt("TG_API_HASH", "TG_API_HASH")
    phone = _prompt("Phone number in international format (e.g. +9665xxxxxxxx)", "TG_PHONE")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            try:
                await client.send_code_request(phone)
            except PhoneNumberInvalidError:
                sys.exit("error: Telegram rejected that phone number — check the country code")

            code = input("Login code Telegram just sent you: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                # Two-step verification is enabled on the account.
                from getpass import getpass

                password = getpass("Two-step verification password: ")
                await client.sign_in(password=password)
            except PhoneCodeInvalidError:
                sys.exit("error: that login code is not valid")
            except PhoneCodeExpiredError:
                sys.exit("error: that login code expired — run this again to request a new one")

        me = await client.get_me()
        display = getattr(me, "username", None) or getattr(me, "first_name", "unknown")
        print(f"\nSigned in as: {display}")

        print("\n" + "=" * 72)
        print("TG_SESSION_STRING (store as a GitHub Actions secret, nowhere else):")
        print("=" * 72)
        print(client.session.save())
        print("=" * 72)
        print("\nNext: GitHub repo -> Settings -> Secrets and variables -> Actions")
        print("      -> New repository secret -> name it TG_SESSION_STRING")
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
