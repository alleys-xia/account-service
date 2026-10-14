"""Create a private local OMS credential file; never overwrite existing secrets."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    content = (
        "export OMS_NATS_PASSWORD=" + secrets.token_urlsafe(32) + "\n"
        "export OMS_DATABASE_URL=postgresql://localhost/oms_dev\n"
    )
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Existing .env preserved.")
        return
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    print(
        "Created private .env (0600). Create a dedicated oms_dev PostgreSQL database before starting OMS."
    )


if __name__ == "__main__":
    main()
