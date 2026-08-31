"""Add a collecting account from the dashboard — no phone, no terminal.

Before this, registering a second Telegram account meant running two
scripts on a real computer: ``make_session_string.py`` to log in
interactively, then ``add_account.py`` to store the result. Both steps
are one-time and both need a machine most operators do not carry around.
This module does the same login Telethon flow, but as two HTTP calls the
dashboard drives, so it works from the phone the operator is already on.

Why the pending login lives in memory, not the database
---------------------------------------------------------
Telegram's login is stateful: ``send_code_request`` opens an MTProto
connection and Telegram expects the *next* call — sign-in with the code —
to arrive on that same connection. There is no way to serialise an open
socket into a database row and reconstruct it on the next HTTP request,
so the half-finished login has to sit somewhere in this process's memory
between the "send the code" call and the "here is the code" call.

That is only safe because the whole deployment already assumes a single
web process (see ``app/live.py``'s module docstring) — the same reason a
plain module-level dict is enough here, with no cross-process store to
keep in sync.

An abandoned login — the operator sends a code, then closes the tab — is
cleaned up by TTL (``LOGIN_TTL_SECONDS``) rather than left to leak a
socket forever. Every entry point prunes expired logins before doing
anything else.

What this module deliberately does not do
------------------------------------------
It does not check the caller's identity or workspace membership — that
is the router's job, via ``get_session_user`` and a password
re-confirmation, because a route that can mint a new bearer credential
for a real Telegram account is exactly the kind of action idea 55's
precedent (change-password, TOTP-disable, account-delete) gates on the
current password rather than the session cookie alone. This module
trusts the ``workspace_id`` it is given.

It also never logs the verification code, the 2FA password, or the
resulting session string. The session string is encrypted with
``app/crypto.py`` before it is written anywhere, exactly as
``add_account.py`` and ``scripts/collect.py`` do for every other account.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import encrypt_field
from app.models import TelegramAccount

logger = logging.getLogger("account_login")

# How long an unfinished login is kept alive. Generous relative to how
# long Telegram's own code stays valid, because the operator has to
# switch apps to read it — five minutes is comfortable for someone typing
# on a phone, not just for a script.
LOGIN_TTL_SECONDS = 600.0


class LoginError(Exception):
    """A login step failed for a reason safe to show the operator.

    Every message here is written to be read by the person who typed the
    phone number, not by a developer — no stack traces, no Telethon
    internals, just what to do next.
    """


class NeedsPassword(Exception):  # noqa: N818 - a control-flow signal, not an error to log
    """The code was correct; the account also has two-factor login.

    Raised rather than returned so the router cannot forget to check for
    it — a caller that does not explicitly catch this sees an unhandled
    exception instead of silently treating "needs a password" as success.
    """


@dataclass
class _PendingLogin:
    workspace_id: int
    label: str
    phone: str
    client: Any  # a telethon.TelegramClient; typed loosely so this module
    # imports cleanly even where telethon is not installed (see app/live.py's
    # docstring on the same trade-off).
    created_at: float = field(default_factory=time.monotonic)
    awaiting_password: bool = False


_pending: dict[str, _PendingLogin] = {}


def _client_credentials() -> tuple[int, str] | None:
    """TG_API_ID / TG_API_HASH, or None. Same two variables app/live.py
    reads, and the same reason: read raw from the environment rather than
    through Settings, because they carry no default that would be safe to
    ship."""
    raw_api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not raw_api_id or not api_hash:
        return None
    try:
        return int(raw_api_id), api_hash
    except ValueError:
        return None


async def _prune_expired() -> None:
    now = time.monotonic()
    expired = [token for token, pending in _pending.items() if now - pending.created_at > LOGIN_TTL_SECONDS]
    for token in expired:
        pending = _pending.pop(token, None)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.client.disconnect()


def _check_can_add(db: Session, workspace_id: int, label: str) -> None:
    """Same two checks ``scripts/add_account.py`` makes, run twice here:
    once before bothering Telegram, once more after sign-in succeeds,
    since a second tab or a second operator could have taken the label or
    filled the last slot while this one was mid-login."""
    clash = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.label == label)
        .first()
    )
    if clash is not None:
        raise LoginError(f"يوجد حساب بالتسمية «{label}» في مساحة عملك بالفعل — اختر تسمية أخرى")

    limit = get_settings().max_accounts_per_workspace
    existing = db.query(TelegramAccount).filter(TelegramAccount.workspace_id == workspace_id).count()
    if existing >= limit:
        raise LoginError(f"بلغتَ الحد الأقصى لعدد حسابات الجمع ({limit})")


def _make_client(api_id: int, api_hash: str) -> Any:
    """A module-level seam, so tests can hand back a fake client without
    ever touching a real Telegram connection or importing telethon
    themselves."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(), api_id, api_hash)


async def start_login(db: Session, workspace_id: int, label: str, phone: str) -> str:
    """Ask Telegram to send a login code. Returns a token for step two."""
    await _prune_expired()
    _check_can_add(db, workspace_id, label)

    credentials = _client_credentials()
    if credentials is None:
        raise LoginError(
            "الخادم غير مهيّأ لتسجيل الدخول: TG_API_ID أو TG_API_HASH غير مضبوطين — أضفهما في متغيّرات البيئة أولاً"
        )
    api_id, api_hash = credentials

    from telethon.errors import FloodWaitError, PhoneNumberInvalidError

    client = _make_client(api_id, api_hash)
    try:
        await client.connect()
        await client.send_code_request(phone)
    except PhoneNumberInvalidError as exc:
        await client.disconnect()
        raise LoginError("رقم الهاتف غير صحيح — تأكد من كتابته بالصيغة الدولية، مثل ‎+9665xxxxxxxx") from exc
    except FloodWaitError as exc:
        await client.disconnect()
        raise LoginError(f"تيليجرام يطلب الانتظار {exc.seconds} ثانية قبل محاولة أخرى بهذا الرقم") from exc
    except Exception as exc:  # noqa: BLE001 - any network/library failure becomes one clear message
        await client.disconnect()
        raise LoginError(f"تعذّر الاتصال بتيليجرام: {exc}") from exc

    token = secrets.token_urlsafe(24)
    _pending[token] = _PendingLogin(workspace_id=workspace_id, label=label, phone=phone, client=client)
    return token


async def verify_login(
    db: Session, workspace_id: int, token: str, code: str | None, password: str | None
) -> TelegramAccount:
    """Step two: the code (and, if needed, the 2FA password).

    Raises ``NeedsPassword`` if the account has two-factor login enabled
    and no password was supplied yet — the caller resubmits with the same
    token and only ``password`` filled in, because the code was already
    accepted and does not need to be sent again.
    """
    await _prune_expired()

    pending = _pending.get(token)
    if pending is None or pending.workspace_id != workspace_id:
        raise LoginError("انتهت صلاحية هذه المحاولة أو أنها لا تخصّك — اضغط «إرسال رمز التحقق» من جديد")

    from telethon.errors import (
        PasswordHashInvalidError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        SessionPasswordNeededError,
    )

    client = pending.client
    try:
        if pending.awaiting_password:
            if not password:
                raise NeedsPassword()
            await client.sign_in(password=password)
        else:
            if not code:
                raise LoginError("أدخل رمز التحقق الذي وصلك في تطبيق تيليجرام")
            await client.sign_in(phone=pending.phone, code=code)
    except SessionPasswordNeededError:
        pending.awaiting_password = True
        raise NeedsPassword() from None
    except PhoneCodeInvalidError as exc:
        raise LoginError("رمز التحقق غير صحيح — تحقّق منه وأعد المحاولة") from exc
    except PhoneCodeExpiredError as exc:
        _pending.pop(token, None)
        await client.disconnect()
        raise LoginError("انتهت صلاحية رمز التحقق — اضغط «إرسال رمز التحقق» من جديد") from exc
    except PasswordHashInvalidError as exc:
        raise LoginError("كلمة مرور التحقق بخطوتين غير صحيحة") from exc

    # Signed in. Recheck the label/limit before writing: time has passed
    # since start_login, and another tab or operator could have taken
    # either while this login was in flight.
    try:
        _check_can_add(db, workspace_id, pending.label)
    except LoginError:
        _pending.pop(token, None)
        await client.disconnect()
        raise

    session_string = client.session.save()
    await client.disconnect()
    _pending.pop(token, None)

    account = TelegramAccount(
        workspace_id=workspace_id, label=pending.label, session_string=encrypt_field(session_string)
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info("account_login: added account %s (%s) to workspace %s", account.id, account.label, workspace_id)
    return account
