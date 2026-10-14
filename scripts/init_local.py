"""Create a private local account-service credential file; never overwrite existing secrets."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    keys = (
        "OMS_NATS_PASSWORD",
        "RISK_NATS_PASSWORD",
        "PORTFOLIO_NATS_PASSWORD",
        "EXECUTION_NATS_PASSWORD",
        "PAPER_ALPHA_PASSWORD",
        "PAPER_OPERATOR_PASSWORD",
    )
    content = "".join(f"export {key}={secrets.token_urlsafe(32)}\n" for key in keys)
    content += "export EXECUTION_NATS_LISTEN=127.0.0.1:4223\n"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Existing .env preserved.")
        return
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    print(
        "Created private .env (0600). Prepare separate OMS/Risk/Portfolio databases before account-runtime."
    )


if __name__ == "__main__":
    main()
