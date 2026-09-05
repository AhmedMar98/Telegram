"""Bulk source import: preview, dry run, explicit commit, undo.

The canonical criteria this file holds:

  AC-I02  bulk import supports preview, validation and duplicate detection
          before the final commit
  AC-I03  the import can be simulated without modifying real data
  AC-I04  a bulk import can be undone, and the undo is defined and traceable
  AC-I06  removing a source does not erase history
  §17     dry run performs zero persistent mutations; import is idempotent
  §18     partial-commit semantics are explicit and every row is traceable

The property that needs the most care is the dry run, because the failure
mode is invisible in the response: a preview that writes returns exactly
what a preview that does not write returns. So the tests below never take
the response's word for it — every dry-run assertion counts the rows in
the database before and after.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.classifier import hash_url
from app.database import SessionLocal
from app.models import AuditLog, Channel, Link
from tests.conftest import register_workspace

PASTE = "@alpha_channel\nhttps://t.me/beta_channel\n@gamma_channel"


def _channel_names(workspace_id: int) -> set[str]:
    db = SessionLocal()
    try:
        return {
            c.username or c.tg_channel_id
            for c in db.query(Channel).filter(Channel.workspace_id == workspace_id).all()
        }
    finally:
        db.close()


def _count(model, workspace_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(model).filter(model.workspace_id == workspace_id).count()
    finally:
        db.close()


@pytest.fixture
def workspace(client: TestClient) -> int:
    register_workspace(client, email="import@example.com", workspace_name="Import Co")
    return client.get("/auth/me").json()["workspace_id"]


def _preview(client: TestClient, text: str) -> dict:
    response = client.post("/channels/import", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client: TestClient, text: str) -> dict:
    response = client.post("/channels/import", json={"text": text, "commit": True})
    assert response.status_code == 200, response.text
    return response.json()


# --- AC-I03: the dry run ---------------------------------------------------


def test_a_preview_writes_absolutely_nothing(client: TestClient, workspace: int):
    """AC-I03. Counted in the database, not read off the response.

    A preview that persisted would return the same body as one that did
    not, so the response cannot be the evidence here.
    """
    channels_before = _count(Channel, workspace)
    audit_before = _count(AuditLog, workspace)

    body = _preview(client, PASTE)

    assert body["committed"] is False
    assert body["batch_id"] is None
    assert body["counts"]["new"] == 3
    assert _count(Channel, workspace) == channels_before, "the dry run created sources"
    assert _count(AuditLog, workspace) == audit_before, "the dry run wrote to the audit log"


def test_a_preview_is_not_recorded_as_an_import(client: TestClient, workspace: int):
    """A preview is not an event.

    If previews wrote audit rows, the audit log could no longer answer
    "what was actually imported" — which is the one question it is kept
    for, and the question the undo depends on.
    """
    before = _count(AuditLog, workspace)

    _preview(client, PASTE)

    assert _count(AuditLog, workspace) == before


def test_the_preview_predicts_exactly_what_the_commit_does(client: TestClient, workspace: int):
    """The dry run's promise is only worth the commit keeping it."""
    predicted = _preview(client, PASTE)
    actual = _commit(client, PASTE)

    assert actual["counts"] == predicted["counts"]
    assert [row["disposition"] for row in actual["rows"]] == [row["disposition"] for row in predicted["rows"]]
    assert [row["raw"] for row in actual["rows"]] == [row["raw"] for row in predicted["rows"]]


# --- AC-I02: validation and duplicate detection ----------------------------


def test_every_line_comes_back_with_a_disposition(client: TestClient, workspace: int):
    """§18: a partial commit has to be traceable, row by row.

    "17 added" without the rows leaves the operator who pasted the list
    unable to find which three were dropped — which is exactly the
    information a partial commit exists to convey.
    """
    body = _preview(
        client,
        "@alpha_channel\nnot a source at all\n@alpha_channel\nhttps://t.me/joinchat/AAAA\n-1001234567890",
    )

    assert [row["disposition"] for row in body["rows"]] == [
        "new",
        "invalid",
        "repeated",
        "needs_account",
        "needs_account",
    ]
    assert all(row["reason"] for row in body["rows"] if row["disposition"] != "new")
    assert body["counts"] == {"new": 1, "duplicate": 0, "repeated": 1, "invalid": 1, "needs_account": 2}


def test_a_source_already_in_the_workspace_is_a_duplicate_not_an_error(client: TestClient, workspace: int):
    """AC-I02. And AC-S01: no second row for one identity."""
    _commit(client, "@alpha_channel")

    body = _preview(client, "@alpha_channel\n@delta_channel")

    assert [row["disposition"] for row in body["rows"]] == ["duplicate", "new"]


def test_two_spellings_of_one_source_in_one_paste_collapse(client: TestClient, workspace: int):
    """In-batch duplicate detection, not only batch-against-database.

    Without it both lines plan as ``new`` and the second fails at INSERT —
    turning a condition the preview could have reported into a runtime
    error partway through a commit.
    """
    body = _commit(client, "@alpha_channel\nhttps://t.me/alpha_channel\nhttps://t.me/s/alpha_channel")

    assert [row["disposition"] for row in body["rows"]] == ["new", "repeated", "repeated"]
    assert _channel_names(workspace) == {"alpha_channel"}


def test_an_invite_link_is_named_rather_than_silently_registered(client: TestClient, workspace: int):
    """A private invite cannot be read without an account inside the chat.

    Registering it anyway would produce a source that collects nothing,
    which is indistinguishable from a channel that posts nothing — the
    failure AC-A04 asks not to disguise ("Needs Access, not Invalid").
    """
    body = _preview(client, "https://t.me/joinchat/AAAAAAAAAAAA")

    assert body["rows"][0]["disposition"] == "needs_account"
    assert "حساب" in body["rows"][0]["reason"]


def test_garbage_does_not_become_a_valid_looking_source(client: TestClient, workspace: int):
    """AC-S03: an unresolvable input must not turn into a real record."""
    body = _commit(client, "hello world\n???\nhttps://example.com/not-telegram/x")

    assert {row["disposition"] for row in body["rows"]} == {"invalid"}
    assert _count(Channel, workspace) == 0


def test_a_bad_line_does_not_reject_the_good_ones(client: TestClient, workspace: int):
    """§18, stated: validated partial commit, not all-or-nothing."""
    body = _commit(client, "@alpha_channel\nnot a source\n@beta_channel")

    assert body["counts"]["new"] == 2
    assert body["counts"]["invalid"] == 1
    assert _channel_names(workspace) == {"alpha_channel", "beta_channel"}


def test_an_oversized_import_is_refused_before_anything_is_planned(client: TestClient, workspace: int):
    text = "\n".join(f"@source_{i}" for i in range(501))

    response = client.post("/channels/import", json={"text": text, "commit": True})

    assert response.status_code == 422
    assert _count(Channel, workspace) == 0


def test_an_empty_body_is_refused(client: TestClient, workspace: int):
    assert client.post("/channels/import", json={"text": ""}).status_code == 422


def test_a_paste_of_only_blank_lines_is_an_empty_plan_not_an_error(client: TestClient, workspace: int):
    """Empty result, valid request — the AC-SR05 principle, applied here."""
    body = _preview(client, "\n\n   \n,,\n")

    assert body["rows"] == []
    assert body["counts"]["new"] == 0


# --- §17: idempotency ------------------------------------------------------


def test_committing_the_same_import_twice_adds_nothing_the_second_time(client: TestClient, workspace: int):
    first = _commit(client, PASTE)
    second = _commit(client, PASTE)

    assert first["counts"]["new"] == 3
    assert second["counts"]["new"] == 0
    assert second["counts"]["duplicate"] == 3
    assert _count(Channel, workspace) == 3


# --- AC-I04: undo ----------------------------------------------------------


def test_an_import_can_be_undone(client: TestClient, workspace: int):
    body = _commit(client, PASTE)

    response = client.post(f"/channels/import/{body['batch_id']}/undo")

    assert response.status_code == 200, response.text
    assert sorted(response.json()["removed"]) == sorted(row["channel_id"] for row in body["rows"])
    assert response.json()["kept"] == []
    assert _count(Channel, workspace) == 0


def test_the_undo_is_traceable_to_the_batch_it_undid(client: TestClient, workspace: int):
    """AC-I04 asks for an undo that is *defined and traceable*.

    The handle is the audit row's own id, so the thing that authorises the
    undo and the thing that records what it will undo are one object, not
    two that can disagree.
    """
    body = _commit(client, "@alpha_channel")
    client.post(f"/channels/import/{body['batch_id']}/undo")

    db = SessionLocal()
    try:
        actions = [row.action for row in db.query(AuditLog).filter(AuditLog.workspace_id == workspace).all()]
        undo_row = db.query(AuditLog).filter(AuditLog.action == "channel.bulk_import_undo").one()
    finally:
        db.close()

    assert "channel.bulk_import" in actions
    assert undo_row.target_id == str(body["batch_id"])
    assert undo_row.detail == "removed=1 kept=0"


def test_the_undo_refuses_to_take_collected_links_with_it(client: TestClient, workspace: int):
    """AC-I06 / AC-S04: removing a source must not erase history.

    Once a source has collected, it is no longer only the import's
    artefact. Deleting it here would delete real links, so it is kept —
    and reported as kept, rather than the undo quietly doing less than its
    name says.
    """
    body = _commit(client, PASTE)
    collected, untouched = body["rows"][0]["channel_id"], body["rows"][1]["channel_id"]

    db = SessionLocal()
    try:
        url = "https://example.com/collected.pdf"
        db.add(
            Link(
                workspace_id=workspace,
                channel_id=collected,
                message_id=1,
                url=url,
                url_hash=hash_url(url),
                domain="example.com",
                category="other",
                confidence=0.5,
                classified_by="rules-v2",
            )
        )
        db.commit()
    finally:
        db.close()

    result = client.post(f"/channels/import/{body['batch_id']}/undo").json()

    assert result["kept"] == [collected]
    assert untouched in result["removed"]
    assert _count(Link, workspace) == 1, "the undo deleted a collected link"


def test_an_unknown_batch_is_not_found(client: TestClient, workspace: int):
    assert client.post("/channels/import/9999/undo").status_code == 404


def test_an_audit_row_that_is_not_an_import_cannot_be_undone(client: TestClient, workspace: int):
    """The undo takes an audit id, so it must check *which* action it is.

    Without the action filter, any audit row's id would be accepted and the
    endpoint would parse an unrelated ``detail`` field as a list of channel
    ids to delete.
    """
    client.post("/channels", json={"tg_channel_id": "-100999", "username": "manual"})

    db = SessionLocal()
    try:
        other = db.query(AuditLog).filter(AuditLog.action == "channel.add").one().id
    finally:
        db.close()

    assert client.post(f"/channels/import/{other}/undo").status_code == 404
    assert _count(Channel, workspace) == 1


# --- workspace isolation ---------------------------------------------------


def test_one_workspace_cannot_undo_another_workspaces_import(client: TestClient):
    """Not merely refused — indistinguishable from a batch that never was."""
    register_workspace(client, email="victim-import@example.com", workspace_name="Victim")
    victim = _commit(client, PASTE)
    victim_workspace = client.get("/auth/me").json()["workspace_id"]
    client.post("/auth/logout")

    register_workspace(client, email="attacker-import@example.com", workspace_name="Attacker")

    assert client.post(f"/channels/import/{victim['batch_id']}/undo").status_code == 404
    assert _count(Channel, victim_workspace) == 3


def test_an_import_lands_in_the_callers_workspace_only(client: TestClient):
    register_workspace(client, email="ws-a@example.com", workspace_name="A")
    first = client.get("/auth/me").json()["workspace_id"]
    _commit(client, "@alpha_channel")
    client.post("/auth/logout")

    register_workspace(client, email="ws-b@example.com", workspace_name="B")
    second = client.get("/auth/me").json()["workspace_id"]
    _commit(client, "@alpha_channel")

    assert _count(Channel, first) == 1
    assert _count(Channel, second) == 1


def test_the_same_source_in_two_workspaces_is_not_a_duplicate(client: TestClient):
    """Duplicate detection is per workspace, which is the isolation
    boundary — not a global uniqueness rule that would leak whether
    another tenant already collects a channel."""
    register_workspace(client, email="dup-a@example.com", workspace_name="DupA")
    _commit(client, "@alpha_channel")
    client.post("/auth/logout")

    register_workspace(client, email="dup-b@example.com", workspace_name="DupB")

    assert _preview(client, "@alpha_channel")["rows"][0]["disposition"] == "new"


def test_an_anonymous_caller_cannot_import(client: TestClient):
    assert client.post("/channels/import", json={"text": PASTE}).status_code in (401, 403)
    assert client.post("/channels/import/1/undo").status_code in (401, 403)


# --- defence in depth ------------------------------------------------------


def test_the_undo_helper_refuses_foreign_ids_even_when_called_directly(client: TestClient):
    """``sourceimport.undo`` scopes by workspace itself, not only via the route.

    The endpoint already refuses another workspace's ``batch_id`` before
    reaching this function, so in the running system this filter is a
    second lock on a locked door — which is exactly why it needs its own
    test. Sabotaging it changes no HTTP behaviour and no HTTP test fails,
    so without this the filter would be an untested claim, and the day the
    outer check moved or a new caller appeared it would be found to have
    never worked.
    """
    from app import sourceimport

    register_workspace(client, email="depth-victim@example.com", workspace_name="DepthVictim")
    victim_workspace = client.get("/auth/me").json()["workspace_id"]
    victim_ids = [row["channel_id"] for row in _commit(client, PASTE)["rows"]]
    client.post("/auth/logout")

    register_workspace(client, email="depth-attacker@example.com", workspace_name="DepthAttacker")
    attacker_workspace = client.get("/auth/me").json()["workspace_id"]

    db = SessionLocal()
    try:
        removed, kept = sourceimport.undo(db, attacker_workspace, victim_ids)
        db.commit()
    finally:
        db.close()

    assert removed == []
    assert kept == []
    assert _count(Channel, victim_workspace) == 3, "another workspace's sources were deleted"


# --- the shape of the work -------------------------------------------------


def test_planning_reads_the_sources_table_once_however_long_the_list(client: TestClient, workspace: int):
    """The duplicate check must not re-read the table per line.

    ``app.dialogs.existing_channel`` reads every channel in the workspace
    and compares canonicalised spellings in Python, which is right for its
    own caller — dialog discovery asks about one dialog at a time. Called
    once per imported line it is an N+1 that grows as lines × sources.

    Measured before it was fixed, not guessed: re-importing 200 sources
    into a workspace already holding 200 took 622ms, against 139ms for the
    commit that actually wrote 200 rows — the no-op path was four and a
    half times slower than the working one. After hoisting the index out
    of the loop the same call is 15.7ms.

    Asserted as a *count of queries* rather than a duration, because a
    timing threshold on a shared runner is a flake generator and would say
    nothing about why. One read is the property; the wall clock was only
    how it was noticed.
    """
    from sqlalchemy import event

    from app import sourceimport
    from app.database import engine

    _commit(client, "\n".join(f"@existing_{i}" for i in range(40)))

    channel_selects = 0

    def count(conn, cursor, statement, parameters, context, executemany):
        nonlocal channel_selects
        normalised = " ".join(statement.split()).lower()
        if normalised.startswith("select") and "from channels" in normalised:
            channel_selects += 1

    db = SessionLocal()
    event.listen(engine, "before_cursor_execute", count)
    try:
        plan = sourceimport.plan(db, workspace, "\n".join(f"@candidate_{i}" for i in range(120)))
    finally:
        event.remove(engine, "before_cursor_execute", count)
        db.close()

    assert plan.counts()["new"] == 120
    assert channel_selects == 1, (
        f"planning 120 lines issued {channel_selects} queries against channels. "
        "The workspace's sources are read once and indexed; one query per line is "
        "the N+1 this test exists to keep out."
    )
