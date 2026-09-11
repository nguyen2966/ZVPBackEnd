"""Add or update upload limits in the enabled app configuration bundle.

Run without ``--apply`` to preview. Run with ``--apply`` during deployment to
persist the settings and let the existing trigger advance the config version.
"""

from __future__ import annotations

import asyncio
import json
import sys

import asyncpg

from .config import DATABASE_DSN
from .config_payload import flatten
from .routers.config import DEFAULT_PAYLOAD


async def main() -> None:
    apply = "--apply" in sys.argv
    entries = flatten({"upload": DEFAULT_PAYLOAD["upload"]})
    connection = await asyncpg.connect(DATABASE_DSN)
    try:
        config_id = await connection.fetchval(
            "select id from app_config where enabled"
        )
        if config_id is None:
            raise SystemExit("No enabled app_config bundle exists")

        print(f"Enabled app_config id: {config_id}")
        for key, value in entries.items():
            print(f"  {key} = {value}")

        if not apply:
            print("No changes written; add --apply to update the database.")
            return

        async with connection.transaction():
            await connection.executemany(
                """
                insert into app_config_entries (config_id, key, value)
                values ($1, $2, $3::jsonb)
                on conflict (config_id, key)
                do update set value = excluded.value
                """,
                [
                    (config_id, key, json.dumps(value))
                    for key, value in entries.items()
                ],
            )
        print("Upload configuration updated.")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
