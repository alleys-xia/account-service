"""Replay committed OMS events; expose bounded strategy views and privileged account views."""

import asyncio

import nats
from oms_protocol.account import AccountEvent, AccountRpcRequest
from oms_protocol.common import encode, utcnow
from oms_protocol.messages import ProtocolError, RpcResponse
from portfolio_client.models import BrokerSnapshot, Request


class Transport:
    def __init__(self, service):
        self.service, self.settings, self.store = service, service.settings, service.store
        self.nc = None
        self.tasks = []
        self.gate = asyncio.Lock()
        self.failure = asyncio.get_running_loop().create_future()
        self.closing = False

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)

        def done(t):
            if not t.cancelled() and t.exception() and not self.failure.done():
                self.failure.set_result(t.exception())

        task.add_done_callback(done)

    async def request(self, subject, request, timeout):
        raw = await self.nc.request(subject, request.model_dump_json().encode(), timeout=timeout)
        response = RpcResponse.model_validate_json(raw.data)
        if response.request_id != request.request_id:
            raise ProtocolError("RPC_CORRELATION_MISMATCH")
        if not response.ok:
            raise ProtocolError(response.error or "SOURCE_UNAVAILABLE")
        return response.data

    async def oms(self, operation, **fields):
        s = self.settings
        value = await self.request(
            f"{s.prefix}.rpc.oms-portfolio.{s.account_id}",
            AccountRpcRequest(
                account_id=s.account_id, environment=s.environment, operation=operation, **fields
            ),
            s.rpc_timeout,
        )
        if value["mode"] != s.mode or value["trade_ready"]:
            raise ProtocolError("OMS_EXECUTION_DOMAIN_MISMATCH")
        return value

    async def catchup(self):
        head = await self.oms("head")
        epoch, target = head["feed_epoch"], head["account_seq"]
        await self.service.bind_head(epoch, target, head["blocked"])
        _, seq = await self.service.cursor()
        while seq < target:
            page = await self.oms("events", feed_epoch=epoch, after_seq=seq, limit=100)
            await self.service.bind_head(page["feed_epoch"], page["account_seq"], page["blocked"])
            if not page["events"]:
                raise ProtocolError("ACCOUNT_REPLAY_INCOMPLETE")
            for raw in page["events"]:
                event = AccountEvent.model_validate(raw)
                await self.service.apply(event)
                seq = event.account_seq
                if seq >= target:
                    break
        self.service.last_sync = utcnow()

    async def start(self):
        s = self.settings

        async def disconnected():
            self.service.last_sync = None
            if not self.closing and not self.failure.done():
                self.failure.set_result(ProtocolError("NATS_DISCONNECTED_RECOVERY_REQUIRED"))

        self.nc = await nats.connect(
            s.nats_url,
            user=s.nats_user,
            password=s.nats_password.get_secret_value(),
            inbox_prefix=f"_INBOX.{s.environment}.portfolio",
            connect_timeout=3,
            max_reconnect_attempts=0,
            disconnected_cb=disconnected,
        )
        async with self.gate:
            await self.catchup()
        for subject in (
            f"{s.prefix}.rpc.portfolio.{s.account_id}.account",
            f"{s.prefix}.rpc.portfolio.{s.account_id}.strategy.*",
        ):
            await self.nc.subscribe(
                subject, cb=self.rpc, pending_msgs_limit=64, pending_bytes_limit=1048576
            )
        await self.nc.flush()
        self.spawn(self.replay())
        self.spawn(self.broker_loop())
        return self

    async def replay(self):
        while True:
            await asyncio.sleep(self.settings.poll_interval)
            async with self.gate:
                await self.catchup()

    async def broker_loop(self):
        s = self.settings
        while True:
            try:
                async with self.gate:
                    await self.catchup()
                    revision = await self.service.revision()
                data = await self.request(
                    f"{s.prefix}.rpc.exec.{s.account_id}.account",
                    Request(account_id=s.account_id, environment=s.environment, mode=s.mode),
                    s.broker_timeout,
                )
                snapshot = BrokerSnapshot.model_validate(data)
                async with self.gate:
                    await self.catchup()
                    await self.service.accept_broker(snapshot, revision)
            except (nats.errors.Error, ProtocolError, ValueError, TimeoutError) as error:
                async with self.store.transaction():
                    await self.store.set_meta(
                        "broker_error",
                        error.code if isinstance(error, ProtocolError) else type(error).__name__,
                    )
            await asyncio.sleep(s.broker_interval)

    async def rpc(self, message):
        request_id = "invalid"
        try:
            request = Request.model_validate_json(message.data)
            request_id = request.request_id
            self.service.scope(request)
            # Subject is authorized by the broker, never supplied as a payload selector.
            strategy = (
                message.subject.rsplit(".", 1)[1] if ".strategy." in message.subject else None
            )
            async with self.gate:
                await self.catchup()
                result = await self.service.snapshot(strategy)
            if len(encode(result).encode()) > 524288:
                raise ProtocolError("SNAPSHOT_CAPACITY")
            response = RpcResponse(request_id=request_id, ok=True, data=result)
        except (ValueError, TimeoutError, nats.errors.Error) as error:
            response = RpcResponse(
                request_id=request_id,
                ok=False,
                error=error.code if isinstance(error, ProtocolError) else type(error).__name__,
            )
        except Exception as error:
            if not self.failure.done():
                self.failure.set_result(error)
            response = RpcResponse(request_id=request_id, ok=False, error="PORTFOLIO_UNAVAILABLE")
        if message.reply:
            await message.respond(response.model_dump_json().encode())

    async def close(self):
        self.closing = True
        self.service.last_sync = None
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.nc:
            await self.nc.close()
