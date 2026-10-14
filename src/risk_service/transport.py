"""OMS-backed account replay before admission; JetStream requests and durable decisions."""

import asyncio

import nats
from nats.errors import TimeoutError as NatsTimeout
from oms_protocol.account import AccountEvent, AccountRpcRequest
from oms_protocol.common import encode, utcnow
from oms_protocol.messages import ProtocolError, RiskRequest, RpcResponse
from portfolio_client.models import Request
from pydantic import ValidationError


class Transport:
    def __init__(self, service):
        self.service = service
        self.settings, self.store = service.settings, service.store
        self.nc = self.js = None
        self.tasks = []
        self.gate = asyncio.Lock()
        self.failure = asyncio.get_running_loop().create_future()
        self.last_sync = None
        self.closing = False

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)

        def done(value):
            if not value.cancelled() and value.exception() is not None and not self.failure.done():
                self.failure.set_result(value.exception())

        task.add_done_callback(done)

    async def rpc(self, operation, **fields):
        s = self.settings
        request = AccountRpcRequest(
            environment=s.environment, account_id=s.account_id, operation=operation, **fields
        )
        response = RpcResponse.model_validate_json(
            (
                await self.nc.request(
                    f"{s.prefix}.rpc.oms-risk.{s.account_id}",
                    request.model_dump_json().encode(),
                    timeout=s.rpc_timeout,
                )
            ).data
        )
        if response.request_id != request.request_id:
            raise ProtocolError("RPC_CORRELATION_MISMATCH")
        if not response.ok:
            raise ProtocolError(response.error or "OMS_UNAVAILABLE")
        if response.data["mode"] != self.settings.mode or response.data["trade_ready"]:
            raise ProtocolError("UNSUPPORTED_EXECUTION_DOMAIN")
        return response.data

    async def catchup(self):
        # Gate held across replay + admission by the request worker. The OMS response head
        # and its events are read in one OMS transaction; every approval catches up anew.
        head = await self.rpc("head")
        epoch, target = head["feed_epoch"], head["account_seq"]
        await self.service.bind_head(epoch, target, head["blocked"])
        _, seq = await self.service.cursor()
        while seq < target:
            batch = await self.rpc("events", feed_epoch=epoch, after_seq=seq, limit=100)
            await self.service.bind_head(
                batch["feed_epoch"], batch["account_seq"], batch["blocked"]
            )
            if not batch["events"]:
                raise ProtocolError("ACCOUNT_REPLAY_INCOMPLETE")
            for value in batch["events"]:
                event = AccountEvent.model_validate(value)
                await self.service.apply(event)
                seq = event.account_seq
                if seq >= target:
                    break
        self.last_sync = utcnow()

    async def start(self):
        s = self.settings

        async def disconnected():
            if not self.closing and not self.failure.done():
                self.failure.set_result(ProtocolError("NATS_DISCONNECTED_RECOVERY_REQUIRED"))

        self.nc = await asyncio.wait_for(
            nats.connect(
                s.nats_url,
                user=s.nats_user,
                password=s.nats_password.get_secret_value(),
                inbox_prefix=f"_INBOX.{s.environment}.risk",
                connect_timeout=3,
                max_reconnect_attempts=0,
                disconnected_cb=disconnected,
                pending_size=1048576,
            ),
            timeout=5,
        )
        self.js = self.nc.jetstream(timeout=s.rpc_timeout)
        async with self.gate:
            await self.catchup()
        subscription = await self.js.pull_subscribe_bind(
            stream=s.stream,
            consumer="risk_checks",
            pending_msgs_limit=16,
            pending_bytes_limit=1048576,
        )
        await self.nc.subscribe(
            f"{s.prefix}.rpc.risk.{s.account_id}.health",
            cb=self.health,
            pending_msgs_limit=32,
            pending_bytes_limit=65536,
        )
        await self.nc.flush()
        self.spawn(self.consume(subscription))
        self.spawn(self.replay())
        self.spawn(self.publish())
        return self

    async def consume(self, subscription):
        while True:
            try:
                messages = await subscription.fetch(1, timeout=1)
            except NatsTimeout:
                continue
            for message in messages:
                try:
                    request = RiskRequest.model_validate_json(message.data)
                except ValidationError:
                    async with self.store.transaction():
                        await self.store.set_meta("blocked", "INVALID_RISK_REQUEST")
                    raise ProtocolError("INVALID_RISK_REQUEST") from None
                async with self.gate:
                    await self.catchup()
                    hold = (
                        await self.portfolio_hold(request)
                        if self.settings.require_portfolio
                        or request.intent.close_position_id is not None
                        else None
                    )
                    await self.service.decide(request, external_hold=hold)
                # Durable reservation, decision and outbox already committed.
                await message.ack_sync()

    async def portfolio_hold(self, risk_request):
        s = self.settings
        request = Request(account_id=s.account_id, environment=s.environment, mode=s.mode)
        try:
            raw = await self.nc.request(
                f"{s.prefix}.rpc.portfolio.{s.account_id}.account",
                request.model_dump_json().encode(),
                timeout=s.rpc_timeout,
            )
            response = RpcResponse.model_validate_json(raw.data)
            data = response.data
            if (
                response.request_id != request.request_id
                or not response.ok
                or (data.get("account_id"), data.get("mode"), data.get("feed_epoch"))
                != (s.account_id, s.mode, risk_request.feed_epoch)
            ):
                return "PORTFOLIO_DOMAIN_OR_RESPONSE_INVALID"
            _, risk_seq = await self.service.cursor()
            if not data.get("safe_for_new_risk") or data.get("applied_account_seq", -1) < max(
                risk_seq, risk_request.required_account_seq
            ):
                return "PORTFOLIO_NOT_READY"
            return None
        except (nats.errors.Error, ValueError, TimeoutError):
            return "PORTFOLIO_UNAVAILABLE"

    async def replay(self):
        while True:
            await asyncio.sleep(self.settings.poll_interval)
            async with self.gate:
                await self.catchup()

    async def publish(self):
        while True:
            rows = await self.store.pending()
            for row in rows:
                await self.js.publish(
                    row["subject"],
                    encode(row["payload"]).encode(),
                    headers={"Nats-Msg-Id": row["message_id"]},
                )
                await self.store.published(row["seq"])
            await asyncio.sleep(0.02 if rows else 0.1)

    async def health(self, message):
        try:
            data = await self.service.health()
            recent = (
                self.last_sync
                and (utcnow() - self.last_sync).total_seconds()
                <= self.settings.rpc_timeout + self.settings.poll_interval
            )
            data.update(
                last_sync=self.last_sync.isoformat() if self.last_sync else None,
                simulation_ready=bool(
                    recent
                    and self.nc.is_connected
                    and not self.failure.done()
                    and not data["blocked"]
                    and not data["legs_unsettled"]
                    and not data["account_halted"]
                ),
            )
            if message.reply:
                await message.respond(encode(data).encode())
        except Exception as error:
            if not self.failure.done():
                self.failure.set_result(error)

    async def close(self):
        self.closing = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.nc:
            await self.nc.close()
