"""Native entry point. No broker execution, simulator, or strategy is started here."""

import argparse
import asyncio
import importlib
import json
import signal

from oms_protocol.messages import ProtocolError

from .config import load_settings
from .service import Service
from .storage import Store
from .transport import Transport


def load_rules(names):
    result = []
    for name in names:
        module, attribute = name.split(":", 1)
        rule = getattr(importlib.import_module(module), attribute)()
        if not callable(getattr(rule, "check", None)):
            raise ValueError("custom rule must implement check(context)")
        result.append(rule)
    return tuple(result)


async def run(settings):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    store = Store(
        settings.database_url.get_secret_value(),
        environment=settings.environment,
        account_id=settings.account_id,
        mode=settings.mode,
    )
    transport = waiter = None
    try:
        await store.open()
        transport = Transport(
            Service(settings, store, extra_rules=load_rules(settings.extra_rules))
        )
        await transport.start()
        print(
            json.dumps({"event": "risk_started", **(await transport.service.health())}), flush=True
        )
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            (waiter, transport.failure), return_when=asyncio.FIRST_COMPLETED
        )
        if transport.failure in done:
            raise transport.failure.result()
    finally:
        if waiter:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        try:
            if transport:
                await transport.close()
        finally:
            await store.close()


def main():
    parser = argparse.ArgumentParser(description="Risk service (paper: SIMULATION or IB_PAPER)")
    parser.add_argument("--config", default="config/risk.example.toml")
    args = parser.parse_args()
    try:
        asyncio.run(run(load_settings(args.config)))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(
            json.dumps(
                dict(
                    event="risk_failed",
                    error=error.code if isinstance(error, ProtocolError) else type(error).__name__,
                )
            ),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
