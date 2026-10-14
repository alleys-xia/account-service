"""Single writer, separate Risk database. No OMS database credentials or table access."""

import asyncio
from contextlib import asynccontextmanager
from importlib.resources import files

import psycopg
from oms_protocol.messages import ProtocolError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class Store:
    def __init__(self, dsn, *, environment="paper", account_id="default", mode="SIMULATION"):
        self.dsn = dsn
        self.header = dict(
            schema_version=1, environment=environment, account_id=account_id, mode=mode
        )
        self.lock = asyncio.Lock()
        self.db = None

    async def open(self):
        try:
            self.db = await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=5
            )
            await self.db.execute("SET statement_timeout = '5s'")
            await self.db.execute("SET lock_timeout = '3s'")
            row = await self.one("SELECT pg_try_advisory_lock(75603802) AS locked")
            if not row["locked"]:
                raise ProtocolError("RISK_ALREADY_RUNNING")
            await self.db.execute(files("risk_service").joinpath("sql/001_initial.sql").read_text())
            async with self.transaction():
                header = await self.meta("header")
                if header and (dict(mode="SIMULATION") | header) != self.header:
                    raise ProtocolError("DATABASE_IDENTITY_OR_SCHEMA_MISMATCH")
                await self.set_meta("header", self.header)
            return self
        except BaseException:
            await self.close()
            raise

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            if self.db is None or self.db.closed:
                raise ProtocolError("DATABASE_UNAVAILABLE")
            async with asyncio.timeout(5):
                async with self.db.transaction():
                    yield self

    async def one(self, query, params=()):
        return await (await self.db.execute(query, params)).fetchone()

    async def all(self, query, params=()):
        return await (await self.db.execute(query, params)).fetchall()

    async def meta(self, key, default=None):
        row = await self.one("SELECT value FROM risk.meta WHERE key=%s", (key,))
        return row["value"] if row else default

    async def set_meta(self, key, value):
        await self.db.execute(
            "INSERT INTO risk.meta VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, Jsonb(value)),
        )

    async def order(self, order_id):
        row = await self.one("SELECT data FROM risk.orders WHERE id=%s", (order_id,))
        return row["data"] if row else None

    async def save_order(self, order_id, data):
        await self.db.execute(
            "INSERT INTO risk.orders VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (order_id, Jsonb(data)),
        )

    async def pending(self):
        async with self.transaction():
            return await self.all(
                "SELECT * FROM risk.outbox WHERE NOT published ORDER BY seq LIMIT 100"
            )

    async def published(self, seq):
        async with self.transaction():
            await self.db.execute("UPDATE risk.outbox SET published=true WHERE seq=%s", (seq,))

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None
