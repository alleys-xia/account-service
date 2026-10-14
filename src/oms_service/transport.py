"""Durable message boundaries. A broker acknowledgement follows the database commit."""

import asyncio

import nats
from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError
from oms_protocol.common import digest, encode
from oms_protocol.messages import (
    ExecutionReport,
    OrderCommand,
    ProtocolError,
    RiskDecision,
    RpcRequest,
    RpcResponse,
)
from oms_protocol.subjects import consumer_name
from pydantic import ValidationError


class Transport:
    def __init__(self, service):
        self.service = service
        self.settings, self.store = service.settings, service.store
        self.nc = self.js = None
        self.tasks = []
        self.failure = asyncio.get_running_loop().create_future()

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)

        def done(value):
            if not value.cancelled() and value.exception() is not None and not self.failure.done():
                self.failure.set_result(value.exception())

        task.add_done_callback(done)

    async def consumer(self, name, subject):
        expected = ConsumerConfig(
            durable_name=name,
            filter_subject=subject,
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=30,
            max_deliver=-1,
            max_ack_pending=32,
            max_waiting=8,
        )
        try:
            current = (await self.js.consumer_info(self.settings.stream, name)).config
        except NotFoundError:
            await self.js.add_consumer(self.settings.stream, expected)
        else:
            for field in (
                "filter_subject",
                "deliver_policy",
                "ack_policy",
                "max_ack_pending",
                "max_deliver",
            ):
                if getattr(current, field) != getattr(expected, field):
                    raise ProtocolError("CONSUMER_CONFIG_MISMATCH:" + name)
        return await self.js.pull_subscribe_bind(
            stream=self.settings.stream,
            consumer=name,
            pending_msgs_limit=64,
            pending_bytes_limit=1048576,
        )

    async def start(self):
        s = self.settings
        self.nc = await asyncio.wait_for(
            nats.connect(
                s.nats_url,
                user=s.nats_user,
                password=s.nats_password.get_secret_value(),
                inbox_prefix=f"_INBOX.{s.environment}.oms",
                connect_timeout=3,
                max_reconnect_attempts=-1,
                reconnect_time_wait=1,
                pending_size=1048576,
            ),
            timeout=5,
        )
        self.js = self.nc.jetstream(timeout=s.rpc_timeout)
        prefix, account = s.prefix, s.account_id
        subjects = [
            f"{prefix}.cmd.oms.{account}.*",
            f"{prefix}.cmd.risk.{account}.check",
            f"{prefix}.cmd.exec.{account}.order",
            f"{prefix}.evt.risk.{account}.decision",
            f"{prefix}.evt.exec.{account}.broker",
            f"{prefix}.evt.oms.{account}.journal",
            f"{prefix}.evt.oms.{account}.strategy.*",
        ]
        expected = StreamConfig(
            name=s.stream,
            subjects=subjects,
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.NEW,
            max_bytes=s.stream_max_bytes,
            max_age=0,
            max_msgs=-1,
            max_msgs_per_subject=-1,
            max_msg_size=262144,
            num_replicas=1,
            duplicate_window=120,
            deny_delete=True,
            deny_purge=True,
        )
        try:
            current = (await self.js.stream_info(s.stream)).config
        except NotFoundError:
            await self.js.add_stream(expected)
        else:
            for field in (
                "subjects",
                "storage",
                "retention",
                "discard",
                "max_bytes",
                "max_age",
                "max_msgs",
                "max_msgs_per_subject",
                "max_msg_size",
                "num_replicas",
                "deny_delete",
                "deny_purge",
            ):
                if getattr(current, field) != getattr(expected, field):
                    raise ProtocolError("STREAM_CONFIG_MISMATCH:" + field)
        for strategy in s.strategies:
            subscription = await self.consumer(
                consumer_name(s.environment, account, strategy),
                f"{prefix}.evt.oms.{account}.strategy.{strategy}",
            )
            await subscription.unsubscribe()  # Provision only; the strategy owns consumption.
        for name, subject, model, handler in (
            ("oms_commands", f"{prefix}.cmd.oms.{account}.*", OrderCommand, self.service.command),
            ("oms_risk", f"{prefix}.evt.risk.{account}.decision", RiskDecision, self.service.risk),
            (
                "oms_execution",
                f"{prefix}.evt.exec.{account}.broker",
                ExecutionReport,
                self.service.execution,
            ),
        ):
            subscription = await self.consumer(name, subject)
            self.spawn(self.consume(name, subscription, model, handler))
        await self.nc.subscribe(
            f"{prefix}.rpc.oms.{account}.*",
            cb=self.rpc,
            pending_msgs_limit=128,
            pending_bytes_limit=1048576,
        )
        await self.nc.flush()
        self.spawn(self.publish_outbox())
        self.spawn(self.expire_waiting())
        return self

    async def consume(self, channel, subscription, model, handler):
        while True:
            try:
                messages = await subscription.fetch(1, timeout=1)
            except NatsTimeout:
                continue
            for message in messages:
                try:
                    value = model.model_validate_json(message.data)
                except (ValidationError, ValueError):
                    async with self.store.transaction():
                        await self.store.quarantine(
                            channel,
                            digest(message.data.hex()),
                            "INVALID_SCHEMA",
                            {
                                "subject": message.subject,
                                "raw": message.data[:262144].decode(errors="replace"),
                            },
                            freeze=channel != "oms_commands",
                        )
                    await message.ack_sync()
                    continue
                if channel == "oms_commands":
                    if value.operation != "cancel":
                        async with self.store.transaction():
                            count = await self.store.one(
                                "SELECT count(*) AS n FROM oms.outbox WHERE NOT published"
                            )
                        if count["n"] >= self.settings.max_outbox:
                            await message.nak(delay=1)
                            continue
                    await handler(value, message.subject.rsplit(".", 1)[1])
                else:
                    await handler(value)
                await message.ack_sync()

    async def rpc(self, message):
        request_id = "invalid"
        try:
            request = RpcRequest.model_validate_json(message.data)
            request_id = request.request_id
            async with asyncio.timeout(self.settings.rpc_timeout):
                result = await self.service.rpc(request, message.subject.rsplit(".", 1)[1])
            response = RpcResponse(request_id=request_id, ok=True, data=result)
        except (ProtocolError, ValidationError, TimeoutError) as error:
            response = RpcResponse(
                request_id=request_id,
                ok=False,
                error=error.code if isinstance(error, ProtocolError) else type(error).__name__,
            )
        except Exception as error:
            if not self.failure.done():
                self.failure.set_result(error)
            response = RpcResponse(request_id=request_id, ok=False, error="SERVICE_UNAVAILABLE")
        if message.reply:
            await message.respond(response.model_dump_json().encode())

    async def expire_waiting(self):
        while True:
            await self.service.expire_waiting()
            await asyncio.sleep(1)

    async def publish_outbox(self):
        while True:
            rows = await self.store.pending()
            for row in rows:
                # Never create a new command identity when a publish acknowledgement is lost.
                try:
                    await self.js.publish(
                        row["subject"],
                        encode(row["payload"]).encode(),
                        headers={"Nats-Msg-Id": row["message_id"]},
                    )
                except (nats.errors.Error, nats.js.errors.APIError):
                    await asyncio.sleep(0.5)
                    break
                await self.store.published(row["seq"])
            await asyncio.sleep(0.02 if rows else 0.1)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.nc:
            await self.nc.close()
