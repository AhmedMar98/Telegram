"""Source identity: one spelling for a Telegram peer, in one place.

``app.dialogs`` has owned this rule since discovery was written, and it
still exports it — every existing caller keeps working. It lives here now
because ``app.models`` needs it too, and ``app.dialogs`` imports
``app.models``: a leaf module with no project imports is what lets both
sides use one rule instead of two that drift.

The rule itself is unchanged, and its edge case is the interesting part.
The ``-100`` prefix is stripped **only from negative ids**, because the
minus sign is what marks the prefix as a peer-type tag rather than digits
of the number. Stripping a leading ``100`` from positive ids as well is
the plausible-looking version that is wrong: a channel genuinely numbered
``1001234`` would canonicalise to ``1234`` from the dashboard and
``1001234`` from Telethon, and would silently never match.
"""

from __future__ import annotations


def canonical_id(raw: object) -> str | None:
    """One spelling for a Telegram peer id, whichever form it arrived in.

    Returns ``None`` for anything that is not an integer id — a username,
    an empty string, ``None``. Callers treat that as "no numeric identity
    here", not as an error.
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


def source_identity_key(tg_channel_id: str | None) -> str | None:
    """The comparable identity for a ``channels`` row.

    Two kinds of row live in that table and both have identities; they
    just come from different namespaces:

    - A real dialog: the canonical peer id, so ``-1001234567890`` and
      ``1234567890`` resolve to one source.
    - A synthetic row (``manual``, ``import:2026-01-02``): its own id,
      unchanged. These stand for a bucket rather than a Telegram peer, and
      a bucket is still a thing a link can belong to.

    Returning the raw value for the second case rather than ``None`` is
    what keeps the column meaningful for every row, so "identity is
    unknown" stays distinguishable from "identity is not a peer id".
    """
    if tg_channel_id is None:
        return None
    raw = tg_channel_id.strip()
    if not raw:
        return None
    return canonical_id(raw) or raw
