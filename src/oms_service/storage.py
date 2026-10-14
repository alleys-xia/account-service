"""Single-writer PostgreSQL store; all mutations and journal/outbox writes are atomic."""

import asyncio
from contextlib import asynccontextmanager
from importlib.resources import files

import psycopg
from oms_protocol.common import uid
from oms_protocol.messages import ProtocolError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .domain import Order


class Store:
    def __init__(self, dsn, *, environment="paper", account_id="default", timeout=5):
        self.dsn, self.timeout = dsn, timeout
        self.header = dict(schema_version=1, environment=environment, account_id=account_id)
        self.prefix = f"{environment}.v1"
        self.account_id = account_id
        self.lock = asyncio.Lock()
        self.db = None
        self.feed_epoch = None

    async def open(self):
        try:
            self.db = await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=5
            )
            await self.db.execute("SET statement_timeout = '5s'")
            await self.db.execute("SET lock_timeout = '3s'")
            row = await (
                await self.db.execute("SELECT pg_try_advisory_lock(75603801) AS locked")
            ).fetchone()
            if not row["locked"]:
                raise ProtocolError("OMS_ALREADY_RUNNING")
            await self.db.execute(files("oms_service").joinpath("sql/001_initial.sql").read_text())
            async with self.transaction():
                header = await self.meta("header")
                if header and header != self.header:
                    raise ProtocolError("DATABASE_IDENTITY_OR_SCHEMA_MISMATCH")
                await self.set_meta("header", self.header)
                self.feed_epoch = await self.meta("feed_epoch") or uid()
                await self.set_meta("feed_epoch", self.feed_epoch)
                if await self.meta("account_seq") is None:
                    await self.set_meta("account_seq", 0)
            return self
        except BaseException:
            await self.close()
            raise

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            if self.db is None or self.db.closed:
                raise ProtocolError("DATABASE_UNAVAILABLE")
            async with asyncio.timeout(self.timeout):
                async with self.db.transaction():
                    yield self

    async def one(self, statement, params=()):
        return await (await self.db.execute(statement, params)).fetchone()

    async def all(self, statement, params=()):
        return await (await self.db.execute(statement, params)).fetchall()

    async def meta(self, key, default=None):
        row = await self.one("SELECT value FROM oms.meta WHERE key=%s", (key,))
        return row["value"] if row else default

    async def set_meta(self, key, value):
        await self.db.execute(
            "INSERT INTO oms.meta VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, Jsonb(value)),
        )

    async def order(self, order_id):
        row = await self.one("SELECT data FROM oms.orders WHERE id=%s", (order_id,))
        return Order.model_validate(row["data"]) if row else None

    async def save(self, order):
        await self.db.execute(
            "INSERT INTO oms.orders VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (
                order.order_id,
                order.strategy_instance_id,
                order.client_order_id,
                Jsonb(order.model_dump(mode="json")),
            ),
        )

    async def seen(self, channel, identifier, payload_hash):
        row = await self.one(
            "SELECT hash,result FROM oms.inbox WHERE channel=%s AND id=%s", (channel, identifier)
        )
        if row and row["hash"] != payload_hash:
            raise ProtocolError("IDEMPOTENCY_CONFLICT")
        return row["result"] if row else None

    async def remember(self, channel, identifier, payload_hash, result, payload=None):
        await self.db.execute(
            "INSERT INTO oms.inbox(channel,id,hash,result,payload) VALUES (%s,%s,%s,%s,%s)",
            (channel, identifier, payload_hash, Jsonb(result), Jsonb(payload or {})),
        )

    async def enqueue(self, subject, message_id, payload):
        await self.db.execute(
            "INSERT INTO oms.outbox(message_id,subject,payload) VALUES (%s,%s,%s)",
            (message_id, subject, Jsonb(payload)),
        )

    async def journal(self, payload, strategy=None):
        seq = await self.meta("account_seq", 0) + 1
        await self.set_meta("account_seq", seq)
        strategy_seq = None
        if strategy:
            row = await self.one(
                "INSERT INTO oms.strategy_heads VALUES (%s,1) ON CONFLICT(strategy) DO UPDATE SET seq=oms.strategy_heads.seq+1 RETURNING seq",
                (strategy,),
            )
            strategy_seq = row["seq"]
            payload = payload | {"strategy_seq": strategy_seq, "feed_epoch": self.feed_epoch}
        event_id = payload.get("event_id", uid())
        payload = payload | {"event_id": event_id}
        await self.db.execute(
            "INSERT INTO oms.journal VALUES (%s,%s,%s,%s,%s)",
            (seq, strategy, strategy_seq, event_id, Jsonb(payload)),
        )
        envelope = dict(
            schema_version=1,
            environment=self.header["environment"],
            account_id=self.account_id,
            account_seq=seq,
            event_id=event_id,
            payload=payload,
        )
        await self.enqueue(
            f"{self.prefix}.evt.oms.{self.account_id}.journal", event_id + ":account", envelope
        )
        if strategy:
            await self.enqueue(
                f"{self.prefix}.evt.oms.{self.account_id}.strategy.{strategy}",
                event_id + ":strategy",
                payload,
            )
        return seq

    async def quarantine(self, channel, message_id, reason, payload, *, freeze=True):
        await self.db.execute(
            "INSERT INTO oms.quarantine(channel,message_id,reason,payload) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (channel, message_id, reason, Jsonb(payload)),
        )
        if freeze:
            await self.set_meta("blocked", reason)

    async def pending(self, limit=100):
        async with self.transaction():
            return await self.all(
                "SELECT * FROM oms.outbox WHERE NOT published ORDER BY seq LIMIT %s", (limit,)
            )

    async def published(self, seq):
        async with self.transaction():
            await self.db.execute("UPDATE oms.outbox SET published=true WHERE seq=%s", (seq,))

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None
