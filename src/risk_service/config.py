"""Operator-owned policy/catalog configuration; never trust strategy-supplied multipliers."""

import os
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from oms_protocol.common import Token, digest
from oms_protocol.futures import FUTURES, RESTRICTED_FUTURES, UnderlyingType
from oms_protocol.orders import Amount, FrozenModel
from pydantic import AwareDatetime, Field, SecretStr, model_validator


class Limits(FrozenModel):
    halted: bool = False
    max_order_cost: Amount = Field(default=Decimal("5000"), gt=0)
    max_committed: Amount = Field(default=Decimal("10000"), gt=0)
    max_open_orders: int = Field(default=20, ge=1)
    max_stock_quantity: int = Field(default=1000, ge=1)
    max_option_quantity: int = Field(default=10, ge=1)
    max_future_quantity: int = Field(default=1, ge=1)
    max_requests_per_minute: int = Field(default=60, ge=1)


class Instrument(FrozenModel):
    version: int = Field(default=1, ge=1)
    asset_type: Literal["STOCK", "OPTION", "FUTURE"]
    multiplier: Amount = Field(gt=0)
    currency: Literal["USD", "HKD"] = "USD"
    future_contract_budget: Amount | None = Field(default=None, gt=0)
    price_increment: Amount | None = Field(default=None, gt=0)
    entry_cutoff_at: AwareDatetime | None = None
    stock_short_budget_per_share: Amount | None = Field(default=None, gt=0)
    underlying: str | None = None
    expiry: date | None = None
    right: Literal["CALL", "PUT"] | None = None
    strike: Amount | None = Field(default=None, gt=0)
    underlying_type: UnderlyingType = "EQUITY"
    settlement: Literal["PHYSICAL", "CASH"] = "PHYSICAL"
    exercise_style: Literal["AMERICAN", "EUROPEAN"] = "AMERICAN"
    standard: Literal[True] = True

    @model_validator(mode="after")
    def valid(self):
        if self.asset_type != "STOCK" and self.stock_short_budget_per_share is not None:
            raise ValueError("short share budgets apply to STOCK only")
        if self.asset_type == "STOCK":
            if self.multiplier != 1 or any(
                x is not None for x in (self.expiry, self.right, self.strike)
            ):
                raise ValueError("stock requires multiplier 1 and no option fields")
        elif self.asset_type == "FUTURE":
            product = FUTURES.get(self.underlying)
            if (
                product is None
                or self.currency != product.currency
                or self.multiplier != product.multiplier
                or (self.underlying != "MES" and self.future_contract_budget is None)
                or self.expiry is None
                or self.right is not None
                or self.strike is not None
                or self.underlying_type != product.underlying_type
                or self.settlement != product.settlement
                or (self.price_increment is not None and self.price_increment != product.tick)
                or (self.underlying in RESTRICTED_FUTURES and self.price_increment is None)
            ):
                raise ValueError("invalid supported futures catalog entry")
            if self.entry_cutoff_at is not None and self.entry_cutoff_at.date() >= self.expiry:
                raise ValueError("entry cutoff must precede expiry date")
        elif self.multiplier != 100 or any(
            x is None for x in (self.underlying, self.expiry, self.right, self.strike)
        ):
            raise ValueError("v1 supports configured standard multiplier-100 US options only")
        if self.asset_type == "OPTION" and (
            self.underlying_type not in {"EQUITY", "INDEX"}
            or (self.settlement, self.exercise_style)
            != (
                ("CASH", "EUROPEAN")
                if self.underlying_type == "INDEX"
                else ("PHYSICAL", "AMERICAN")
            )
        ):
            raise ValueError("unsupported option settlement/exercise combination")
        if self.asset_type != "FUTURE" and (
            self.currency != "USD" or self.future_contract_budget is not None
        ):
            raise ValueError("stock/options require USD")
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
    min_future_dte: int = Field(default=1, ge=1)
    account_by_currency: dict[Literal["HKD"], Limits] = {}
    strategies_by_currency: dict[Token, dict[Literal["HKD"], Limits]] = {}
    fee_buffers_by_currency: dict[Literal["HKD"], Amount] = {}
    min_option_dte: int = Field(default=2, ge=1)
    fee_buffer_per_unit_per_leg: Amount = Field(default=Decimal("1"), ge=0)
    max_outbox: int = Field(default=10000, ge=100)
    account: Limits = Field(default_factory=Limits)
    strategies: dict[Token, Limits]
    instruments: dict[Token, Instrument]
    stock_short_allowlist: dict[Token, tuple[Token, ...]] = {}
    paper_signal_only_allowlist: dict[Token, tuple[Token, ...]] = {}
    paper_market_allowlist: dict[Token, tuple[Token, ...]] = {}
    extra_rules: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self):
        url = urlparse(self.nats_url)
        if url.scheme not in {"nats", "tls"} or not url.hostname or url.username or url.password:
            raise ValueError("NATS URL must not contain credentials")
        if any(v < 0 for v in self.fee_buffers_by_currency.values()):
            raise ValueError("fee buffers must be nonnegative")
        if not self.strategies or not self.instruments:
            raise ValueError("explicit strategy limits and instrument catalog required")
        if self.paper_market_allowlist and (self.mode != "IB_PAPER" or not self.require_portfolio):
            raise ValueError(
                "paper market admission requires IB_PAPER and Portfolio reconciliation"
            )
        for strategy, names in self.paper_market_allowlist.items():
            if strategy not in self.strategies or not names or len(set(names)) != len(names):
                raise ValueError(
                    "paper market allowlist requires a known strategy and unique instruments"
                )
            for name in names:
                ref = self.instruments.get(name)
                if (
                    ref is None
                    or ref.asset_type != "FUTURE"
                    or ref.underlying not in RESTRICTED_FUTURES | {"MHI"}
                ):
                    raise ValueError(
                        "paper market admission requires a supported explicit futures contract"
                    )
        if self.paper_signal_only_allowlist and (
            self.mode != "IB_PAPER" or not self.require_portfolio
        ):
            raise ValueError("signal-only admission requires IB_PAPER and Portfolio reconciliation")
        for strategy, names in self.paper_signal_only_allowlist.items():
            if strategy not in self.strategies or not names or len(set(names)) != len(names):
                raise ValueError(
                    "signal-only allowlist requires a known strategy and unique instruments"
                )
            for name in names:
                ref = self.instruments.get(name)
                if ref is None or ref.asset_type != "FUTURE" or ref.underlying != "MES":
                    raise ValueError("signal-only admission supports configured MES contracts only")
        for strategy, names in self.stock_short_allowlist.items():
            if strategy not in self.strategies or len(set(names)) != len(names):
                raise ValueError("short allowlist requires a known strategy and unique instruments")
            for name in names:
                ref = self.instruments.get(name)
                if (
                    ref is None
                    or ref.asset_type != "STOCK"
                    or ref.stock_short_budget_per_share is None
                ):
                    raise ValueError(
                        "short allowlist requires a stock catalog entry and share budget"
                    )
        return self

    @property
    def prefix(self):
        return f"{self.environment}.v1"

    @property
    def stream(self):
        return f"OMS_{self.environment}_{self.account_id}"

    @property
    def recovery_allowlist(self):
        scopes = dict(self.paper_signal_only_allowlist)
        for strategy, names in self.paper_market_allowlist.items():
            scopes[strategy] = scopes.get(strategy, ()) + names
        return scopes

    @property
    def policy_hash(self):
        return digest(
            self.model_dump(
                mode="json",
                include={
                    "account",
                    "account_by_currency",
                    "strategies_by_currency",
                    "fee_buffers_by_currency",
                    "min_future_dte",
                    "strategies",
                    "instruments",
                    "stock_short_allowlist",
                    "paper_signal_only_allowlist",
                    "paper_market_allowlist",
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
