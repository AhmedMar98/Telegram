"""Create a new tenant (SaaS multi-tenant setup)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_setup import setup_logging
from app.tenant import create_tenant


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new SaaS tenant")
    parser.add_argument("--name", required=True, help="Display name (e.g. 'Acme Corp')")
    parser.add_argument("--slug", required=True, help="URL-safe slug (e.g. 'acme')")
    parser.add_argument("--plan", default="free", choices=["free", "pro", "enterprise"])
    args = parser.parse_args()

    setup_logging()
    try:
        tenant, api_key = create_tenant(name=args.name, slug=args.slug, plan=args.plan)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"✅ Tenant created: {tenant.name} (id={tenant.id}, slug={tenant.slug})")
    print(f"   Plan: {tenant.plan}")
    print("=" * 60)
    print("\n⚠️  Save this API key now — it will NOT be shown again:\n")
    print(f"    {api_key}")
    print("\n" + "=" * 60)
    print("\nUsage:")
    print(f"  curl -H 'X-API-Key: {api_key[:20]}...' \\")
    print("       https://your-host/api/v1/links")
    print("\nTo activate SaaS mode (require API key on every request):")
    print("  echo 'SAAS_MODE=true' >> .env")


if __name__ == "__main__":
    main()
