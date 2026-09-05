"""Bulk source import: decide everything, then commit nothing until asked.

AC-I02, AC-I03 and AC-I04 ask for one shape — preview, validation and
duplicate detection *before* the commit, a dry run that changes nothing,
and an undo that is defined rather than improvised. The platform could add
sources one at a time (``POST /channels``) and had no way to add fifty,
which in practice means fifty requests with no preview of what they would
do and no way back if half of them were wrong.

**The dry run is the same code path, not a parallel one.** ``plan()``
decides every row's disposition and touches nothing; ``commit()`` takes a
plan and writes only the rows the plan already marked ``new``. A dry run
that runs different code from the real thing is a dry run that can
disagree with it, which is worse than not having one — the preview would
be a promise the commit does not keep.

**Transaction semantics are validated partial commit, stated rather than
implied** (§18). One unparseable line in a pasted list of two hundred must
not reject the other hundred and ninety-nine, so accepted, rejected and
skipped rows are all reported per row with a reason. The alternative,
all-or-nothing, reads stricter but makes the feature unusable for its
actual input: a list a person assembled by hand.

**Provenance lives in the audit log, not in a column.** ``channels`` has
no origin column and adding one is a schema migration against a live
database, which this phase deliberately does not take. The audit row
written by ``commit()`` carries the batch's identifier and the ids it
created, which is what makes the undo traceable and what distinguishes a
bulk-imported source from a hand-added one (``channel.add``) or a public
one (``channel.add_public``). The limitation is real and worth naming:
"which sources arrived by import?" is answered by reading the audit log,
not by filtering a column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session

from app.dialogs import SOURCE_PUBLIC, index_channels, lookup_channel
from app.identity import source_identity_key
from app.models import Channel
from app.publicsource import classify_source

# What can happen to one line of the input. Every line gets exactly one,
# and only ``new`` produces a row.
Disposition = Literal[
    "new",  # will be created (or was)
    "duplicate",  # this workspace already has this source
    "repeated",  # named twice in this same input
    "invalid",  # not a Telegram source reference at all
    "needs_account",  # a real reference that cannot be read without one
]

# Why each non-``new`` disposition happened, in the operator's language.
# Held here rather than built at the call site so the preview and the
# commit cannot describe the same row differently.
REASONS: dict[str, str] = {
    "duplicate": "مُضاف مسبقاً في هذه المساحة",
    "repeated": "مكرّر داخل نفس القائمة",
    "invalid": "لم أتعرّف على هذا المدخل. المتوقّع: @اسم_القناة أو رابط t.me/اسم_القناة",
    "needs_account": "يحتاج حساب جمع منضمّاً — أضِفه من «حسابات الجمع»",
}

# One import may not exceed this many lines. Not a performance limit: the
# preview reads the workspace's channels once and compares in Python (see
# app.dialogs.existing_channel), and an operator pasting more than this has
# a file rather than a list, which is a different feature.
MAX_LINES = 500


@dataclass(frozen=True)
class PlannedSource:
    """One input line, and what would become of it."""

    raw: str
    disposition: Disposition
    # The public username this line resolves to, when it resolves to one.
    # ``None`` for every disposition that will not create a row.
    username: str | None = None
    reason: str | None = None
    # Set only after a commit, and only for rows that were created.
    channel_id: int | None = None


@dataclass(frozen=True)
class ImportPlan:
    """What an import would do, before it does any of it."""

    rows: list[PlannedSource] = field(default_factory=list)

    @property
    def creatable(self) -> list[PlannedSource]:
        return [row for row in self.rows if row.disposition == "new"]

    def counts(self) -> dict[str, int]:
        """One tally per disposition, including the zeroes.

        Every key is always present: a preview that omits ``invalid``
        because there were none reads the same as one where the field was
        never computed, and an operator cannot tell those apart.
        """
        tally = dict.fromkeys(("new", "duplicate", "repeated", "invalid", "needs_account"), 0)
        for row in self.rows:
            tally[row.disposition] += 1
        return tally


def parse_lines(text: str) -> list[str]:
    """The candidate references in a pasted block.

    Split on newlines and commas both: an operator copying from a
    spreadsheet column produces the first, and one copying from a sentence
    produces the second. Blank entries are dropped rather than becoming
    ``invalid`` rows — a trailing newline is not a mistake the operator
    needs told about.
    """
    parts: list[str] = []
    for line in (text or "").splitlines():
        parts.extend(piece.strip() for piece in line.split(","))
    return [piece for piece in parts if piece]


def plan(db: Session, workspace_id: int, text: str) -> ImportPlan:
    """Decide every line's fate. Reads the database; writes nothing.

    This *is* the dry run. Calling it and not calling ``commit`` is the
    whole of AC-I03 — there is no separate simulation to drift out of step
    with the real path.
    """
    rows: list[PlannedSource] = []
    # Identities claimed by earlier lines of this same input. Duplicate
    # detection has to cover the batch against itself, not only the batch
    # against the database: two spellings of one channel in one paste
    # would otherwise both read as "new" and the second would fail at
    # INSERT, turning an avoidable preview into a runtime error.
    claimed: set[str] = set()

    # The workspace's existing sources, read once.
    #
    # ``app.dialogs.existing_channel`` is the same lookup and reads the
    # table on every call, which is right for its own caller — dialog
    # discovery checks one dialog at a time. Called once per line it is an
    # N+1, and the cost was measured rather than assumed: re-importing 200
    # sources into a workspace that already held 200 took 622ms against
    # 139ms for the first commit, so the *no-op* path was four and a half
    # times slower than the one that wrote 200 rows. It grows as
    # lines × sources, and the MVP targets 500 sources per account across
    # ten accounts, where the same shape is seconds rather than
    # milliseconds.
    #
    # Building the index once is exact, not approximate: it is the same
    # index_channels/lookup_channel pair existing_channel builds
    # internally, so both spellings still resolve. Safe to hoist because
    # plan() writes nothing — no row can appear mid-loop — and rows
    # claimed by earlier lines are tracked separately above.
    known = index_channels(db.query(Channel).filter(Channel.workspace_id == workspace_id).all())

    for raw in parse_lines(text):
        ref = classify_source(raw)
        if ref is None:
            rows.append(PlannedSource(raw=raw, disposition="invalid", reason=REASONS["invalid"]))
            continue

        if ref.kind in ("invite", "id"):
            # Both are real references the platform understands and neither
            # can be read without an account already inside the chat. Saying
            # so by name is the point: registering them anyway would produce
            # a source that silently collects nothing, which looks exactly
            # like a channel that posts nothing.
            rows.append(PlannedSource(raw=raw, disposition="needs_account", reason=REASONS["needs_account"]))
            continue

        username = ref.value
        identity = source_identity_key(username) or username

        if identity in claimed:
            rows.append(PlannedSource(raw=raw, disposition="repeated", reason=REASONS["repeated"]))
            continue

        if lookup_channel(known, username, username) is not None:
            rows.append(PlannedSource(raw=raw, disposition="duplicate", reason=REASONS["duplicate"]))
            continue

        claimed.add(identity)
        rows.append(PlannedSource(raw=raw, disposition="new", username=username))

    return ImportPlan(rows=rows)


def commit(db: Session, workspace_id: int, plan_to_apply: ImportPlan) -> ImportPlan:
    """Create exactly the rows the plan marked ``new``.

    Returns the same plan with ``channel_id`` filled in on the rows that
    were created, so the caller reports what happened rather than what was
    predicted — identical here by construction, and the caller should not
    have to take that on trust.

    Flushes but does not commit: the audit row the caller writes belongs in
    the same transaction as the channels it describes. An audit entry that
    can survive a rolled-back import, or an import that can survive a
    failed audit write, is a record that disagrees with the data.
    """
    applied: list[PlannedSource] = []
    for row in plan_to_apply.rows:
        if row.disposition != "new" or row.username is None:
            applied.append(row)
            continue

        channel = Channel(
            workspace_id=workspace_id,
            tg_channel_id=row.username,
            username=row.username,
            title=row.username,
            # Public preview reader: an imported username is exactly the
            # case add_public_source handles, and it needs no account.
            kind="channel",
            source=SOURCE_PUBLIC,
            account_id=None,
        )
        db.add(channel)
        db.flush()
        applied.append(
            PlannedSource(
                raw=row.raw,
                disposition="new",
                username=row.username,
                channel_id=channel.id,
            )
        )

    return ImportPlan(rows=applied)


def undo(db: Session, workspace_id: int, channel_ids: list[int]) -> tuple[list[int], list[int]]:
    """Remove the sources one import created. Returns (removed, kept).

    **An undo that would lose data is refused, per source rather than for
    the batch.** A source that has collected links since the import is no
    longer only the import's artefact: deleting it would take the links
    with it, and AC-S04/AC-I06 both say removing a source must not erase
    history. Those are reported as kept, with their ids, so the operator
    sees precisely what the undo did not touch and why — rather than the
    undo silently doing less than its name.

    Scoped by workspace in the query itself, so an id belonging to another
    workspace matches nothing instead of revealing that it exists.
    """
    removed: list[int] = []
    kept: list[int] = []

    for channel in (
        db.query(Channel)
        .filter(Channel.workspace_id == workspace_id, Channel.id.in_(channel_ids or [-1]))
        .order_by(Channel.id)
        .all()
    ):
        if channel.links:
            kept.append(channel.id)
            continue
        db.delete(channel)
        removed.append(channel.id)

    return removed, kept
