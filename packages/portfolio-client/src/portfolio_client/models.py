from typing import Literal

from oms_protocol.common import Token, uid
from oms_protocol.messages import Scoped
from oms_protocol.orders import Amount, FrozenModel
from pydantic import AwareDatetime, Field, model_validator


class Request(Scoped):
    request_id: Token = Field(default_factory=uid)
    mode: Literal["SIMULATION", "IB_PAPER"] = "SIMULATION"
    operation: Literal["snapshot"] = "snapshot"


class BrokerPosition(FrozenModel):
    con_id: int = Field(gt=0)
    symbol: str
    sec_type: str
    currency: str
    quantity: Amount
    average_cost: Amount


class Mapping(FrozenModel):
    instrument_id: Token
    instrument_version: int = Field(ge=1)
    con_id: int = Field(gt=0)
    multiplier: Amount = Field(gt=0)


class ManagedPosition(Mapping):
    quantity: Amount


class AccountValue(FrozenModel):
    tag: str
    currency: str
    value: Amount


class BrokerSnapshot(Scoped):
    mode: Literal["SIMULATION", "IB_PAPER"]
    snapshot_id: Token
    source_session: Token
    started_at: AwareDatetime
    completed_at: AwareDatetime
    complete: bool
    stable: bool
    feed_epoch: Token
    positions: tuple[BrokerPosition, ...]
    catalog: tuple[Mapping, ...]
    account_values: tuple[AccountValue, ...]
    # None is a legacy producer without an independent owned-execution projection.
    managed_positions: tuple[ManagedPosition, ...] | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.completed_at < self.started_at:
            raise ValueError("invalid acquisition window")
        for values in (
            [x.con_id for x in self.positions],
            [x.con_id for x in self.catalog],
            [x.instrument_id for x in self.catalog],
            [(x.tag, x.currency) for x in self.account_values],
            [x.con_id for x in self.managed_positions or ()],
            [x.instrument_id for x in self.managed_positions or ()],
        ):
            if len(set(values)) != len(values):
                raise ValueError("duplicate snapshot identity")
        catalog = {x.instrument_id: x for x in self.catalog}
        for position in self.managed_positions or ():
            reference = catalog.get(position.instrument_id)
            if reference is None or any(
                getattr(reference, key) != getattr(position, key)
                for key in ("con_id", "instrument_version", "multiplier")
            ):
                raise ValueError("managed position does not match broker catalog")
        return self
