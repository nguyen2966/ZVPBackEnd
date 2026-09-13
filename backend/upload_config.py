"""Read the upload file-size limit from the enabled database configuration."""

from __future__ import annotations

import json

from . import db


async def load_max_upload_file_size() -> int:
    value = await db.pool().fetchval(
        """
        select e.value
          from app_config c
          join app_config_entries e on e.config_id = c.id
         where c.enabled
           and e.key = 'upload.maxFileSizeBytes'
        """
    )
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError("Active upload.maxFileSizeBytes must be a positive integer")
    return value
