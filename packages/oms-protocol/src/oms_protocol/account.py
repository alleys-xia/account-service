"""Privileged, read-only OMS account replay contract for the Risk service."""

from typing import Any, Literal

from pydantic import Field

from .common import Identifier, Token, uid
from .messages import Scoped


class AccountEvent(Scoped):
    feed_epoch: Token
    account_seq: int = Field(ge=1)
    event_id: Identifier
    payload: dict[str, Any]


class AccountRpcRequest(Scoped):
    request_id: Identifier = Field(default_factory=uid)
    operation: Literal["head", "events"]
    feed_epoch: Token | None = None
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
