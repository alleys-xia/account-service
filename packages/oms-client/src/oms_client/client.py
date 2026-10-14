"""Strategy-facing OMS client. PubAck means durable transport receipt, never an order fill."""

import asyncio
from datetime import datetime

import nats
from nats.errors import TimeoutError as NatsTimeout
from oms_protocol.common import uid, utcnow
from oms_protocol.messages import OrderCommand, ProtocolError, RpcRequest, RpcResponse
from oms_protocol.orders import ExecutionEvent, OrderUpdate, ViewSnapshot
from oms_protocol.subjects import consumer_name, stream_name


class Delivery:
    def __init__(self, message):
        self.message = message
        import json

        data = json.loads(message.data)
        self.event = (
            OrderUpdate if data["kind"] == "order_update" else ExecutionEvent
        ).model_validate(data)

    async def ack(self):
        """Call only after the strategy's callback/checkpoint transaction commits."""
        await self.message.ack_sync(timeout=3)


class OmsClient:
    def __init__(
        self,
        strategy,
        *,
        account_id="default",
        environment="paper",
        run_id=None,
        nats_url="nats://127.0.0.1:4222",
        password,
        rpc_timeout=5,
        expected_mode="SIMULATION",
    ):
        self.identity = dict(
            strategy_instance_id=strategy, account_id=account_id, environment=environment
        )
        if expected_mode not in {"SIMULATION", "IB_PAPER"}:
            raise ValueError("unsupported execution domain")
        self.expected_mode = expected_mode
        self.run_id = run_id or uid()
        RpcRequest(**self.identity, operation="acquire", run_id=self.run_id)
        if environment != "paper":
            raise ValueError("OMS v1 supports only the paper simulation domain")
        self.url, self.password, self.timeout = nats_url, password, rpc_timeout
        self.nc = self.js = self.subscription = None
        self.session = None
        self.feed_epoch = None
        self.heartbeat = None
        self.failure = None
        self.closing = False
        self.prefix = f"{environment}.v1"
        self.subject = f"{self.prefix}.cmd.oms.{account_id}.{strategy}"
        self.rpc_subject = f"{self.prefix}.rpc.oms.{account_id}.{strategy}"

    @property
    def ready(self):
        return bool(
            not self.closing
            and self.failure is None
            and self.nc
            and self.nc.is_connected
            and self.session
            and datetime.fromisoformat(self.session["expires_at"]) > utcnow()
        )

    async def rpc(self, operation, **values):
        session = (
            dict(session_token=self.session["token"], session_epoch=self.session["epoch"])
            if self.session
            else {}
        )
        request = RpcRequest(**self.identity, **session, operation=operation, **values)
        response = await self.nc.request(
            self.rpc_subject, request.model_dump_json().encode(), timeout=self.timeout
        )
        result = RpcResponse.model_validate_json(response.data)
        if result.request_id != request.request_id:
            raise ProtocolError("RPC_CORRELATION_MISMATCH")
        if not result.ok:
            raise ProtocolError(result.error or "RPC_FAILED")
        return result.data

    async def connect(self):
        async def disconnected():
            if not self.closing:
                self.failure = ProtocolError("OMS_CONNECTION_LOST_RECOVERY_REQUIRED")

        try:
            self.nc = await asyncio.wait_for(
                nats.connect(
                    self.url,
                    user=self.identity["strategy_instance_id"],
                    password=self.password,
                    inbox_prefix=f"_INBOX.{self.identity['environment']}.{self.identity['strategy_instance_id']}.{self.run_id}",
                    disconnected_cb=disconnected,
                    connect_timeout=3,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=1,
                    pending_size=1048576,
                ),
                timeout=5,
            )
            self.js = self.nc.jetstream(timeout=self.timeout)
            result = await self.rpc("acquire", run_id=self.run_id)
            self.session, self.feed_epoch = result["session"], result["feed_epoch"]
            if result["mode"] != self.expected_mode or result["trade_ready"]:
                raise ProtocolError("UNSUPPORTED_EXECUTION_DOMAIN")
            self.session, self.feed_epoch = result["session"], result["feed_epoch"]
            self.subscription = await self.js.pull_subscribe_bind(
                stream=stream_name(self.identity["environment"], self.identity["account_id"]),
                consumer=consumer_name(
                    self.identity["environment"],
                    self.identity["account_id"],
                    self.identity["strategy_instance_id"],
                ),
                pending_msgs_limit=64,
                pending_bytes_limit=1048576,
            )
            self.heartbeat = asyncio.create_task(self.renew())
            return self
        except BaseException:
            await self.close()
            raise

    async def renew(self):
        try:
            while True:
                remaining = (
                    datetime.fromisoformat(self.session["expires_at"]) - utcnow()
                ).total_seconds()
                await asyncio.sleep(max(0.2, remaining / 3))
                self.session = (await self.rpc("renew"))["session"]
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Lease loss is terminal for this run; restart performs recovery before decisions.
            self.failure = error

    async def send(self, command):
        if not self.ready:
            raise ProtocolError("OMS_NOT_READY")
        if isinstance(command, dict):
            identity = command["identity"]
            if command["transport"] != "OMS" or any(
                (
                    identity["strategy_instance_id"] != self.identity["strategy_instance_id"],
                    identity["account_alias"] != self.identity["account_id"],
                    identity["environment"] != self.identity["environment"],
                )
            ):
                raise ProtocolError("COMMAND_IDENTITY_MISMATCH")
            operation, payload = command["operation"], command["payload"]
            fields = {"intent": payload} if operation == "submit" else payload
            command = OrderCommand(
                **self.identity,
                command_id=command["command_id"],
                client_order_id=command["client_order_id"],
                operation=operation,
                deadline_at=command["deadline_at"],
                input_id=command["input_id"],
                strategy_config_version=identity["config_version"],
                session_token=self.session["token"],
                session_epoch=self.session["epoch"],
                **fields,
            )
        else:
            command = OrderCommand.model_validate(command)
            if any(getattr(command, key) != value for key, value in self.identity.items()):
                raise ProtocolError("COMMAND_IDENTITY_MISMATCH")
            command = command.model_copy(
                update={
                    "session_token": self.session["token"],
                    "session_epoch": self.session["epoch"],
                }
            )
        # A renewed session must not cause JetStream to drop a previously rejected/undelivered send.
        # Application idempotency is authoritative; the transport dedup key includes the epoch.
        return await self.js.publish(
            self.subject,
            command.model_dump_json().encode(),
            headers={
                "Nats-Msg-Id": f"{command.command_id}:{command.session_epoch}:{command.business_hash()}"
            },
        )

    async def poll(self, *, timeout=0.05):
        if self.failure:
            raise self.failure
        try:
            messages = await self.subscription.fetch(1, timeout=timeout)
        except NatsTimeout:
            return None
        return Delivery(messages[0])

    async def snapshot(self):
        return ViewSnapshot.model_validate(await self.rpc("snapshot"))

    async def history(self, after_seq=0, *, limit=100):
        return await self.rpc("events", after_seq=after_seq, limit=limit)

    async def command_result(self, command_id):
        return await self.rpc("command", command_id=command_id)

    async def close(self):
        self.closing = True
        try:
            if self.heartbeat:
                self.heartbeat.cancel()
                await asyncio.gather(self.heartbeat, return_exceptions=True)
            if self.nc and self.nc.is_connected and self.session:
                try:
                    async with asyncio.timeout(min(2, self.timeout)):
                        await self.rpc("release")
                except Exception:
                    pass
        finally:
            if self.nc:
                await self.nc.close()
