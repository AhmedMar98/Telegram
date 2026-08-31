"""What counts as a collectable dialog, and how one becomes a row.

Before this module, a row in ``channels`` had exactly one origin: somebody
typed it into the dashboard. That made "collect from Telegram" mean
"collect from the handful of channels you remembered to register", while
the groups and private conversations that carry most of a real account's
links were unreachable by construction.

Discovery closes that gap, and it needs three things that neither the
scheduled collector nor the live listener should own alone — both of them
answer the same questions, and two answers that drift are worse than one:

1. **What kind of dialog is this?** Telegram's own types do not line up
   with how a reader thinks about them: a broadcast channel and a
   megagroup are both ``Channel`` in the API, and a basic group is a
   different type entirely.
2. **Is this kind in scope?** Configured per deployment, because
   collecting private conversations is a real decision with a real
   privacy consequence, not a default to stumble into.
3. **Do we already have a row for it?** This is the subtle one. The same
   dialog can be spelled ``-1001234567890`` by Telethon and
   ``1234567890`` by whoever typed it in, and a naive uniqueness check on
   the raw string would happily create a second row for a channel already
   being collected — splitting its watermark and its links in two.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import Channel

logger = logging.getLogger(__name__)

# The three kinds a row can stand for. "channel" is a broadcast channel,
# "group" covers megagroups and basic groups alike (they differ in the API
# and not at all in what collection does with them), "private" is a
# one-to-one conversation, including with a bot.
KINDS = ("channel", "group", "private")
DEFAULT_KIND = "channel"


def parse_scope(raw: str | None) -> frozenset[str]:
    """Which kinds discovery may register.

    Accepts ``all``, or any comma-separated subset of the three kinds.
    Unknown words are ignored rather than fatal: a typo in one entry of a
    list should narrow the scope, not stop the collector from running at
    all — and the run logs what it actually used.
    """
    if raw is None:
        return frozenset(KINDS)
    text = raw.strip().lower()
    if not text or text == "all":
        return frozenset(KINDS)
    chosen = {word.strip() for word in text.split(",")}
    kinds = frozenset(word for word in chosen if word in KINDS)
    unknown = sorted(chosen - set(KINDS) - {""})
    if unknown:
        logger.warning("ignoring unknown collector scope entries: %s", ", ".join(unknown))
    if not kinds:
        logger.warning("collector scope %r names no known kind; falling back to channels only", raw)
        return frozenset({DEFAULT_KIND})
    return kinds


def dialog_kind(obj: Any) -> str | None:
    """Classify a Telethon dialog (or a bare entity) into one of KINDS.

    Telethon's ``Dialog`` already exposes ``is_user``/``is_group``/
    ``is_channel``, and those are preferred when present because they
    encode the library's own knowledge of the peer. The duck-typed
    fallback below is for bare entities — what ``get_entity`` and an
    update's ``chat`` return — where those helpers do not exist.

    Order is load-bearing in the fallback: a Telethon ``Channel`` carries
    **both** ``broadcast`` and ``megagroup``, and a megagroup is a group
    to every reader even though the API files it under channels. Testing
    ``title`` first (the obvious-looking version of this function) would
    label every megagroup a channel.
    """
    if obj is None:
        return None

    if getattr(obj, "is_user", False):
        return "private"
    if getattr(obj, "is_group", False):
        return "group"
    if getattr(obj, "is_channel", False):
        return "channel"

    entity = getattr(obj, "entity", None) or obj

    if hasattr(entity, "broadcast") or hasattr(entity, "megagroup"):
        if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
            return "group"
        return "channel"
    if hasattr(entity, "first_name") or hasattr(entity, "bot") or hasattr(entity, "phone"):
        return "private"
    if hasattr(entity, "title"):
        return "group"
    return None


def _display_name(entity: Any) -> str | None:
    """A human label for the dialog, whatever type it turns out to be."""
    title = getattr(entity, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()[:300]
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    if name:
        return name[:300]
    handle = getattr(entity, "username", None)
    if isinstance(handle, str) and handle.strip():
        return handle.strip()[:300]
    return None


def dialog_identity(obj: Any) -> tuple[str | None, str | None, str | None]:
    """``(id, username, title)`` for a dialog or entity, as strings.

    The id preferred is the *marked* peer id (``-100…`` for channels)
    because that is the form ``client.get_entity`` accepts back, and the
    form a person pastes out of a Telegram link. ``canonical_id`` is what
    makes the two spellings compare equal, so storing either is safe —
    storing the one Telethon hands back is merely the least surprising.
    """
    entity = getattr(obj, "entity", None) or obj

    raw_id = getattr(obj, "id", None)
    if raw_id is None:
        raw_id = getattr(entity, "id", None)
    tg_id = str(raw_id) if raw_id is not None else None

    handle = getattr(entity, "username", None)
    username = handle.strip().lstrip("@") if isinstance(handle, str) and handle.strip() else None

    return tg_id, username, _display_name(entity)


# --- one spelling for an identity ------------------------------------------


def canonical_id(raw: object) -> str | None:
    """One spelling for a Telegram peer id, whichever form it arrived in.

    Telethon reports a channel as ``-1001234567890``; an operator typing
    the id into the dashboard usually pastes ``1234567890``. Both have to
    match the same row.

    The ``-100`` prefix is stripped **only from negative ids**, because
    that minus sign is the marker that the prefix is a peer-type tag
    rather than part of the number. Stripping a leading ``100`` from
    positive ids too would be the plausible-looking version of this
    function that is wrong: a channel genuinely numbered ``1001234``
    would canonicalise to ``1234`` from the dashboard and to ``1001234``
    from Telethon, and would silently never match.
    """
    try:
        text = str(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if text.startswith("-100"):
        return text[4:]
    if text.startswith("-"):
        return text[1:]
    return text


def canonical_username(raw: object) -> str | None:
    """One spelling for a @handle. Telegram handles are case-insensitive."""
    if not isinstance(raw, str):
        return None
    handle = raw.strip().lstrip("@").lower()
    return handle or None


def index_channels(channels: list[Channel]) -> dict[str, Channel]:
    """Every name a row answers to -> the row.

    Both identities are indexed for every row, because which one an
    incoming dialog or event carries is not ours to decide: a public
    channel usually resolves a username, a private chat never does.
    """
    index: dict[str, Channel] = {}
    for channel in channels:
        key = canonical_id(channel.tg_channel_id)
        if key:
            index[f"id:{key}"] = channel
        handle = canonical_username(channel.username)
        if handle:
            index[f"@{handle}"] = channel
    return index


def lookup_channel(index: dict[str, Channel], tg_id: object, username: object) -> Channel | None:
    key = canonical_id(tg_id)
    if key and f"id:{key}" in index:
        return index[f"id:{key}"]
    handle = canonical_username(username)
    if handle:
        return index.get(f"@{handle}")
    return None


def existing_channel(db: Session, workspace_id: int, *, tg_id: object, username: object) -> Channel | None:
    """The row already standing for this dialog, under any spelling.

    Deliberately compares canonicalised forms in Python rather than with a
    WHERE clause: the stored value is free text that may be either
    spelling, so no single equality test in SQL finds both. At the scale
    this runs on — the dialogs of one account — reading the workspace's
    rows costs less than the bug of a second row for a dialog already
    being collected, which would split its watermark and its links.
    """
    rows = db.query(Channel).filter(Channel.workspace_id == workspace_id).all()
    return lookup_channel(index_channels(rows), tg_id, username)


def register_dialog(
    db: Session,
    *,
    workspace_id: int,
    account_id: int | None,
    kind: str,
    tg_id: str,
    username: str | None,
    title: str | None,
    existing: Channel | None = None,
) -> tuple[Channel, bool]:
    """Ensure a row stands for this dialog. Returns ``(row, created)``.

    An existing row is refreshed rather than duplicated, and refreshed
    narrowly: title and username move (channels get renamed, handles get
    claimed), ``kind`` fills in for rows that predate it, and everything
    else — the watermark, whether it is active, which account collects it
    — is left exactly as it was. Discovery must never quietly re-enable a
    dialog somebody switched off, which is why ``is_active`` is not in
    that list.
    """
    row = existing if existing is not None else existing_channel(db, workspace_id, tg_id=tg_id, username=username)
    if row is not None:
        if title and row.title != title:
            row.title = title
        if username and row.username != username:
            row.username = username
        if row.kind != kind and row.kind in (None, "", DEFAULT_KIND):
            row.kind = kind
        return row, False

    row = Channel(
        workspace_id=workspace_id,
        account_id=account_id,
        tg_channel_id=tg_id,
        username=username,
        title=title,
        kind=kind,
    )
    db.add(row)
    db.flush()
    return row, True
