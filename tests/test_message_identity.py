"""Message identity and the idempotency it buys (§43.5).

The invariant that carries the most risk is the *narrowness* of it: only a
real Telegram message has identity here. Manual entry and the bookmark
importer number every row 0 or 1, 2, 3…, and treating those as identities
drops rows silently — which is what ``test_manual_links_are_never_collapsed``
and ``test_a_bookmark_files_position_is_not_an_identity`` exist to catch.
"""

import pytest

from app.database import SessionLocal
from app.ingest import ingest_text, message_seen, record_message
from app.models import Channel, Link, Message, Workspace
from tests.conftest import register_workspace


@pytest.fixture()
def channel() -> tuple[int, int]:
    db = SessionLocal()
    try:
        workspace = Workspace(name="MsgId")
        db.add(workspace)
        db.flush()
        row = Channel(workspace_id=workspace.id, tg_channel_id="-1009", title="قناة", username="msgid")
        db.add(row)
        db.commit()
        return workspace.id, row.id
    finally:
        db.close()


def _ingest(workspace_id: int, channel_id: int, text: str, message_id: int):
    db = SessionLocal()
    try:
        summary = ingest_text(
            db, workspace_id=workspace_id, channel_id=channel_id, text=text, message_id=message_id
        )
        db.commit()
        return summary
    finally:
        db.close()


def test_a_message_is_processed_once_however_many_readers_reach_it(channel):
    """The overlap is by design: the live listener and the hourly collector
    both see a message that arrived while the instance was awake."""
    workspace_id, channel_id = channel

    first = _ingest(workspace_id, channel_id, "https://example.com/a.apk", 100)
    second = _ingest(workspace_id, channel_id, "https://example.com/a.apk", 100)

    assert (first.stored, first.already_processed) == (1, 0)
    assert (second.stored, second.already_processed) == (0, 1)


def test_a_message_row_means_processed_not_merely_seen(channel):
    """A message that produced nothing durable leaves no row.

    Otherwise a 1 GiB database becomes a full Telegram archive: most
    messages carry no link at all.
    """
    workspace_id, channel_id = channel
    _ingest(workspace_id, channel_id, "مرحبا كيف الحال", 200)

    db = SessionLocal()
    try:
        assert db.query(Message).filter(Message.tg_message_id == 200).first() is None
        assert not message_seen(db, channel_id=channel_id, message_id=200)
    finally:
        db.close()


def test_manual_links_are_never_collapsed_into_one_message(client):
    """Every manual link carries message_id 0. If 0 had identity, the
    second link a person adds by hand would vanish without a word."""
    register_workspace(client, email="manual@example.com", workspace_name="Manual")

    client.post("/links", json={"text": "https://example.com/first"})
    client.post("/links", json={"text": "https://example.com/second"})
    client.post("/links", json={"text": "https://example.com/third"})

    items = client.get("/links").json()["items"]
    assert len(items) == 3


def test_a_bookmark_files_position_is_not_an_identity(tmp_path):
    """Re-importing an edited bookmark file must not skip shifted rows.

    The importer used to pass the row's position as its message id. With
    identity attached to that, inserting one bookmark at the top of the
    file shifts every position onto an id already claimed, and the whole
    file after the insert is skipped as "already processed".
    """
    from scripts.import_bookmarks import run as import_bookmarks

    db = SessionLocal()
    try:
        workspace = Workspace(name="Bookmarks")
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id
    finally:
        db.close()

    path = tmp_path / "b.html"
    path.write_text('<a href="https://example.com/one">One</a>', encoding="utf-8")
    import_bookmarks([path], workspace_id, dry_run=False)

    # The same file with a new bookmark inserted *before* the existing one.
    path.write_text(
        '<a href="https://example.com/zero">Zero</a>\n<a href="https://example.com/one">One</a>',
        encoding="utf-8",
    )
    second, _ = import_bookmarks([path], workspace_id, dry_run=False)

    assert second.stored == 1, "the inserted bookmark must be stored, not skipped"
    assert second.duplicates == 1


def test_record_message_refuses_a_zero_id(channel):
    workspace_id, channel_id = channel
    db = SessionLocal()
    try:
        assert record_message(db, workspace_id=workspace_id, channel_id=channel_id, message_id=0) is None
    finally:
        db.close()


def test_message_seen_refuses_a_zero_id_on_its_own(channel):
    """Both halves of the id-0 rule are pinned, not just their combination.

    ``record_message`` refusing to write a row for id 0 already makes
    ``message_seen(0)`` answer False in practice — which means removing the
    guard *inside* ``message_seen`` breaks nothing any other test can see.
    A guard whose loss no test detects is untested, so this reaches past
    the other guard: it writes the row by hand, then asks.
    """
    workspace_id, channel_id = channel
    db = SessionLocal()
    try:
        db.add(Message(workspace_id=workspace_id, channel_id=channel_id, tg_message_id=0))
        db.commit()
        assert message_seen(db, channel_id=channel_id, message_id=0) is False
        assert message_seen(db, channel_id=channel_id, message_id=-5) is False
    finally:
        db.close()


def test_a_concurrent_writer_loses_one_row_not_the_batch(channel):
    """Two readers can reach the same message at the same moment."""
    workspace_id, channel_id = channel
    db = SessionLocal()
    try:
        first = record_message(db, workspace_id=workspace_id, channel_id=channel_id, message_id=300)
        db.commit()
        second = record_message(db, workspace_id=workspace_id, channel_id=channel_id, message_id=300)
        assert first is not None and second is not None
        assert first.id == second.id, "the loser gets the winner's row, not an exception"
    finally:
        db.close()


def test_links_point_back_at_their_message(channel):
    workspace_id, channel_id = channel
    _ingest(workspace_id, channel_id, "https://example.com/x.apk و https://example.com/y.pdf", 400)

    db = SessionLocal()
    try:
        message = db.query(Message).filter(Message.tg_message_id == 400).one()
        links = db.query(Link).filter(Link.message_id == 400).all()
        assert len(links) == 2
        assert {link.message_ref_id for link in links} == {message.id}
    finally:
        db.close()


def test_a_manual_link_has_no_message_to_point_at(client):
    register_workspace(client, email="noref@example.com", workspace_name="NoRef")
    client.post("/links", json={"text": "https://example.com/manual"})

    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.url == "https://example.com/manual").one()
        assert link.message_ref_id is None
    finally:
        db.close()
