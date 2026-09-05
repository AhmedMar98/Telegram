"""The backup workflow is the last line against losing everything.

It is also the one piece of this project that cannot be tested by running
it: a real run needs a real database and a real GitHub runner. So what is
pinned here is its *contract* — the properties that, if they silently
changed, would leave a backup that looks healthy and protects nothing.

The failure this file exists to prevent is specific: a dump that quietly
uploads unencrypted because a secret was missing. That is the same shape
as the defect app/main.py's lifespan check was written for — something
that keeps working, keeps reporting success, and has stopped protecting
anything.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github/workflows/backup.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


DUMP_STEP = "Dump and encrypt database"


def _dump_step_script() -> str:
    """The dump step's script, found by name rather than by position.

    This used to take the first step carrying a ``run:``, which was the
    dump step right up until the job grew steps in front of it — installing
    a matching pg_dump, checking the version it actually resolved. Then
    every assertion below quietly started reading `pip install` instead and
    passing on it, which is the failure mode this whole file exists to
    catch, arriving through its own front door.
    """
    for step in _workflow()["jobs"]["backup"]["steps"]:
        if step.get("name") == DUMP_STEP:
            return step["run"]
    raise AssertionError(
        f"no step named {DUMP_STEP!r} in the backup job — it was renamed or removed, "
        "and every contract in this file is about that step specifically"
    )


def test_the_backup_runs_daily():
    """Owner decision. Weekly meant up to seven days of collection could
    be lost to one bad restore point.

    The contract is *daily*, not one particular hour. This used to assert
    the literal string "0 3 * * *", which turned every legitimate retune of
    the hour into a red build — and it did, the first time the owner moved
    the job an hour earlier. The hour is an operational preference; the
    cadence is the thing that, if it silently changed, would cost up to a
    week of collection. So the cadence is what is pinned: exactly one
    schedule, every day of every month, at a fixed time of day.
    """
    # PyYAML parses the bare key `on` as the boolean True.
    schedules = _workflow()[True]["schedule"]

    assert len(schedules) == 1, f"expected exactly one schedule, got {schedules}"

    minute, hour, day_of_month, month, day_of_week = schedules[0]["cron"].split()

    assert (day_of_month, month, day_of_week) == ("*", "*", "*"), (
        f"the backup is no longer daily: {schedules[0]['cron']!r}. Restricting any of "
        "day-of-month, month or day-of-week means whole days go unbacked-up, which is "
        "the weekly cadence this job was moved off."
    )
    assert minute.isdigit() and hour.isdigit(), (
        f"the backup no longer runs once a day at a fixed time: {schedules[0]['cron']!r}. "
        "A wildcard or step in the minute or hour field runs the dump repeatedly, and "
        "each run briefly lifts FORCE ROW LEVEL SECURITY on eighteen tables."
    )


def test_a_missing_passphrase_fails_instead_of_uploading_plaintext():
    """The core contract. Falling back to an unencrypted upload would be
    worse than not running at all, because the run would report success."""
    script = _dump_step_script()

    assert "BACKUP_PASSPHRASE" in script
    assert "exit 1" in script
    assert "Refusing to upload an unencrypted dump" in script


def test_the_dump_is_encrypted_with_aes256():
    script = _dump_step_script()

    assert "--symmetric" in script
    assert "--cipher-algo AES256" in script


def test_the_passphrase_never_reaches_the_process_list():
    """--passphrase on the command line is readable by any other process
    on the runner; --passphrase-fd 0 with the value piped in is not."""
    script = _dump_step_script()

    assert "--passphrase-fd 0" in script
    assert "--passphrase " not in script


def test_the_plaintext_dump_is_removed_before_the_upload_step():
    script = _dump_step_script()

    assert "shred -u backup.dump" in script or "rm -f backup.dump" in script


def test_only_the_encrypted_file_is_uploaded():
    """Guards against the upload step being pointed back at backup.dump
    while every other assertion above still passes."""
    steps = _workflow()["jobs"]["backup"]["steps"]
    uploads = [step for step in steps if "upload-artifact" in str(step.get("uses", ""))]

    assert len(uploads) == 1
    assert uploads[0]["with"]["path"] == "backup.dump.gpg"


def test_the_workflow_declares_least_privilege():
    """CR-07 for this file: without an explicit block the job inherits
    whatever the repository default happens to be."""
    assert _workflow()["permissions"] == {"contents": "read"}


def test_backups_are_retained_for_a_month():
    """Rotation is GitHub's expiry rather than a script this project has
    to maintain — but the window still has to be long enough to notice a
    problem that started weeks ago."""
    steps = _workflow()["jobs"]["backup"]["steps"]
    upload = next(step for step in steps if "upload-artifact" in str(step.get("uses", "")))

    assert upload["with"]["retention-days"] == 30
