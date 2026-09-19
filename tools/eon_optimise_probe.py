"""Read-only volunteer price comparison; no Home Assistant or inverter access."""

import asyncio
import getpass
import json
from pathlib import Path
import sys

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "predbat"))
from eon_optimise_client import AuthenticationError, OptimiseClient, PriceFeedError


async def probe(email, password):
    """Read prices, exercise token renewal, and return only normalised rates."""
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        client = OptimiseClient(session, email, password)
        await client.fetch(asyncio.to_thread)
        client.expires = 0
        return await client.fetch(asyncio.to_thread)


def main():
    """Prompt privately; never include credentials or raw backend responses."""
    email = getpass.getpass("Optimise email (hidden): ")
    password = getpass.getpass("Optimise password (hidden): ")
    try:
        rates = asyncio.run(probe(email, password))
    except (AuthenticationError, PriceFeedError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"price_unit": "p/kWh", "reads_completed": 2, "rates": rates}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
