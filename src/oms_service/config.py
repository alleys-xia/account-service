import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from oms_protocol.common import Token
from oms_protocol.orders import FrozenModel
from pydantic import Field, SecretStr, model_validator


class Settings(FrozenModel):
    environment: Literal["paper"] = "paper"
    account_id: Token = "default"
    mode: Literal["SIMULATION"] = "SIMULATION"
    database_url: SecretStr = SecretStr("postgresql://localhost/oms_dev")
    nats_url: str = "nats://127.0.0.1:4222"
    nats_user: Token = "oms"
    nats_password: SecretStr
    strategies: tuple[Token, ...] = ("alpha", "beta")
    session_ttl: float = Field(default=30, ge=5, le=300)
    max_outbox: int = Field(default=10000, ge=100, le=1000000)
    stream_max_bytes: int = Field(default=134217728, ge=1048576)
    rpc_timeout: float = Field(default=5, gt=0, le=30)

    @model_validator(mode="after")
    def valid(self):
        parsed = urlparse(self.nats_url)
        if (
            parsed.scheme not in {"nats", "tls"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("NATS URL must not contain credentials")
        if not self.strategies or len(set(self.strategies)) != len(self.strategies):
            raise ValueError("strategies must be nonempty and unique")
        return self

    @property
    def prefix(self):
        return f"{self.environment}.v1"

    @property
    def stream(self):
        return f"OMS_{self.environment}_{self.account_id}"


def load_settings(path):
    values = tomllib.loads(Path(path).read_text())
    for field in Settings.model_fields:
        name = "OMS_" + field.upper()
        if name in os.environ:
            if field == "strategies":
                values[field] = tuple(x.strip() for x in os.environ[name].split(","))
            else:
                values[field] = os.environ[name]
    return Settings.model_validate(values)
