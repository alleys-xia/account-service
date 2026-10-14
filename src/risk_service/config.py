"""Operator-owned policy/catalog configuration; never trust strategy-supplied multipliers."""

import os
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from oms_protocol.common import Token, digest
from oms_protocol.orders import Amount, FrozenModel
from pydantic import Field, SecretStr, model_validator


class Limits(FrozenModel):
    halted: bool = False
    max_order_cost: Amount = Field(default=Decimal("5000"), gt=0)
    max_committed: Amount = Field(default=Decimal("10000"), gt=0)
    max_open_orders: int = Field(default=20, ge=1)
    max_stock_quantity: int = Field(default=1000, ge=1)
    max_option_quantity: int = Field(default=10, ge=1)
    max_requests_per_minute: int = Field(default=60, ge=1)


class Instrument(FrozenModel):
    version: int = Field(default=1, ge=1)
    asset_type: Literal["STOCK", "OPTION"]
    multiplier: Amount = Field(gt=0)
    underlying: str | None = None
    expiry: date | None = None
    right: Literal["CALL", "PUT"] | None = None
    strike: Amount | None = Field(default=None, gt=0)
    underlying_type: Literal["EQUITY", "INDEX"] = "EQUITY"
    settlement: Literal["PHYSICAL", "CASH"] = "PHYSICAL"
    exercise_style: Literal["AMERICAN", "EUROPEAN"] = "AMERICAN"
    standard: Literal[True] = True

    @model_validator(mode="after")
    def valid(self):
        if self.asset_type == "STOCK":
            if self.multiplier != 1 or any(
                x is not None for x in (self.expiry, self.right, self.strike)
            ):
                raise ValueError("stock requires multiplier 1 and no option fields")
        elif self.multiplier != 100 or any(
            x is None for x in (self.underlying, self.expiry, self.right, self.strike)
        ):
            raise ValueError("v1 supports configured standard multiplier-100 US options only")
        if self.asset_type == "OPTION" and (self.settlement, self.exercise_style) != (
            ("CASH", "EUROPEAN") if self.underlying_type == "INDEX" else ("PHYSICAL", "AMERICAN")
        ):
            raise ValueError("unsupported option settlement/exercise combination")
        return self


class Settings(FrozenModel):
    environment: Literal["paper"] = "paper"
    account_id: Token = "default"
    mode: Literal["SIMULATION", "IB_PAPER"] = "SIMULATION"
    database_url: SecretStr = SecretStr("postgresql://localhost/risk_dev")
    nats_url: str = "nats://127.0.0.1:4222"
    nats_user: Token = "risk"
    nats_password: SecretStr
    rpc_timeout: float = Field(default=5, gt=0, le=30)
    poll_interval: float = Field(default=0.5, ge=0.1, le=10)
    require_portfolio: bool = False
    approval_seconds: int = Field(default=5, ge=1, le=30)
    max_decision_age: int = Field(default=30, ge=1, le=300)
    min_option_dte: int = Field(default=2, ge=1)
    fee_buffer_per_unit_per_leg: Amount = Field(default=Decimal("1"), ge=0)
    max_outbox: int = Field(default=10000, ge=100)
    account: Limits = Field(default_factory=Limits)
    strategies: dict[Token, Limits]
    instruments: dict[Token, Instrument]
    extra_rules: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self):
        url = urlparse(self.nats_url)
        if url.scheme not in {"nats", "tls"} or not url.hostname or url.username or url.password:
            raise ValueError("NATS URL must not contain credentials")
        if not self.strategies or not self.instruments:
            raise ValueError("explicit strategy limits and instrument catalog required")
        return self

    @property
    def prefix(self):
        return f"{self.environment}.v1"

    @property
    def stream(self):
        return f"OMS_{self.environment}_{self.account_id}"

    @property
    def policy_hash(self):
        return digest(
            self.model_dump(
                mode="json",
                include={
                    "account",
                    "strategies",
                    "instruments",
                    "extra_rules",
                    "max_decision_age",
                    "min_option_dte",
                    "fee_buffer_per_unit_per_leg",
                },
            )
        )


def load_settings(path):
    values = tomllib.loads(Path(path).read_text())
    for field in ("database_url", "nats_url", "nats_user", "nats_password"):
        name = "RISK_" + field.upper()
        if name in os.environ:
            values[field] = os.environ[name]
    return Settings.model_validate(values)
