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

from app import access, assignments
from app.identity import canonical_id
from app.models import Channel, SourceAccess
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

# The four kinds a row can stand for. "channel" is a broadcast channel,
# "group" covers megagroups and basic groups alike (they differ in the API
# and not at all in what collection does with them), "private" is a
# one-to-one conversation with a person, and "bot" is one with a bot.
#
# "bot" was said to be impossible to separate, on the grounds that Telegram
# models a bot chat as a User like any other. That is true of the *type*
# and false of the *entity*: a Telethon ``User`` carries a ``bot`` boolean,
# and the fallback below was already testing for the attribute's existence
# before throwing the answer away. Separating them matters because a bot
# chat is usually a feed — links arrive from it by the hundred — while a
# private chat is a conversation with a person, and lumping the two makes
# the privacy-sensitive setting and the high-volume one the same switch.
KINDS = ("channel", "group", "private", "bot")
DEFAULT_KIND = "channel"

# Who reads a row. Named here rather than spelled as string literals at
# the four call sites, because a typo in one of them does not fail — it
# silently produces a row nobody ever reads.
SOURCE_USERBOT = "userbot"
SOURCE_PUBLIC = "public"
SOURCES = (SOURCE_USERBOT, SOURCE_PUBLIC)

# Channel rows that stand for something other than a Telegram dialog: the
# bucket for hand-added links, and one per import file. They are ordinary
# rows carrying real links — which is why they exist at all, so the
# (channel_id, url_hash) dedupe key keeps working for links with no
# channel — and they are collectable by nobody. Asking Telegram to resolve
# "manual" fails every time, which the collector had been logging as a
# warning on every run, for every such row, forever.
MANUAL_CHANNEL_ID = "manual"
IMPORT_CHANNEL_PREFIX = "import:"


def is_synthetic(tg_channel_id: str) -> bool:
    """Whether this row stands for a real Telegram dialog at all."""
    return tg_channel_id == MANUAL_CHANNEL_ID or tg_channel_id.startswith(IMPORT_CHANNEL_PREFIX)


# What "nothing said" means. Channels and groups are what a link-collection
# tool is for; private conversations and bot feeds are separate decisions
# with separate consequences (privacy, and volume) and have to be asked for.
DEFAULT_SCOPE = frozenset({"channel", "group"})


def parse_scope(raw: str | None) -> frozenset[str]:
    """Which kinds discovery may register.

    Accepts ``all``, or any comma-separated subset of the four kinds.
    Unknown words are ignored rather than fatal: a typo in one entry of a
    list should narrow the scope, not stop the collector from running at
    all — and the run logs what it actually used.

    **Absent or empty means the default scope, not everything.** It used to
    mean everything, so ``COLLECTOR_SCOPE=`` — an env var present but
    blank, which is what an unset repository variable expands to in GitHub
    Actions — silently turned on collection of private conversations. A
    setting that widens itself when nobody has set it is the wrong
    direction to fail in.
    """
    if raw is None:
        return DEFAULT_SCOPE
    text = raw.strip().lower()
    if not text:
        return DEFAULT_SCOPE
    if text == "all":
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


def _user_kind(entity: Any) -> str:
    """A user dialog is a bot chat or a person's chat — never "a user".

    Split out so both paths through ``dialog_kind`` answer it identically.
    Defaults to "private" when the flag is absent, because a peer we cannot
    prove is a bot must be treated with the more cautious of the two
    settings, not the more permissive one.
    """
    return "bot" if getattr(entity, "bot", False) else "private"


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
        return _user_kind(getattr(obj, "entity", None) or obj)
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
        return _user_kind(entity)
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


# ``canonical_id`` is imported at the top of this module rather than
# defined here. It moved to app/identity.py because ``app.models`` needs
# the same rule for the identity_key column and cannot import this module
# — this one imports it. Callers keep saying ``dialogs.canonical_id``;
# there is still exactly one definition of the rule.


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


# Which stored kinds a freshly observed kind is allowed to overwrite.
#
# The rule used to be "only overwrite the default", which was right while
# there were three kinds and wrong the moment a fourth arrived: every bot
# chat already on disk is stored as "private", so a new "bot" classifier
# would have applied to new rows only and left the entire existing archive
# misfiled — a feature that looks like it works precisely because you
# check it on something you just added.
#
# Narrow on purpose. "private" may become "bot" because that is strictly
# more specific about the same peer; nothing may become "private", because
# losing the distinction is not a refinement.
_KIND_UPGRADES: dict[str, frozenset[str]] = {
    DEFAULT_KIND: frozenset({"group", "private", "bot"}),
    "": frozenset({"group", "private", "bot"}),
    "private": frozenset({"bot"}),
}


def _may_upgrade_kind(stored: str | None, observed: str) -> bool:
    if stored == observed:
        return False
    return observed in _KIND_UPGRADES.get(stored or "", frozenset())


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
        if _may_upgrade_kind(row.kind, kind):
            row.kind = kind
        # Seeing a dialog in an account's list is evidence that account can
        # read it — the strongest kind this system gets without collecting.
        # Recorded even for a row that already exists, and even when
        # another account collects it: knowing who *else* can reach a
        # source is what makes failover possible before it is needed.
        _record_discovery_access(db, row, account_id)
        return row, False

    # Created unassigned, then assigned. ``account_id`` is a mirror of
    # ``source_assignments`` and carrying one on INSERT would be an
    # assignment made by writing the mirror — which the database refuses,
    # and should.
    row = Channel(
        workspace_id=workspace_id,
        tg_channel_id=tg_id,
        username=username,
        title=title,
        kind=kind,
    )
    db.add(row)
    db.flush()
    _record_discovery_access(db, row, account_id)
    if account_id is not None:
        assignments.assign(db, row, account_id, reason="discovered by this account")
    return row, True


def _record_discovery_access(db: Session, row: Channel, account_id: int | None) -> None:
    """Note that this account could see this dialog, with the reason why."""
    if account_id is None:
        return
    access.record(
        db,
        row,
        SourceAccess.ACCESSIBLE,
        account_id=account_id,
        observed_at=utcnow(),
        evidence_kind="dialog_discovery",
        evidence_summary="the dialog appeared in this account's own dialog list",
    )
