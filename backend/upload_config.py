"""Database-backed limits shared by upload endpoints and video validation."""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import db


@dataclass(frozen=True)
class UploadConfiguration:
    max_file_size_bytes: int
    max_video_bit_rate: int
    max_duration_seconds: int
    max_frames_per_second: int
    max_resolution_width: int
    max_resolution_height: int


_ENTRY_KEYS = {
    "upload.maxFileSizeBytes": "max_file_size_bytes",
    "upload.maxVideoBitRate": "max_video_bit_rate",
    "upload.maxDurationSeconds": "max_duration_seconds",
    "upload.maxFramesPerSecond": "max_frames_per_second",
    "upload.maxResolution.width": "max_resolution_width",
    "upload.maxResolution.height": "max_resolution_height",
}


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def load_upload_configuration() -> UploadConfiguration:
    rows = await db.pool().fetch(
        """
        select e.key, e.value
          from app_config c
          join app_config_entries e on e.config_id = c.id
         where c.enabled
           and e.key = any($1::text[])
        """,
        list(_ENTRY_KEYS),
    )
    values = {
        _ENTRY_KEYS[row["key"]]: _decode(row["value"])
        for row in rows
    }
    missing = set(_ENTRY_KEYS.values()) - set(values)
    if missing:
        raise RuntimeError(
            f"Active app config is missing upload settings: {sorted(missing)}"
        )

    configuration = UploadConfiguration(**values)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in configuration.__dict__.values()
    ):
        raise RuntimeError("Active upload settings must be positive integers")
    return configuration
