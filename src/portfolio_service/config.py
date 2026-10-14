import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from oms_protocol.common import Token
from oms_protocol.orders import Amount, FrozenModel
from pydantic import Field, SecretStr, model_validator


class Instrument(FrozenModel):
    asset_type: Literal["STOCK", "OPTION"]
    version: int = Field(default=1, ge=1)
    multiplier: Amount = Field(gt=0)

    @model_validator(mode="after")
    def standard(self):
        if self.multiplier != (1 if self.asset_type == "STOCK" else 100):
            raise ValueError("standard stock/option multipliers only")
        return self


class Settings(FrozenModel):
    environment: Literal["paper"] = "paper"
    mode: Literal["SIMULATION", "IB_PAPER"] = "SIMULATION"
    account_id: Token = "default"
    database_url: SecretStr = SecretStr("postgresql://localhost/portfolio_dev")
    nats_url: str = "nats://127.0.0.1:4222"
    nats_user: Token = "portfolio"
    nats_password: SecretStr
    strategies: tuple[Token, ...] = ("alpha",)
    instruments: dict[Token, Instrument] = {}
    rpc_timeout: float = Field(default=5, gt=0, le=60)
    poll_interval: float = Field(default=1, ge=0.1, le=30)
    broker_interval: float = Field(default=30, ge=5, le=300)
    broker_timeout: float = Field(default=45, ge=2, le=120)
    max_snapshot_age: float = Field(default=90, ge=5, le=600)
    max_positions: int = Field(default=1000, ge=1, le=5000)

    @model_validator(mode="after")
    def valid(self):
        url = urlparse(self.nats_url)
        if url.scheme not in {"nats", "tls"} or not url.hostname or url.username or url.password:
            raise ValueError("NATS URL must not contain credentials")
        if not self.strategies or len(set(self.strategies)) != len(self.strategies):
            raise ValueError("explicit unique strategy identities required")
        return self

    @property
    def prefix(self):
        return f"{self.environment}.v1"


def load_settings(path):
    values = tomllib.loads(Path(path).read_text())
    if "catalog_file" in values:
        catalog = tomllib.loads((Path(path).parent / values.pop("catalog_file")).read_text())[
            "instruments"
        ]
        values["instruments"] = {
            name: {k: v for k, v in ref.items() if k in Instrument.model_fields}
            for name, ref in catalog.items()
        }
    for field in ("database_url", "nats_url", "nats_user", "nats_password"):
        if "PORTFOLIO_" + field.upper() in os.environ:
            values[field] = os.environ["PORTFOLIO_" + field.upper()]
    return Settings.model_validate(values)
