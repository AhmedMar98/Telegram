"""Generate AES-256 + HMAC keys for .env setup."""
import secrets


def main() -> None:
    print("# Add these to your .env file:\n")
    print(f"AES_ENCRYPTION_KEY={secrets.token_hex(32)}")
    print(f"HMAC_KEY={secrets.token_hex(32)}")
    print(f"APP_SECRET_KEY={secrets.token_hex(32)}")


if __name__ == "__main__":
    main()
