"""Scheduled Telegram collector.

Run as a one-shot script by .github/workflows/collector.yml on a cron
schedule — deliberately **not** a long-running process, because Render
has no free tier for a persistent background worker (see
docs/01-critical-analysis.md, Appendix C). GitHub Actions minutes are
free for public repos and budgeted (2,000 min/month) for private ones,
which comfortably covers a job that runs for well under a minute per
hour.

Required environment (set as GitHub Actions secrets):
  TG_API_ID               - from https://my.telegram.org
  TG_API_HASH             - from https://my.telegram.org
  TG_SESSION_STRING       - a Telethon StringSession for the collecting
                            account (generate once locally with
                            scripts/make_session_string.py, store only
                            in GitHub Secrets, never in the repo)
  DATABASE_URL            - the same Render Postgres URL the web service uses
  COLLECTOR_WORKSPACE_ID  - the workspace this collector feeds (see README)
  FIELD_ENCRYPTION_KEY    - encrypts the session string before it is stored
                            in TelegramAccount (app/crypto.py). Must match
                            whatever value any future consumer of that row
                            uses to decrypt it; changing it strands
                            previously-stored rows. Generate with:
                            python -c "from cryptography.fernet import \
                            Fernet; print(Fernet.generate_key().decode())"

Optional:
  COLLECTOR_MESSAGE_LIMIT - messages scanned per channel per run (default 200)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.errors import FloodWaitError, RPCError  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from app import coverage  # noqa: E402
from app.accounts import record_failure, record_success  # noqa: E402
from app.alerts import COLLECTOR_FAILED  # noqa: E402
from app.assignment import apply_assignments  # noqa: E402
from app.audit import record as audit_record  # noqa: E402
from app.config import get_settings, require_real_secrets  # noqa: E402
from app.crypto import InvalidToken, decrypt_field, encrypt_field  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.dialogs import (
    IMPORT_CHANNEL_PREFIX,
    MANUAL_CHANNEL_ID,
    SOURCE_USERBOT,
    dialog_identity,
    dialog_kind,
    index_channels,
    lookup_channel,
    parse_scope,
    register_dialog,
)
from app.ingest import MAX_LINKS_PER_MESSAGE, IngestSummary, ingest_text
from app.leads import active_rules as active_keyword_rules  # noqa: E402
from app.models import Channel, TelegramAccount  # noqa: E402
from app.notify import raise_alert, report_adult_links  # noqa: E402
from app.rls import scope_session_to_workspace  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

logger = logging.getLogger("collector")

DEFAULT_MESSAGE_LIMIT = 200

# Telegram's rate limits are per account, so this bound is per account too.
DEFAULT_MAX_CHANNELS_PER_ACCOUNT = 20


def _message_limit() -> int:
    """Read the per-channel scan limit at call time, not import time."""
    try:
        return max(1, int(os.environ.get("COLLECTOR_MESSAGE_LIMIT", DEFAULT_MESSAGE_LIMIT)))
    except ValueError:
        logger.warning("invalid COLLECTOR_MESSAGE_LIMIT, falling back to %d", DEFAULT_MESSAGE_LIMIT)
        return DEFAULT_MESSAGE_LIMIT


def _entity_ref(channel: Channel) -> str | int:
    """Resolve how to address a channel, preferring its @username.

    ``tg_channel_id`` is free text on the model because a user may type
    either a numeric id or a handle, so the int() conversion has to be
    guarded: an unconvertible value is a bad row, not a reason to abort
    the entire collection run.
    """
    if channel.username:
        return channel.username
    return int(channel.tg_channel_id)


def _button_urls(message: object) -> list[str]:
    """URL targets on a message's inline keyboard.

    Telegram bots routinely post the actual download link on a button and
    leave the message body as pure marketing copy, so a collector that only
    reads ``raw_text`` misses precisely the link the post exists to share.

    ``reply_markup`` is absent on most messages and its shape varies by
    button type (URL buttons carry ``.url``; callback and switch-inline
    buttons do not), so every access is guarded rather than assumed.
    """
    markup = getattr(message, "reply_markup", None)
    urls: list[str] = []
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
    return urls


def _forward_origin(message: object) -> str | None:
    """Human-readable name of where a forwarded message came from.

    Telethon exposes the origin through several mutually exclusive fields
    depending on whether the source was a channel, a user, or a sender who
    hid their account, so each is tried in turn and a missing origin simply
    means the message was not forwarded.
    """
    forward = getattr(message, "forward", None)
    if forward is None:
        return None
    name = getattr(forward, "from_name", None)
    if isinstance(name, str) and name:
        return name
    chat = getattr(forward, "chat", None) or getattr(forward, "sender", None)
    for attribute in ("title", "username", "first_name"):
        value = getattr(chat, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def _still_owned_by(db: Session, channel: Channel, account_id: int | None, *, is_default: bool) -> bool:
    """Whether this account still owns the channel it has just finished reading.

    Ownership can change *under* a run: ``apply_assignments`` commits from
    another process every time an operator presses rebalance in the
    dashboard. Writing a watermark for a channel this account no longer
    owns advances it past messages the **new** owner has not read — and
    since the new owner starts from ``min_id=last_message_id``, those
    messages are never collected by anyone. A permanent gap, invisible in
    every counter, which is exactly what §44.7 defines collection success
    against.

    Read fresh rather than trusting the in-session row: the whole point is
    to see a change another process committed. ``None`` still passes when
    this is the default account, because an unassigned channel legitimately
    falls to it (see ``_channels_for``).
    """
    owner = db.query(Channel.account_id).filter(Channel.id == channel.id).scalar()
    if owner is None:
        return is_default
    return owner == account_id


def _classify_failure(exc: BaseException) -> str:
    """Which of ``app.coverage.FAILURE_KINDS`` this failure is.

    Classified by what an operator would *do* about it, not by exception
    class: a revoked session and a channel that removed the account both
    surface as Telethon errors and need opposite responses. Anything
    unrecognised becomes ``unknown`` — a named bucket that shows up in the
    report, rather than being folded into a neighbour it does not belong to.
    """
    from telethon.errors import ChannelPrivateError, FloodWaitError

    if isinstance(exc, FloodWaitError):
        return coverage.RATE_LIMITED
    if isinstance(exc, ChannelPrivateError):
        return coverage.ACCESS_DENIED
    if isinstance(exc, ValueError | TypeError):
        # Telethon raises ValueError for an entity it cannot resolve at
        # all: renamed, deleted, or never visible to this account.
        return coverage.SOURCE_UNAVAILABLE
    if isinstance(exc, OSError | ConnectionError | TimeoutError):
        return coverage.NETWORK_ERROR
    if isinstance(exc, RPCError):
        return coverage.TELEGRAM_ERROR
    if isinstance(exc, SQLAlchemyError):
        return coverage.DATABASE_ERROR
    return coverage.UNKNOWN


def _record_outcome(
    db: Session, channel: Channel, outcome: str, *, kind: str | None = None, commit: bool = True
) -> None:
    """Stamp what happened to one source, for the measurement contract.

    Separate from ``last_collected_at``, which means "last *successful*
    read" and is what the rotation ordering needs. Without this a source
    nobody attempted and a source that failed are indistinguishable — and
    coverage cannot be computed from an indistinguishable pair.
    """
    channel.last_attempt_at = utcnow()
    channel.last_outcome = outcome
    channel.last_failure_kind = kind
    if commit:
        db.commit()


async def _collect_channel(
    client: TelegramClient,
    db: Session,
    channel: Channel,
    run: IngestSummary | None = None,
    keyword_rules: list | None = None,
    *,
    account_id: int | None = None,
    is_default: bool = True,
) -> int:
    label = channel.username or channel.tg_channel_id
    try:
        entity = await client.get_entity(_entity_ref(channel))
    except (ValueError, TypeError, RPCError) as exc:
        logger.warning("skipping channel %s (%s): %s", channel.id, label, exc)
        _record_outcome(db, channel, coverage.FAILED, kind=_classify_failure(exc))
        return 0

    new_watermark = channel.last_message_id
    summary = IngestSummary()
    rate_limited = False
    scanned = 0

    # reverse=True walks *forward* from the watermark (oldest unseen first).
    # Telethon's default is newest-first, which combined with a limit would
    # advance the watermark past messages that were never scanned, silently
    # dropping them on a busy channel. Ascending order keeps the watermark
    # contiguous, so a capped run simply resumes where it stopped next time.
    try:
        async for message in client.iter_messages(
            entity, min_id=channel.last_message_id, reverse=True, limit=_message_limit()
        ):
            ingest_text(
                db,
                workspace_id=channel.workspace_id,
                channel_id=channel.id,
                text=message.raw_text or "",
                message_id=message.id,
                posted_at=message.date.replace(tzinfo=None) if message.date else None,
                button_urls=_button_urls(message),
                forwarded_from=_forward_origin(message),
                # Read off the message, never fetched. get_sender() is a
                # round trip per message, and paying one to attribute a
                # lead that may never match would multiply this run's
                # Telegram traffic by the number of messages it reads —
                # which is exactly what the pacing budget exists to hold
                # down.
                sender_id=str(message.sender_id) if getattr(message, "sender_id", None) else None,
                # Evidence, not decoration: a channel called "أفلام" is a
                # real signal about a bare URL posted in it, and this is
                # the one place that already holds the row.
                channel_title=channel.title,
                keyword_rules=keyword_rules,
                summary=summary,
            )
            new_watermark = max(new_watermark, message.id)
            scanned += 1
    except FloodWaitError as exc:
        # Keep whatever was collected before the rate limit and resume from
        # the contiguous watermark on the next scheduled run.
        logger.warning("flood wait on channel %s (%s): %s", channel.id, label, exc)
        rate_limited = True

    if account_id is not None and not _still_owned_by(db, channel, account_id, is_default=is_default):
        # Reassigned mid-run. Everything already stored stays stored — the
        # links are committed per message and are not the new owner's to
        # collect again — but the watermark is the new owner's to move.
        _record_outcome(db, channel, coverage.FAILED, kind=coverage.ASSIGNMENT_ERROR)
        logger.warning(
            "channel %s (%s) changed owner during the run; leaving the watermark at %d for the new owner",
            channel.id,
            label,
            channel.last_message_id,
        )
        return summary.stored

    if new_watermark < channel.last_message_id:
        # Never observed, and counted rather than silently corrected: a
        # watermark moving backwards means messages between the two values
        # are about to be re-read forever, and §46 reports the count so it
        # cannot be a zero nobody checked.
        channel.watermark_regressions = (channel.watermark_regressions or 0) + 1
        logger.error(
            "channel %s (%s): refusing a watermark regression %d -> %d",
            channel.id,
            label,
            channel.last_message_id,
            new_watermark,
        )
        new_watermark = channel.last_message_id

    # Whether the window was finished. Hitting the per-run cap means a
    # backlog remains — not an error, an unfinished window that the next
    # run continues, and the difference is what "behind" means in §46.
    channel.caught_up = scanned < _message_limit()
    channel.last_message_id = new_watermark
    # Stamped even when the run found nothing: the question this answers is
    # "when did anything last look at this dialog", which is what the
    # rotation ordering in _channels_for needs. Stamping only on a
    # non-empty run would park every quiet dialog permanently at the front
    # of the queue and starve the rest.
    channel.last_collected_at = utcnow()
    _record_outcome(
        db,
        channel,
        coverage.FAILED if rate_limited else coverage.SUCCEEDED,
        kind=coverage.RATE_LIMITED if rate_limited else None,
        commit=False,
    )
    audit_record(
        db,
        workspace_id=channel.workspace_id,
        user_id=None,
        action="collector.run",
        target_type="channel",
        target_id=str(channel.id),
        detail=f"{summary.stored} new link(s)",
    )
    db.commit()
    if run is not None:
        # After the commit, not before: these URLs are only real once the
        # transaction that stored them succeeded.
        run.adult_urls.extend(summary.adult_urls)
    logger.info(
        "channel %s (%s): %d new link(s), %d duplicate(s), watermark -> %d",
        channel.id,
        label,
        summary.stored,
        summary.duplicates,
        new_watermark,
    )
    if summary.truncated_messages:
        logger.warning(
            "channel %s (%s): %d message(s) exceeded the %d-link cap, %d link(s) not stored",
            channel.id,
            label,
            summary.truncated_messages,
            MAX_LINKS_PER_MESSAGE,
            summary.dropped_links,
        )
    return summary.stored


def _ensure_primary_account(db: Session, workspace_id: int, session_string: str) -> None:
    """Bootstrap the workspace's first account from the environment.

    Only the *first* account comes from ``TG_SESSION_STRING``; further
    accounts are registered with scripts/add_account.py, which is why this
    is a no-op once any account exists.
    """
    exists = db.query(TelegramAccount).filter(TelegramAccount.workspace_id == workspace_id).first()
    if exists is None:
        db.add(
            TelegramAccount(
                workspace_id=workspace_id, label="primary", session_string=encrypt_field(session_string)
            )
        )
        db.commit()


def _max_channels_per_account() -> int:
    """Cap on channels one account handles per run, read at call time.

    Telegram rate-limits per *account*, not per workspace, so a workspace
    that assigns forty channels to one account is asking that one account
    to trip a FloodWait — which costs the whole run, not just that channel.
    Spreading the work is the point of multiple accounts; this is the limit
    that makes the spread matter.

    Channels beyond the cap are not dropped: ordering is by id, and each
    channel keeps its own watermark, so the ones skipped this run are
    picked up next run from exactly where they were left.
    """
    try:
        return max(1, int(os.environ.get("COLLECTOR_MAX_CHANNELS_PER_ACCOUNT", DEFAULT_MAX_CHANNELS_PER_ACCOUNT)))
    except ValueError:
        logger.warning(
            "invalid COLLECTOR_MAX_CHANNELS_PER_ACCOUNT, falling back to %d", DEFAULT_MAX_CHANNELS_PER_ACCOUNT
        )
        return DEFAULT_MAX_CHANNELS_PER_ACCOUNT


class Pacer:
    """Randomised pauses between dialogs, spent from a fixed budget.

    Two independent jobs, and separating them is the whole design:

    * **Look less mechanical.** A constant gap between requests is as much
      a signature as no gap at all, so each pause is drawn from a range.
    * **Never cause the outage it prevents.** Up to 4s x 20 dialogs x 10
      accounts is 800 seconds added to an hourly job on runners that are
      already late under load. So the pauses are drawn from a budget, and
      when the budget is gone the collector stops pausing and finishes its
      work. Slowing down is a defence; timing out is not.

    ``sleep`` and ``random`` are injected so a test can prove the budget is
    honoured without spending real seconds proving it.
    """

    def __init__(
        self,
        *,
        minimum: float,
        maximum: float,
        budget: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._min = max(0.0, minimum)
        self._max = max(self._min, maximum)
        self._remaining = max(0.0, budget)
        self._sleep = sleep
        self._jitter = jitter
        self.spent = 0.0
        self.paused = 0

    @property
    def exhausted(self) -> bool:
        return self._remaining <= 0

    async def wait(self) -> None:
        """Pause once, if there is budget for it. Never raises."""
        if self._max <= 0 or self.exhausted:
            return
        # Clamped to what is left, so the budget is a real ceiling rather
        # than one it can overshoot by a final full-length pause.
        delay = min(self._jitter(self._min, self._max), self._remaining)
        if delay <= 0:
            return
        self._remaining -= delay
        self.spent += delay
        self.paused += 1
        await self._sleep(delay)


def _pacer() -> Pacer:
    settings = get_settings()
    return Pacer(
        minimum=settings.collector_pace_min_seconds,
        maximum=settings.collector_pace_max_seconds,
        budget=settings.collector_pace_budget_seconds,
    )


def _channels_for(db: Session, workspace_id: int, account: TelegramAccount, *, is_default: bool) -> list[Channel]:
    """Which channels this account is responsible for.

    A channel names its collecting account through ``account_id``. Channels
    that name nobody fall to the default account, so a single-account
    workspace keeps working exactly as before without anyone having to
    assign anything.
    """
    query = db.query(Channel).filter(
        Channel.workspace_id == workspace_id,
        Channel.is_active.is_(True),
        # Rows that stand for no Telegram dialog: the manual-entry bucket
        # and one per import file. get_entity("manual") cannot succeed, so
        # every run spent a round trip failing on each of them and logged a
        # warning that meant nothing.
        Channel.tg_channel_id != MANUAL_CHANNEL_ID,
        Channel.tg_channel_id.notlike(f"{IMPORT_CHANNEL_PREFIX}%"),
        # Rows the public scraper owns are excluded here and only here.
        # They carry their own watermark and the scraper advances it; a
        # userbot reading the same row would move that watermark past
        # messages the scraper has not fetched, and they would never be
        # collected by either reader.
        Channel.source == SOURCE_USERBOT,
    )
    limit = _max_channels_per_account()
    if is_default:
        query = query.filter((Channel.account_id == account.id) | (Channel.account_id.is_(None)))
    else:
        query = query.filter(Channel.account_id == account.id)
    # Least recently collected first, never-collected before everything.
    #
    # This used to order by id and take a stable prefix, which was fine
    # while every row was hand-added and there were a dozen of them. With
    # automatic discovery an account can hold hundreds of dialogs, and a
    # fixed prefix means the rows past the cap are never read *at all* —
    # not "later", never. Ordering by age of last collection turns the cap
    # into a rotation: every dialog gets its turn, and each keeps its own
    # watermark so a turn resumes rather than skips.
    #
    # Sorting on a boolean expression rather than NULLS FIRST on purpose:
    # both engines this project runs on order it the same way, which the
    # NULLS syntax cannot claim across SQLite versions.
    return (
        query.order_by(
            Channel.last_collected_at.is_(None).desc(),
            Channel.last_collected_at.asc(),
            Channel.id,
        )
        .limit(limit)
        .all()
    )


def _discovery_settings() -> tuple[bool, frozenset[str], int]:
    """``(enabled, kinds, cap)`` for this run, read at call time.

    Read here rather than at import so a deployment can change the scope
    by changing an environment variable, without a code change and
    without a stale value captured when the module was first imported.
    """
    settings = get_settings()
    return (
        settings.collector_auto_discover,
        parse_scope(settings.collector_scope),
        max(1, settings.collector_max_dialogs),
    )


async def _discover_dialogs(
    client: TelegramClient, db: Session, workspace_id: int, account: TelegramAccount
) -> int:
    """Register the account's dialogs that this workspace does not know yet.

    This is what makes "collect from Telegram" mean the account's actual
    Telegram — channels, groups and private conversations — rather than
    the subset somebody remembered to type into the dashboard.

    Three properties worth stating because each one is a bug if absent:

    - **Incremental.** A dialog already known is refreshed, never
      duplicated, and matching is on the canonicalised id/handle so the
      two spellings of the same channel cannot become two rows with two
      watermarks (see ``app.dialogs.existing_channel``).
    - **Bounded.** ``collector_max_dialogs`` caps one pass. An account with
      a thousand conversations registers the first N now and the rest on
      later runs, instead of turning the first run into an hour of
      ``iter_dialogs`` and a FloodWait.
    - **Never fatal.** Discovery failing is not collection failing. The
      channels already registered are still collectable, so a failure here
      is logged and the run continues rather than losing everything to a
      feature that is an addition.
    """
    enabled, kinds, cap = _discovery_settings()
    if not enabled:
        return 0

    known = index_channels(db.query(Channel).filter(Channel.workspace_id == workspace_id).all())
    created = 0
    seen = 0
    try:
        async for dialog in client.iter_dialogs():
            if seen >= cap:
                logger.info(
                    "account %s (%s): discovery stopped at the %d-dialog cap; the rest follow next run",
                    account.id,
                    account.label,
                    cap,
                )
                break
            seen += 1

            kind = dialog_kind(dialog)
            if kind is None or kind not in kinds:
                continue

            tg_id, username, title = dialog_identity(dialog)
            if not tg_id:
                continue

            row, is_new = register_dialog(
                db,
                workspace_id=workspace_id,
                account_id=account.id,
                kind=kind,
                tg_id=tg_id,
                username=username,
                title=title,
                existing=lookup_channel(known, tg_id, username),
            )
            if is_new:
                created += 1
                # Index the row as soon as it exists, so a second dialog
                # resolving to the same peer later in this same pass finds
                # it instead of inserting a duplicate.
                known.update(index_channels([row]))
    except Exception as exc:  # noqa: BLE001 - discovery is additive; collection still runs
        logger.warning("account %s (%s): dialog discovery failed: %s", account.id, account.label, exc)
        db.rollback()
        return 0

    db.commit()
    if created:
        logger.info(
            "account %s (%s): discovered %d new dialog(s) out of %d scanned (kinds: %s)",
            account.id,
            account.label,
            created,
            seen,
            ", ".join(sorted(kinds)),
        )
    return created


async def _collect_with_account(
    db: Session,
    account: TelegramAccount,
    channels: list[Channel],
    pacer: Pacer,
    api_id: int,
    api_hash: str,
    run: IngestSummary | None = None,
    *,
    is_default: bool = False,
) -> tuple[int, int]:
    """Run one account's share of the channels. Never raises.

    Returns ``(links stored, dialogs actually read)``. The second number is
    not ``len(channels)`` as the caller passed it: discovery can register
    dialogs *during* this call, and the run's own log line and the
    "everything failed" alert both describe what was really attempted.

    Per-account isolation is the point: a revoked session, a banned
    account or a network failure on one account must not cost the run the
    channels every *other* account could still have collected.
    """
    try:
        session_string = decrypt_field(account.session_string)
    except InvalidToken:
        logger.error(
            "account %s (%s): session string could not be decrypted — wrong FIELD_ENCRYPTION_KEY, "
            "or the row predates encryption; skipping",
            account.id,
            account.label,
        )
        # A key mismatch will not fix itself on the next run, so it counts
        # as a failure like any other and will eventually disable the
        # account rather than being retried hourly forever.
        record_failure(db, account, "session string could not be decrypted (FIELD_ENCRYPTION_KEY mismatch)")
        return 0, 0

    try:
        # Constructing the client is inside the try, not before it:
        # StringSession() *parses* the stored string and raises on a
        # malformed one. Outside the try that exception escapes
        # _collect_with_account entirely and ends the whole run — which is
        # the opposite of the per-account isolation this function promises,
        # and it happens for exactly the account most likely to be broken.
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.start()
    except Exception as exc:  # noqa: BLE001 - one bad account must not end the run
        logger.error("account %s (%s): cannot connect: %s", account.id, account.label, exc)
        record_failure(db, account, f"cannot connect: {exc}")
        return 0, 0

    try:
        # Discovery first, and its result re-reads the channel list: a
        # dialog registered a moment ago is collectable in this same run,
        # not only in the next one. The caller's list was computed before
        # the connection existed, so it cannot contain anything new.
        if await _discover_dialogs(client, db, account.workspace_id, account):
            channels = _channels_for(db, account.workspace_id, account, is_default=is_default)

        # Read once per account, not once per message. The rule set is
        # small and never changes mid-run, and reading it per message would
        # put a SELECT between every stored link and the next.
        keyword_rules = active_keyword_rules(db, account.workspace_id)

        total = 0
        for index, channel in enumerate(channels):
            # Before each dialog except the first: the pause belongs
            # *between* reads, and pausing before the only read in a run
            # would be pure latency with nothing to hide.
            if index:
                await pacer.wait()
            total += await _collect_channel(
                client, db, channel, run, keyword_rules, account_id=account.id, is_default=is_default
            )
        logger.info(
            "account %s (%s): %d new link(s) across %d channel(s)",
            account.id,
            account.label,
            total,
            len(channels),
        )
        record_success(db, account, links_collected=total)
        return total, len(channels)
    except Exception as exc:  # noqa: BLE001 - same reasoning as above
        logger.error("account %s (%s): run aborted: %s", account.id, account.label, exc)
        record_failure(db, account, f"run aborted: {exc}")
        return 0, len(channels)
    finally:
        await client.disconnect()


async def collect() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_string = os.environ["TG_SESSION_STRING"]
    workspace_id = int(os.environ["COLLECTOR_WORKSPACE_ID"])

    db = SessionLocal()
    try:
        # Every query below hits tables under row-level security. Without
        # this the collector would find zero channels and report a quiet,
        # successful run that collected nothing (see app/rls.py).
        scope_session_to_workspace(db, workspace_id)

        _ensure_primary_account(db, workspace_id, session_string)

        accounts = (
            db.query(TelegramAccount)
            .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.is_active.is_(True))
            .order_by(TelegramAccount.id)
            .all()
        )
        if not accounts:
            logger.info("no active collecting accounts for this workspace; nothing to do")
            return

        # The lowest-numbered account is the default, and inherits every
        # channel that has not been assigned to a specific account.
        default_account_id = accounts[0].id
        auto_discover = get_settings().collector_auto_discover

        # Distribute sources across the accounts before reading any of
        # them. Runs every time rather than on a button, because the input
        # it reacts to — an account disabled after three failures — is
        # itself automatic, and an assignment that only refreshes when a
        # human remembers to click is an assignment that is wrong exactly
        # when it matters.
        report = apply_assignments(db, workspace_id)
        if report.moved or report.needs_attention:
            logger.info(
                "assignment: %d moved, %d kept, %d stranded, %d over capacity",
                report.moved,
                report.kept,
                len(report.stranded),
                len(report.overflow),
            )

        total = 0
        collected_channels = 0
        working_accounts = 0
        # Run-level, not per-channel: a workspace following twenty channels
        # would otherwise get twenty separate messages from one run.
        run = IngestSummary()
        # One pacer for the whole run, not one per account. The budget is a
        # property of the *job's* wall clock — ten accounts each with their
        # own 240-second allowance is 2,400 seconds, which is the timeout
        # this exists to avoid.
        pacer = _pacer()
        for account in accounts:
            is_default = account.id == default_account_id
            channels = _channels_for(db, workspace_id, account, is_default=is_default)
            if not channels and not auto_discover:
                # With discovery off this is genuinely nothing to do. With
                # it on, the account may hold dialogs nobody has registered
                # yet — which is exactly the case discovery exists for, so
                # the run must reach the connection to find out.
                logger.info("account %s (%s): no channels assigned", account.id, account.label)
                continue
            before = account.consecutive_failures
            links, read = await _collect_with_account(
                db, account, channels, pacer, api_id, api_hash, run, is_default=is_default
            )
            total += links
            collected_channels += read
            # A run that raised increments the counter; one that worked
            # resets it. Comparing across the call is how this tells the
            # two apart without duplicating the bookkeeping.
            if account.consecutive_failures <= before:
                working_accounts += 1

        if collected_channels == 0:
            logger.info("no active channels configured for this workspace; nothing to do")
            return

        if working_accounts == 0:
            # Idea 154. This is the failure with no symptom: the dashboard
            # keeps working, search keeps working, and the collection
            # simply stops growing. Every account failing while channels
            # are still configured is the one case worth interrupting
            # somebody for — and it stayed silent before this.
            await raise_alert(
                db,
                workspace_id,
                COLLECTOR_FAILED.key,
                title="⛔ توقّف الجمع بالكامل",
                body=(
                    f"كل حسابات الجمع ({len(accounts)}) أخفقت في هذه التشغيلة، "
                    f"بينما ما تزال {collected_channels} قناة نشطة.\n"
                    "افحص «حسابات الجمع» في لوحة التحكم: جلسة ملغاة تحتاج إعادة تفويض."
                ),
            )
        # Idea 152. Sent once for the whole run, after every channel has
        # committed, so nothing is announced that a rollback took back.
        await report_adult_links(db, workspace_id, run.adult_urls)

        logger.info(
            "done: %d new link(s) across %d channel(s) using %d account(s)",
            total,
            collected_channels,
            len(accounts),
        )
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Before anything touches Telegram or the database. This job's entire
    # purpose involves a Telegram session string — a bearer credential for
    # a real account — which it stores encrypted under FIELD_ENCRYPTION_KEY.
    # If that key is the one published in this repository, the encryption
    # is decorative: anyone with the database and the public code reads the
    # session string and controls the account. app/main.py has refused to
    # start the web service on a published default since 6bda8ba; the
    # collector was still exempt, which is the more dangerous of the two
    # because it is the process that writes those rows in the first place.
    #
    # SECRET_KEY is not checked here: the collector serves no HTTP and
    # signs no cookie, and collector.yml sets it to a placeholder on
    # purpose. Checking it would fail the run for an irrelevant reason.
    require_real_secrets(get_settings(), names=("FIELD_ENCRYPTION_KEY",), job="collector")
    asyncio.run(collect())


if __name__ == "__main__":
    main()
