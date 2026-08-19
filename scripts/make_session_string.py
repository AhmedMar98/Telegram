"""One-time interactive helper: log in once, print a Telethon StringSession.

Run this locally (never in CI, never on Render):

    python scripts/make_session_string.py

It asks for your phone number and the login code Telegram sends you, then
prints a session string. Paste that string into the TG_SESSION_STRING
GitHub Actions secret and never write it to a file in the repository —
it is equivalent to your Telegram account password.
"""

from __future__ import annotations

import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(os.environ.get("TG_API_ID") or input("TG_API_ID (from https://my.telegram.org): "))
api_hash = os.environ.get("TG_API_HASH") or input("TG_API_HASH: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()
    print("\nSave this as the TG_SESSION_STRING GitHub secret (do not print/share it elsewhere):\n")
    print(session_string)
