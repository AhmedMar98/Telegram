"""Initialize the database — creates all tables + seeds a default account."""
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db
from app.logging_setup import setup_logging
from app.models import Account
from loguru import logger


def seed_default_account() -> None:
    """Create a default 'main' account if none exists."""
    with SessionLocal() as db:
        if db.query(Account).count() == 0:
            default = Account(
                name="main",
                api_id_enc="",  # will be populated when real credentials added
                api_hash_enc="",
                phone_enc="",
                is_active=True,
                is_main=True,
            )
            db.add(default)
            db.commit()
            logger.info("✅ Seeded default 'main' account (id=1)")


def main() -> None:
    setup_logging()
    logger.info("Initializing database...")
    init_db()
    seed_default_account()
    logger.info("✅ Database initialized successfully")
    logger.info("   Tables: accounts, channels, links, link_embeddings,")
    logger.info("           classification_logs, vitality_checks, llm_provider_state, audit_events")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Copy .env.example to .env and fill in credentials")
    logger.info("  2. Run: python scripts/run_worker.py collect")
    logger.info("  3. Run: python scripts/run_worker.py classify")
    logger.info("  4. Run: python scripts/run_web.py")
    logger.info("  Or run everything together: python scripts/run_all.py")


if __name__ == "__main__":
    main()
