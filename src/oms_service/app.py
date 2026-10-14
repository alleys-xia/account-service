"""Native OMS service entry point. No Risk or Execution simulator is started here."""

import argparse
import asyncio
import json
import signal

from oms_protocol.messages import ProtocolError

from .config import load_settings
from .service import Service
from .storage import Store
from .transport import Transport


async def run(settings):
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)
    store = Store(
        settings.database_url.get_secret_value(),
        environment=settings.environment,
        account_id=settings.account_id,
        mode=settings.mode,
    )
    transport = None
    waiter = None
    try:
        await store.open()
        transport = Transport(Service(settings, store))
        await transport.start()
        print(
            json.dumps(
                dict(
                    event="oms_started",
                    environment=settings.environment,
                    account_id=settings.account_id,
                    mode=settings.mode,
                    trade_ready=False,
                )
            ),
            flush=True,
        )
        waiter = asyncio.create_task(stopping.wait())
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
    parser = argparse.ArgumentParser(description="Durable OMS (paper: SIMULATION or IB_PAPER)")
    parser.add_argument("--config", default="config/oms.example.toml")
    args = parser.parse_args()
    try:
        asyncio.run(run(load_settings(args.config)))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # DSNs and credentials must not appear in logs or tracebacks.
        print(
            json.dumps(
                dict(
                    event="oms_failed",
                    error=error.code if isinstance(error, ProtocolError) else type(error).__name__,
                )
            ),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
