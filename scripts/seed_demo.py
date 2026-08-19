"""Populate a workspace with realistic sample data.

Useful for seeing the whole pipeline work — classification, search,
stats — before any Telegram credentials exist, and for trying UI changes
against something that looks like real content.

    python scripts/seed_demo.py --email demo@example.com --password demo-pass-123

Creates the account if it does not exist, then ingests sample messages
through the same code path the real collector uses.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.ingest import IngestSummary, get_or_create_channel, ingest_text  # noqa: E402
from app.models import User, Workspace  # noqa: E402
from app.security import hash_password, normalize_email  # noqa: E402

logger = logging.getLogger("seed")

SAMPLE_CHANNELS: dict[str, list[str]] = {
    "أفلام ومسلسلات": [
        "فيلم الأكشن الجديد بجودة عالية https://example.com/action-2024.mkv استمتعوا!",
        "الحلقة الأولى من المسلسل https://example.com/series-s01e01.mp4، والباقي قريباً",
        "مراجعة الفيلم على https://www.imdb.com/title/tt1234567",
        "الإعلان الرسمي https://www.youtube.com/watch?v=trailer2024.",
    ],
    "تطبيقات وبرامج": [
        "تطبيق تعديل الصور https://example.com/photo-editor.apk مجاناً",
        "برنامج المونتاج للويندوز https://example.com/video-editor.exe",
        "مشروع مفتوح المصدر رائع https://github.com/example/awesome-tool",
        "نسخة الماك https://example.com/tool-mac.dmg، جربوها",
    ],
    "كتب ودورات": [
        "كتاب تعلم بايثون من الصفر https://example.com/python-basics.pdf",
        "دورة FastAPI الكاملة https://www.udemy.com/course/fastapi-complete",
        "كورس مجاني على https://www.coursera.org/learn/machine-learning",
        "رواية مترجمة https://example.com/novel.epub!",
    ],
    "موسيقى وألعاب": [
        "ألبوم جديد https://open.spotify.com/album/xyz123",
        "أغنية رائعة https://soundcloud.com/artist/track",
        "اللعبة متاحة الآن https://store.steampowered.com/app/999999",
        "تحديث اللعبة https://example.com/game-patch.exe",
    ],
}


def seed(email: str, password: str, workspace_name: str) -> None:
    email = normalize_email(email)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            workspace = Workspace(name=workspace_name)
            db.add(workspace)
            db.flush()
            user = User(
                workspace_id=workspace.id,
                email=email,
                password_hash=hash_password(password),
                role="owner",
            )
            db.add(user)
            db.flush()
            logger.info("created account %s (workspace id %d)", email, workspace.id)
        else:
            logger.info("reusing existing account %s (workspace id %d)", email, user.workspace_id)

        workspace_id = user.workspace_id
        summary = IngestSummary()
        for index, (title, messages) in enumerate(SAMPLE_CHANNELS.items(), start=1):
            channel = get_or_create_channel(
                db, workspace_id=workspace_id, tg_channel_id=f"demo:{index}", title=title
            )
            for message_id, text in enumerate(messages, start=1):
                ingest_text(
                    db,
                    workspace_id=workspace_id,
                    channel_id=channel.id,
                    text=text,
                    message_id=message_id,
                    summary=summary,
                )
        db.commit()

        logger.info(
            "seeded %d link(s) across %d channel(s) (%d already present)",
            summary.stored,
            len(SAMPLE_CHANNELS),
            summary.duplicates,
        )
        logger.info("workspace id for COLLECTOR_WORKSPACE_ID: %d", workspace_id)
        logger.info("sign in at /login with %s", email)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", default="demo-pass-123")
    parser.add_argument("--workspace-name", default="مساحة تجريبية")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    seed(args.email, args.password, args.workspace_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
