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


def _dump_step_script() -> str:
    for step in _workflow()["jobs"]["backup"]["steps"]:
        if "run" in step:
            return step["run"]
    raise AssertionError("no run step found in the backup job")


def test_the_backup_runs_daily():
    """Owner decision. Weekly meant up to seven days of collection could
    be lost to one bad restore point."""
    # PyYAML parses the bare key `on` as the boolean True.
    schedules = _workflow()[True]["schedule"]

    assert [entry["cron"] for entry in schedules] == ["0 3 * * *"]


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
