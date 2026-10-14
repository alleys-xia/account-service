"""Small read-only SDK, with a strategy-scoped endpoint enforced by NATS ACL."""

import nats
from oms_protocol.common import Token
from oms_protocol.messages import ProtocolError, RpcResponse
from oms_protocol.orders import ViewSnapshot
from pydantic import TypeAdapter

from .models import Request


class PortfolioClient:
    def __init__(
        self,
        strategy,
        *,
        account_id="default",
        environment="paper",
        expected_mode="SIMULATION",
        nats_url="nats://127.0.0.1:4222",
        password,
        rpc_timeout=5,
    ):
        for value in (strategy, account_id, environment):
            TypeAdapter(Token).validate_python(value)
        self.strategy, self.account_id, self.environment = strategy, account_id, environment
        self.mode, self.url, self.password, self.timeout = (
            expected_mode,
            nats_url,
            password,
            rpc_timeout,
        )
        self.nc = None

    async def connect(self):
        self.nc = await nats.connect(
            self.url,
            user=self.strategy,
            password=self.password,
            inbox_prefix=f"_INBOX.{self.environment}.{self.strategy}",
            max_reconnect_attempts=0,
            connect_timeout=3,
        )
        return self

    async def snapshot(self):
        request = Request(account_id=self.account_id, environment=self.environment, mode=self.mode)
        message = await self.nc.request(
            f"{self.environment}.v1.rpc.portfolio.{self.account_id}.strategy.{self.strategy}",
            request.model_dump_json().encode(),
            timeout=self.timeout,
        )
        response = RpcResponse.model_validate_json(message.data)
        if response.request_id != request.request_id:
            raise ProtocolError("RPC_CORRELATION_MISMATCH")
        if not response.ok:
            raise ProtocolError(response.error or "PORTFOLIO_UNAVAILABLE")
        data = response.data
        if (data.get("mode"), data.get("account_id"), data.get("strategy")) != (
            self.mode,
            self.account_id,
            self.strategy,
        ):
            raise ProtocolError("PORTFOLIO_SCOPE_MISMATCH")
        return ViewSnapshot.model_validate(data["snapshot"])

    async def close(self):
        if self.nc:
            await self.nc.close()
            self.nc = None
